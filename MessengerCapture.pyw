import ctypes
import json
import os
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

import webview

APP_NAME = "PrintFlow CRM"
START_URL = "https://www.messenger.com/marketplace/"


def data_dir():
    base = os.getenv("LOCALAPPDATA")
    root = (Path(base) / "PrintFlowCRM") if base else (Path.home() / ".printflowcrm")
    root.mkdir(parents=True, exist_ok=True)
    return root


CAPTURE_FILE = Path(sys.argv[1]) if len(sys.argv) > 1 else data_dir() / "messenger_capture.json"
PAYMENT_REQUEST_FILE = data_dir() / "messenger_payment_request.json"
STORAGE_DIR = data_dir() / "messenger_browser"
STORAGE_DIR.mkdir(parents=True, exist_ok=True)

WINDOW_TITLE = "PrintFlow CRM — Marketplace Messenger"
_MUTEX_HANDLE = None

def focus_existing_window():
    if os.name != "nt":
        return False
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, WINDOW_TITLE)
        if not hwnd or not user32.IsWindow(hwnd):
            return False
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        return bool(user32.IsWindowVisible(hwnd))
    except Exception:
        return False

def enforce_single_instance():
    global _MUTEX_HANDLE
    if os.name != "nt":
        return True
    try:
        kernel32 = ctypes.windll.kernel32
        _MUTEX_HANDLE = kernel32.CreateMutexW(None, False, "Local\\PrintFlowCRM_MessengerBrowser")
        # ERROR_ALREADY_EXISTS = 183
        if kernel32.GetLastError() == 183:
            # Only yield to the prior process when it still owns a real visible
            # browser window. A WebView process can briefly survive after its window
            # is closed; that stale mutex must not block reopening Messenger.
            if focus_existing_window():
                return False
            return True
    except Exception:
        pass
    return True


class Bridge:
    def capture(self, text, url="", title=""):
        text = (text or "").strip()
        if not text:
            return "No chat text found"
        payload = {
            "captured_at": datetime.now().isoformat(timespec="seconds"),
            "text": text,
            "url": url or "",
            "title": title or "",
        }
        tmp = Path(tempfile.gettempdir()) / ("printflow-messenger-" + str(os.getpid()) + ".json")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(CAPTURE_FILE)
        return "Captured to PrintFlow ✓"

    def payment_sent(self, request_id):
        try:
            if not PAYMENT_REQUEST_FILE.exists():
                return False
            payload = json.loads(PAYMENT_REQUEST_FILE.read_text(encoding="utf-8"))
            if str(payload.get("request_id") or "") != str(request_id or ""):
                return False
            payload["status"] = "sent"
            payload["sent_at"] = datetime.now().isoformat(timespec="seconds")
            tmp = Path(tempfile.gettempdir()) / ("printflow-payment-sent-" + str(os.getpid()) + ".json")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(PAYMENT_REQUEST_FILE)
            return True
        except Exception:
            return False


bridge = Bridge()
window = webview.create_window(
    WINDOW_TITLE,
    START_URL,
    js_api=bridge,
    width=1220,
    height=860,
    min_size=(850, 600),
)

INJECT = r'''
(() => {
  // Messenger normally makes some chat text awkward/impossible to select in an
  // embedded WebView. Force normal desktop text selection so drag + Ctrl+C works.
  if (!document.getElementById('printflow-copy-style')) {
    const style = document.createElement('style');
    style.id = 'printflow-copy-style';
    style.textContent = `
      body *:not(input):not(textarea):not(button):not([contenteditable="true"]) {
        -webkit-user-select: text !important;
        user-select: text !important;
      }
    `;
    document.documentElement.appendChild(style);
  }

  const existingCapture = document.getElementById('printflow-capture-chat');
  const pendingPayment = window.__PRINTFLOW_PAYMENT_REQUEST__;
  const existingPaymentPanel = document.getElementById('printflow-payment-reminder');
  const panelRequestId = existingPaymentPanel ? existingPaymentPanel.dataset.requestId : '';
  const incomingRequestId = pendingPayment ? String(pendingPayment.request_id || '') : '';
  if (existingPaymentPanel && incomingRequestId && panelRequestId !== incomingRequestId) {
    existingPaymentPanel.remove();
  }
  const currentPaymentPanel = document.getElementById('printflow-payment-reminder');
  if (existingCapture && (!pendingPayment || (currentPaymentPanel && currentPaymentPanel.dataset.requestId === incomingRequestId))) return;
  // A newly armed reminder may arrive while this single browser window is
  // already open. Recreate only the capture button so the payment panel can
  // be injected without opening a second Messenger window.
  if (existingCapture) existingCapture.remove();

  const btn = document.createElement('button');
  btn.id = 'printflow-capture-chat';
  btn.textContent = 'Capture Chat → PrintFlow';
  Object.assign(btn.style, {
    position:'fixed', top:'14px', right:'16px', zIndex:'2147483647',
    background:'#2563eb', color:'#fff', border:'1px solid #60a5fa',
    borderRadius:'9px', padding:'10px 14px', font:'600 14px Segoe UI,Arial,sans-serif',
    boxShadow:'0 6px 20px rgba(0,0,0,.35)', cursor:'pointer'
  });

  const visibleRect = el => {
    if (!el || !el.getBoundingClientRect) return null;
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    if (r.width < 2 || r.height < 2 || cs.display === 'none' || cs.visibility === 'hidden') return null;
    return r;
  };

  const findComposer = () => {
    const vw = innerWidth, vh = innerHeight;
    const selectors = [
      '[contenteditable=\"true\"][role=\"textbox\"]',
      '[contenteditable=\"true\"]',
      'textarea[placeholder]',
      'textarea'
    ];
    const found = [];
    for (const sel of selectors) {
      try { document.querySelectorAll(sel).forEach(el => found.push(el)); } catch (_) {}
    }
    let best = null, bestScore = -Infinity;
    for (const el of [...new Set(found)]) {
      const r = visibleRect(el);
      if (!r) continue;
      const cx = r.left + r.width / 2;
      const cy = r.top + r.height / 2;
      // The real Messenger composer is low on the page and in the center conversation column.
      if (cy < vh * 0.58 || cx < vw * 0.22 || cx > vw * 0.82) continue;
      const label = (
        (el.getAttribute('aria-label') || '') + ' ' +
        (el.getAttribute('data-lexical-editor') || '') + ' ' +
        (el.getAttribute('placeholder') || '')
      ).toLowerCase();
      let score = cy + 500;
      score -= Math.abs(cx - vw * 0.52) * 0.6;
      if (/message|reply|write|chat/.test(label)) score += 2500;
      if (el.getAttribute('contenteditable') === 'true') score += 1000;
      if (score > bestScore) { bestScore = score; best = el; }
    }
    return best;
  };

  const findConversationRoot = composer => {
    if (!composer) return null;
    const vw = innerWidth, vh = innerHeight;
    let el = composer;
    let best = null, bestScore = -Infinity;

    // Only inspect ancestors of the CENTER message composer. The left Chats column is never eligible.
    for (let depth = 0; el && el !== document.body && el !== document.documentElement && depth < 18; depth++, el = el.parentElement) {
      const r = visibleRect(el);
      if (!r) continue;
      const text = (el.innerText || '').trim();
      if (text.length < 20) continue;
      const widthRatio = r.width / vw;
      const heightRatio = r.height / vh;
      const center = (r.left + r.right) / 2 / vw;
      const rightRatio = r.right / vw;
      const leftRatio = r.left / vw;

      if (heightRatio < 0.50 || widthRatio < 0.28 || widthRatio > 0.78) continue;
      if (center < 0.34 || center > 0.70) continue;

      let score = 0;
      score += Math.min(text.length, 18000);
      score += heightRatio * 12000;
      score += (1 - Math.abs(center - 0.52)) * 9000;
      if (rightRatio < 0.86) score += 7000;
      if (leftRatio > 0.16) score += 3500;
      if (el.querySelector('[contenteditable=\"true\"][role=\"textbox\"], [contenteditable=\"true\"], textarea')) score += 8000;
      if (/search messenger/i.test(text)) score -= 30000;
      if (/facebook marketplace assistant/i.test(text) && /search messenger/i.test(text)) score -= 12000;
      score -= widthRatio * 4000;
      score -= depth * 80;
      if (score > bestScore) { bestScore = score; best = el; }
    }
    return best;
  };

  const cleanConversationText = text => {
    const junkExact = new Set([
      'Mark as pending', 'More options', 'Mute', 'Search',
      'Customize chat', 'Chat members', 'Media, files and links', 'Privacy & support',
      'Aa', 'GIF'
    ]);
    const lines = (text || '').split(/\n+/).map(x => x.trim()).filter(Boolean);
    return lines.filter(line => !junkExact.has(line)).join('\n').trim();
  };

  btn.onclick = async () => {
    const old = btn.textContent;
    btn.textContent = 'Capturing center chat…';
    try {
      const composer = findComposer();
      const target = findConversationRoot(composer);
      if (!composer || !target) throw new Error('Could not isolate the open center conversation.');
      const text = cleanConversationText(target.innerText || '');
      if (!text || text.length < 10) throw new Error('No center-chat text found.');
      const result = await window.pywebview.api.capture(text, location.href, document.title);
      btn.textContent = result || 'Captured ✓';
      setTimeout(() => { btn.textContent = old; }, 2800);
    } catch (e) {
      console.error('PrintFlow capture error', e);
      btn.textContent = 'Center chat not found';
      setTimeout(() => { btn.textContent = old; }, 3500);
    }
  };

  document.documentElement.appendChild(btn);

  // Smart multilingual sales replies for the currently open Marketplace chat.
  // PrintFlow prepares the reply automatically but requires one deliberate click
  // before sending, preventing unattended spam or replies to the wrong buyer.
  if (!document.getElementById('printflow-smart-reply')) {
    const smartPanel = document.createElement('div');
    smartPanel.id = 'printflow-smart-reply';
    Object.assign(smartPanel.style, {
      position:'fixed', top:'66px', left:'50%', transform:'translateX(-50%)',
      zIndex:'2147483646', width:'min(620px,calc(100vw - 40px))',
      background:'#111827', color:'#fff', border:'2px solid #3b82f6',
      borderRadius:'10px', padding:'11px 13px',
      font:'13px Segoe UI,Arial,sans-serif',
      boxShadow:'0 8px 26px rgba(0,0,0,.45)'
    });
    smartPanel.innerHTML =
      '<div style="display:flex;align-items:center;gap:10px;margin-bottom:7px">' +
        '<div style="font-weight:700;color:#93c5fd;flex:1">PrintFlow Smart Sales Reply</div>' +
        '<div id="printflow-smart-language" style="font-size:11px;color:#cbd5e1">Detecting language…</div>' +
        '<button id="printflow-smart-close" type="button" style="border:0;background:transparent;color:#94a3b8;font-size:18px;cursor:pointer">×</button>' +
      '</div>' +
      '<textarea id="printflow-smart-text" rows="3" style="box-sizing:border-box;width:100%;resize:vertical;border:1px solid #475569;border-radius:7px;background:#0f172a;color:#fff;padding:8px;font:13px Segoe UI,Arial,sans-serif"></textarea>' +
      '<div style="display:flex;align-items:center;gap:8px;margin-top:8px">' +
        '<div id="printflow-smart-status" style="flex:1;font-size:11px;color:#93c5fd">Opening the conversation…</div>' +
        '<button id="printflow-smart-refresh" type="button" style="padding:7px 10px;border:1px solid #64748b;border-radius:7px;background:#1e293b;color:#fff;font-weight:600;cursor:pointer">Refresh Reply</button>' +
        '<button id="printflow-smart-send" type="button" style="padding:7px 12px;border:1px solid #60a5fa;border-radius:7px;background:#2563eb;color:#fff;font-weight:700;cursor:pointer">Send Smart Reply</button>' +
      '</div>';
    document.documentElement.appendChild(smartPanel);

    const replyBox = document.getElementById('printflow-smart-text');
    const languageBox = document.getElementById('printflow-smart-language');
    const smartStatus = document.getElementById('printflow-smart-status');
    const setSmartStatus = (text, color='#93c5fd') => {
      if (smartStatus) {
        smartStatus.textContent = text;
        smartStatus.style.color = color;
      }
    };

    const languageNames = {
      en:'English', es:'Spanish', fr:'French', pt:'Portuguese',
      de:'German', it:'Italian'
    };
    const replies = {
      en:"Hi! Yes, it’s still available. Are you looking for local pickup near Aiken, SC, or would you need it shipped? I can also make custom sizes and colors.",
      es:"¡Hola! Sí, todavía está disponible. ¿Prefieres recogerlo cerca de Aiken, Carolina del Sur, o necesitas envío? También puedo hacer tamaños y colores personalizados.",
      fr:"Bonjour ! Oui, c’est toujours disponible. Préférez-vous le récupérer près d’Aiken, en Caroline du Sud, ou avez-vous besoin d’une livraison ? Je peux aussi faire des tailles et couleurs personnalisées.",
      pt:"Olá! Sim, ainda está disponível. Você prefere retirar perto de Aiken, Carolina do Sul, ou precisa de envio? Também posso fazer tamanhos e cores personalizados.",
      de:"Hallo! Ja, der Artikel ist noch verfügbar. Möchten Sie ihn in der Nähe von Aiken, South Carolina, abholen oder benötigen Sie Versand? Ich kann auch individuelle Größen und Farben anfertigen.",
      it:"Ciao! Sì, è ancora disponibile. Preferisci il ritiro vicino ad Aiken, South Carolina, oppure hai bisogno della spedizione? Posso anche realizzare misure e colori personalizzati."
    };

    const detectLanguage = text => {
      const value = String(text || '').toLowerCase();
      const scores = {
        es:['hola','sigue disponible','todavía','precio','cuánto','cuanto','envío','envio','gracias','interesado'],
        fr:['bonjour','toujours disponible','combien','prix','livraison','merci','intéressé','interesse'],
        pt:['olá','ola','ainda está disponível','ainda esta disponivel','preço','preco','envio','obrigado','interessado'],
        de:['hallo','verfügbar','verfugbar','preis','versand','danke','interessiert'],
        it:['ciao','disponibile','prezzo','spedizione','grazie','interessato']
      };
      let best='en', bestScore=0;
      for (const [code, words] of Object.entries(scores)) {
        const score = words.reduce((total, word) => total + (value.includes(word) ? 1 : 0), 0);
        if (score > bestScore) { best=code; bestScore=score; }
      }
      return best;
    };

    const conversationSnapshot = () => {
      const composer = findComposer();
      const root = findConversationRoot(composer);
      if (!composer || !root) return null;
      const text = cleanConversationText(root.innerText || '');
      return {composer, text, key:location.href + '|' + text.slice(-500)};
    };

    const prepareSmartReply = () => {
      const snapshot = conversationSnapshot();
      if (!snapshot || !snapshot.text) {
        setSmartStatus('Open a buyer conversation so PrintFlow can prepare a reply.', '#fbbf24');
        return;
      }
      const language = detectLanguage(snapshot.text);
      if (languageBox) languageBox.textContent = languageNames[language] || 'English';
      if (replyBox) {
        replyBox.value = replies[language] || replies.en;
        replyBox.dataset.conversationKey = snapshot.key;
      }
      const sentKey = localStorage.getItem('printflow-smart-last-sent') || '';
      if (sentKey === snapshot.key) {
        setSmartStatus('A smart reply was already sent for this visible conversation.', '#fbbf24');
      } else {
        setSmartStatus('Reply prepared automatically. Review or edit it, then send.');
      }
    };

    const fillComposer = (composer, message) => {
      composer.focus();
      if (composer.isContentEditable) {
        const selection = window.getSelection();
        const range = document.createRange();
        range.selectNodeContents(composer);
        selection.removeAllRanges();
        selection.addRange(range);
        document.execCommand('delete', false, null);
        document.execCommand('insertText', false, message);
      } else {
        composer.value = message;
        composer.dispatchEvent(new InputEvent('input', {
          bubbles:true, inputType:'insertText', data:null
        }));
      }
    };

    document.getElementById('printflow-smart-close').onclick = () => smartPanel.remove();
    document.getElementById('printflow-smart-refresh').onclick = prepareSmartReply;
    document.getElementById('printflow-smart-send').onclick = async () => {
      const snapshot = conversationSnapshot();
      const message = String(replyBox ? replyBox.value : '').trim();
      if (!snapshot) {
        setSmartStatus('Message box not found. Open the buyer conversation again.', '#fca5a5');
        return;
      }
      if (!message) {
        setSmartStatus('The reply is empty.', '#fca5a5');
        return;
      }
      const alreadySent = localStorage.getItem('printflow-smart-last-sent') || '';
      if (alreadySent === snapshot.key) {
        setSmartStatus('This conversation was already replied to. Edit the reply or wait for a new buyer message.', '#fbbf24');
        return;
      }
      try {
        fillComposer(snapshot.composer, message);
      } catch (_) {
        setSmartStatus('Could not fill the message box. Nothing was sent.', '#fca5a5');
        return;
      }
      await new Promise(resolve => setTimeout(resolve, 450));
      const composed = String(
        snapshot.composer.isContentEditable ? snapshot.composer.innerText : snapshot.composer.value
      ).replace(/\s+/g, ' ').trim();
      const expectedMessage = message.replace(/\s+/g, ' ').trim();
      if (composed !== expectedMessage) {
        setSmartStatus('The reply did not fill exactly once. It was not sent.', '#fca5a5');
        return;
      }
      const send = [...document.querySelectorAll('button,[role="button"]')].find(el => {
        const r = visibleRect(el);
        if (!r) return false;
        const label = (
          (el.getAttribute('aria-label') || '') + ' ' +
          (el.title || '') + ' ' + (el.innerText || '')
        ).trim();
        return /^(send|press enter to send)$/i.test(label) || /send message/i.test(label);
      });
      if (!send) {
        setSmartStatus('Reply filled. Review it and press Messenger’s Send button.', '#fbbf24');
        return;
      }
      send.click();
      localStorage.setItem('printflow-smart-last-sent', snapshot.key);
      setSmartStatus('Smart reply sent ✓', '#86efac');
    };

    let lastConversation = '';
    setInterval(() => {
      if (!document.getElementById('printflow-smart-reply')) return;
      const snapshot = conversationSnapshot();
      const key = snapshot ? snapshot.key : '';
      if (key && key !== lastConversation) {
        lastConversation = key;
        prepareSmartReply();
      }
    }, 2200);
    setTimeout(prepareSmartReply, 900);
  }

  const payment = window.__PRINTFLOW_PAYMENT_REQUEST__;
  if (payment && payment.status === 'armed' && !document.getElementById('printflow-payment-reminder')) {
    const panel = document.createElement('div');
    panel.id = 'printflow-payment-reminder';
    panel.dataset.requestId = String(payment.request_id || '');
    Object.assign(panel.style, {
      position:'fixed', top:'66px', right:'16px', zIndex:'2147483647', width:'360px',
      background:'#111827', color:'#fff', border:'2px solid #f59e0b', borderRadius:'10px',
      padding:'12px 14px', font:'14px Segoe UI,Arial,sans-serif', boxShadow:'0 8px 26px rgba(0,0,0,.45)'
    });
    panel.innerHTML = `<div style="font-weight:700;color:#fbbf24;margin-bottom:6px">Payment reminder armed</div>
      <div>Click <b>${String(payment.buyer_first_name || payment.buyer_name || 'the buyer')}</b> in the conversation list.</div>
      <div style="font-size:12px;color:#cbd5e1;margin-top:7px">${String(payment.message || '')}</div>
      <div id="printflow-payment-status" style="font-size:12px;color:#93c5fd;margin-top:8px">Waiting for the matching conversation…</div>
      <button id="printflow-send-anyway" type="button" style="display:none;width:100%;margin-top:10px;padding:8px 10px;border:1px solid #fca5a5;border-radius:7px;background:#7f1d1d;color:#fff;font:700 12px Segoe UI,Arial,sans-serif;cursor:pointer">Send Anyway to This Conversation</button>`;
    document.documentElement.appendChild(panel);

    const normalize = value => String(value || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
    const expected = normalize(payment.buyer_first_name || payment.buyer_name).split(' ')[0] || '';
    let sending = false;

    const setStatus = (text, color='#93c5fd') => {
      const el = document.getElementById('printflow-payment-status');
      if (el) { el.textContent = text; el.style.color = color; }
    };

    const overrideButton = document.getElementById('printflow-send-anyway');
    const hideOverride = () => { if (overrideButton) overrideButton.style.display = 'none'; };

    const fillAndSend = async () => {
      const requestId = String(payment.request_id || '');
      if (sending || window.__PRINTFLOW_SENDING_REQUEST_ID__ === requestId) return;
      sending = true;
      window.__PRINTFLOW_SENDING_REQUEST_ID__ = requestId;
      setStatus('Opening conversation and preparing reminder…');
      await new Promise(resolve => setTimeout(resolve, 1200));
      const composer = findComposer();
      if (!composer) { sending=false; window.__PRINTFLOW_SENDING_REQUEST_ID__=''; setStatus('Message box not found. Click the buyer conversation again.', '#fca5a5'); return; }
      composer.focus();
      try {
        const message = String(payment.message || '');
        if (composer.isContentEditable) {
          // Scope the selection to the composer. document.execCommand('selectAll')
          // can select Messenger's surrounding document, and manually dispatching
          // an insertText InputEvent makes Lexical apply the same text a second time.
          const selection = window.getSelection();
          const range = document.createRange();
          range.selectNodeContents(composer);
          selection.removeAllRanges();
          selection.addRange(range);
          document.execCommand('delete', false, null);
          document.execCommand('insertText', false, message);
        } else {
          composer.value = message;
          composer.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'insertText', data:null}));
        }
      } catch (_) {
        sending=false; window.__PRINTFLOW_SENDING_REQUEST_ID__=''; setStatus('Could not fill the message box. Click the conversation again.', '#fca5a5'); return;
      }
      await new Promise(resolve => setTimeout(resolve, 450));
      const expectedMessage = String(payment.message || '').replace(/\s+/g, ' ').trim();
      const composedMessage = String(composer.isContentEditable ? composer.innerText : composer.value).replace(/\s+/g, ' ').trim();
      if (composedMessage !== expectedMessage) {
        sending=false;
        window.__PRINTFLOW_SENDING_REQUEST_ID__='';
        setStatus('The reminder did not fill exactly once. It was not sent; click the buyer again to retry.', '#fca5a5');
        return;
      }
      const buttons = [...document.querySelectorAll('button,[role="button"]')];
      const send = buttons.find(el => {
        const r=visibleRect(el); if(!r) return false;
        const label=((el.getAttribute('aria-label')||'')+' '+(el.title||'')+' '+(el.innerText||'')).trim();
        return /^(send|press enter to send)$/i.test(label) || /send message/i.test(label);
      });
      if (!send) {
        sending=false;
        window.__PRINTFLOW_SENDING_REQUEST_ID__='';
        setStatus('Reminder filled in. Review it and press Send.', '#fbbf24');
        return;
      }
      send.click();
      setStatus('Payment reminder sent ✓', '#86efac');
      try { await window.pywebview.api.payment_sent(payment.request_id); } catch (_) {}
      setTimeout(() => panel.remove(), 4500);
    };

    if (overrideButton) {
      overrideButton.addEventListener('click', event => {
        event.preventDefault();
        event.stopPropagation();
        hideOverride();
        setStatus('Name mismatch overridden. Sending to the open conversation…', '#fbbf24');
        fillAndSend();
      });
    }

    document.addEventListener('click', event => {
      if (sending || !window.__PRINTFLOW_PAYMENT_REQUEST__ || event.clientX > innerWidth * .42) return;
      let el=event.target, text='';
      for(let i=0; el && i<6; i++,el=el.parentElement) {
        const r=visibleRect(el); const candidate=(el.innerText||el.textContent||'').trim();
        if(r && candidate && candidate.length<500) text += ' ' + candidate;
      }
      const clicked=normalize(text);
      if (!expected || !clicked.split(' ').includes(expected)) {
        setStatus(`That does not look like ${payment.buyer_first_name || payment.buyer_name}. Nothing was sent. If this is the right person, use Send Anyway below.`, '#fca5a5');
        if (overrideButton) overrideButton.style.display = 'block';
        return;
      }
      hideOverride();
      setStatus(`Matched ${payment.buyer_first_name || payment.buyer_name}. Sending…`);
      fillAndSend();
    }, true);
  }
})();
'''


def keep_button_alive(win):
    # Facebook is a single-page app and may replace its DOM while navigating conversations.
    while True:
        time.sleep(2.5)
        try:
            payment = None
            try:
                if PAYMENT_REQUEST_FILE.exists():
                    candidate = json.loads(PAYMENT_REQUEST_FILE.read_text(encoding="utf-8"))
                    if candidate.get("status") == "armed":
                        payment = candidate
            except Exception:
                payment = None
            win.run_js("window.__PRINTFLOW_PAYMENT_REQUEST__ = " + json.dumps(payment, ensure_ascii=False) + ";")
            win.run_js(INJECT)
        except Exception:
            try:
                if not win.get_current_url():
                    break
            except Exception:
                pass


if __name__ == "__main__":
    if enforce_single_instance():
        webview.start(keep_button_alive, window, private_mode=False, storage_path=str(STORAGE_DIR))

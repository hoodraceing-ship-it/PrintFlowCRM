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

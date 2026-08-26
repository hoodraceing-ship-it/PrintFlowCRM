import ctypes
import json
import os
import ssl
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
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


    _translation_cache = {}
    _translation_lock = threading.Lock()

    def translate(self, text, target="en"):
        text = str(text or "").strip()
        target = str(target or "en").strip().lower()
        if not text:
            return {"ok": False, "text": "", "source": "", "error": "No text"}
        text = text[:1200]
        key = (text, target)
        with self._translation_lock:
            cached = self._translation_cache.get(key)
        if cached:
            return cached

        normalized = " ".join(text.lower().replace("¿", "").replace("?", "").split())
        offline_es = {
            "hola": "Hello",
            "hola sigue disponible": "Hi, is it still available?",
            "aún está disponible": "Is it still available?",
            "aun esta disponible": "Is it still available?",
            "sigue disponible": "Is it still available?",
            "cuál es la ubicación": "What is the location?",
            "cual es la ubicacion": "What is the location?",
            "dónde está baño": "Where is the bathroom?",
            "donde esta bano": "Where is the bathroom?",
            "estafa es lo siento": "It is a scam, sorry.",
        }
        if target == "en" and normalized in offline_es:
            result = {"ok": True, "text": offline_es[normalized], "source": "es", "error": ""}
            with self._translation_lock:
                self._translation_cache[key] = result
            return result

        try:
            query = urllib.parse.urlencode({
                "client": "gtx", "sl": "auto", "tl": target, "dt": "t", "q": text
            })
            request = urllib.request.Request(
                "https://translate.googleapis.com/translate_a/single?" + query,
                headers={"User-Agent": "Mozilla/5.0 PrintFlowCRM/0.7.95"},
            )
            context = ssl.create_default_context()
            try:
                import certifi
                context = ssl.create_default_context(cafile=certifi.where())
            except Exception:
                pass
            with urllib.request.urlopen(request, timeout=15, context=context) as response:
                payload = json.loads(response.read().decode("utf-8"))
            translated = "".join(
                str(part[0] or "") for part in (payload[0] or [])
                if isinstance(part, list) and part
            ).strip()
            result = {
                "ok": bool(translated),
                "text": translated,
                "source": str(payload[2] or "") if len(payload) > 2 else "",
                "error": "" if translated else "Google returned no translation",
            }
        except Exception as exc:
            result = {
                "ok": False, "text": "", "source": "",
                "error": type(exc).__name__ + ": " + str(exc),
            }
        if result["ok"]:
            with self._translation_lock:
                if len(self._translation_cache) >= 300:
                    self._translation_cache.clear()
                self._translation_cache[key] = result
        return result


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
      position:'fixed', top:'66px', left:'calc(50% - 310px)', zIndex:'2147483646',
      width:'620px', minWidth:'360px', minHeight:'280px',
      maxWidth:'calc(100vw - 20px)', maxHeight:'calc(100vh - 20px)',
      resize:'both', overflow:'auto', boxSizing:'border-box',
      background:'#111827', color:'#fff', border:'2px solid #3b82f6',
      borderRadius:'10px', padding:'11px 13px',
      font:'13px Segoe UI,Arial,sans-serif',
      boxShadow:'0 8px 26px rgba(0,0,0,.45)'
    });
    smartPanel.innerHTML =
      '<div id="printflow-smart-drag" style="display:flex;align-items:center;gap:10px;margin:-5px -5px 8px;padding:5px;cursor:move;user-select:none">' +
        '<div style="font-weight:700;color:#93c5fd;flex:1">PrintFlow Smart Sales Reply</div>' +
        '<div id="printflow-smart-language" style="font-size:11px;color:#cbd5e1">Detecting language…</div>' +
        '<button id="printflow-smart-close" type="button" style="border:0;background:transparent;color:#94a3b8;font-size:18px;cursor:pointer">×</button>' +
      '</div>' +
      '<div style="border:1px solid #334155;border-radius:7px;padding:7px 9px;margin-bottom:7px;background:#0f172a">' +
        '<div style="font-size:10px;font-weight:700;color:#94a3b8;text-transform:uppercase">Buyer said (English)</div>' +
        '<div id="printflow-smart-buyer-translation" style="margin-top:3px;color:#e2e8f0">Waiting for a buyer message…</div>' +
      '</div>' +
      '<textarea id="printflow-smart-text" rows="3" style="box-sizing:border-box;width:100%;resize:vertical;border:1px solid #475569;border-radius:7px;background:#0f172a;color:#fff;padding:8px;font:13px Segoe UI,Arial,sans-serif"></textarea>' +
      '<div style="border:1px solid #334155;border-radius:7px;padding:7px 9px;margin-top:7px;background:#0f172a">' +
        '<div style="font-size:10px;font-weight:700;color:#94a3b8;text-transform:uppercase">You are sending (English)</div>' +
        '<div id="printflow-smart-reply-translation" style="margin-top:3px;color:#e2e8f0">Preparing translation…</div>' +
      '</div>' +
      '<div style="display:flex;align-items:center;gap:8px;margin-top:8px">' +
        '<div id="printflow-smart-status" style="flex:1;font-size:11px;color:#93c5fd">Opening the conversation…</div>' +
        '<button id="printflow-smart-refresh" type="button" style="padding:7px 10px;border:1px solid #64748b;border-radius:7px;background:#1e293b;color:#fff;font-weight:600;cursor:pointer">Refresh Reply</button>' +
        '<button id="printflow-smart-send" type="button" style="padding:7px 12px;border:1px solid #60a5fa;border-radius:7px;background:#2563eb;color:#fff;font-weight:700;cursor:pointer">Send Smart Reply</button>' +
      '</div>';
    document.documentElement.appendChild(smartPanel);

    const geometryKey = 'printflow-smart-geometry-v1';
    const saveGeometry = () => {
      const r = smartPanel.getBoundingClientRect();
      localStorage.setItem(geometryKey, JSON.stringify({
        left:Math.round(r.left), top:Math.round(r.top),
        width:Math.round(r.width), height:Math.round(r.height)
      }));
    };
    const clampPanel = () => {
      const r = smartPanel.getBoundingClientRect();
      smartPanel.style.left = Math.max(0, Math.min(r.left, window.innerWidth - 80)) + 'px';
      smartPanel.style.top = Math.max(0, Math.min(r.top, window.innerHeight - 50)) + 'px';
      smartPanel.style.width = Math.min(r.width, window.innerWidth - 10) + 'px';
      if (smartPanel.style.height) smartPanel.style.height = Math.min(r.height, window.innerHeight - 10) + 'px';
    };
    try {
      const saved = JSON.parse(localStorage.getItem(geometryKey) || 'null');
      if (saved) {
        smartPanel.style.left = Number(saved.left || 0) + 'px';
        smartPanel.style.top = Number(saved.top || 0) + 'px';
        smartPanel.style.width = Math.max(360, Number(saved.width || 620)) + 'px';
        if (saved.height) smartPanel.style.height = Math.max(280, Number(saved.height)) + 'px';
      }
    } catch (_) {}
    clampPanel();

    let dragState = null;
    document.getElementById('printflow-smart-drag').addEventListener('mousedown', event => {
      if (event.button !== 0 || event.target.closest('button')) return;
      const r = smartPanel.getBoundingClientRect();
      dragState = {x:event.clientX, y:event.clientY, left:r.left, top:r.top};
      event.preventDefault();
    });
    document.addEventListener('mousemove', event => {
      if (!dragState || !document.getElementById('printflow-smart-reply')) return;
      smartPanel.style.left = Math.max(0, Math.min(dragState.left + event.clientX - dragState.x, window.innerWidth - 80)) + 'px';
      smartPanel.style.top = Math.max(0, Math.min(dragState.top + event.clientY - dragState.y, window.innerHeight - 50)) + 'px';
    });
    document.addEventListener('mouseup', () => {
      if (dragState) { dragState = null; saveGeometry(); }
    });
    window.addEventListener('resize', () => { clampPanel(); saveGeometry(); });
    if (window.ResizeObserver) {
      let resizeTimer = 0;
      new ResizeObserver(() => {
        clearTimeout(resizeTimer);
        resizeTimer = setTimeout(saveGeometry, 180);
      }).observe(smartPanel);
    }

    const replyBox = document.getElementById('printflow-smart-text');
    const languageBox = document.getElementById('printflow-smart-language');
    const buyerTranslation = document.getElementById('printflow-smart-buyer-translation');
    const replyTranslation = document.getElementById('printflow-smart-reply-translation');
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
        es:['hola','sigue disponible','aún está disponible','aun esta disponible','disponible','ubicacion','ubicación','dónde','donde','baño','bano','todavía','precio','cuánto','cuanto','envío','envio','gracias','interesado'],
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

    const messageCandidates = root => {
      const composer = findComposer();
      if (!composer) return [];
      const composerRect = composer.getBoundingClientRect();
      const leftBound = Math.max(0, composerRect.left - 170);
      const rightBound = Math.min(innerWidth, composerRect.right + 30);
      return [...document.querySelectorAll('[dir="auto"]')].filter(el => {
        const r = visibleRect(el);
        if (!r || el.closest('#printflow-smart-reply') || el.closest('.printflow-inline-translation')) return false;
        if (el.isContentEditable || el.closest('[contenteditable="true"]')) return false;
        if (r.bottom >= composerRect.top + 8 || r.right < leftBound || r.left > rightBound) return false;
        const value = String(el.innerText || '').replace(/\s+/g, ' ').trim();
        if (!value || value.length > 600 || /^\d{1,2}:\d{2}/.test(value)) return false;
        if (/^(send|like|more|search|message sent)$/i.test(value)) return false;
        if ([...el.querySelectorAll('[dir="auto"]')].some(child => String(child.innerText || '').trim())) return false;
        return true;
      }).sort((a,b) => a.getBoundingClientRect().bottom - b.getBoundingClientRect().bottom);
    };

    const conversationSnapshot = () => {
      const composer = findComposer();
      const root = findConversationRoot(composer);
      if (!composer || !root) return null;
      const text = cleanConversationText(root.innerText || '');
      const composerRect = composer.getBoundingClientRect();
      const candidates = messageCandidates(root);
      const incoming = candidates.filter(el => {
        const r = el.getBoundingClientRect();
        return r.left + r.width / 2 < composerRect.left + composerRect.width * 0.5;
      });
      const incomingNode = incoming.length ? incoming[incoming.length - 1] : null;
      const incomingText = incomingNode ? String(incomingNode.innerText || '').replace(/\s+/g, ' ').trim() : '';
      return {composer, root, text, incomingText, key:location.href + '|' + (incomingText || text.slice(-500))};
    };

    const translateEnglish = async value => {
      const sourceText = String(value || '').trim();
      if (!sourceText) return {ok:false, text:'', source:'', error:'No text'};
      try {
        return await window.pywebview.api.translate(sourceText, 'en');
      } catch (error) {
        return {ok:false, text:'', source:'', error:String(error || 'Translation unavailable')};
      }
    };

    const translateVisibleBubbles = async root => {
      for (const node of messageCandidates(root).slice(-12)) {
        const original = String(node.innerText || '').replace(/\s+/g, ' ').trim();
        if (!original || node.dataset.printflowTranslatedText === original) continue;
        node.dataset.printflowTranslatedText = original;
        const result = await translateEnglish(original);
        let caption = node.parentElement ? node.parentElement.querySelector(':scope > .printflow-inline-translation') : null;
        if (!result.ok || !result.text || result.source === 'en' || result.text.toLowerCase() === original.toLowerCase()) {
          if (caption) caption.remove();
          continue;
        }
        if (!caption) {
          caption = document.createElement('div');
          caption.className = 'printflow-inline-translation';
          Object.assign(caption.style, {
            margin:'3px 6px 5px', padding:'3px 7px', maxWidth:'420px',
            borderRadius:'6px', background:'rgba(30,58,138,.35)',
            color:'#bfdbfe', font:'11px Segoe UI,Arial,sans-serif', lineHeight:'1.3'
          });
          node.insertAdjacentElement('afterend', caption);
        }
        caption.textContent = 'English: ' + result.text;
      }
    };

    let translationRun = 0;
    const refreshTranslations = async (snapshot, outgoingText) => {
      const run = ++translationRun;
      if (buyerTranslation) buyerTranslation.textContent = snapshot.incomingText ? 'Translating…' : 'No buyer message detected.';
      if (replyTranslation) replyTranslation.textContent = outgoingText ? 'Translating…' : 'No reply prepared.';
      const results = await Promise.all([
        translateEnglish(snapshot.incomingText),
        translateEnglish(outgoingText)
      ]);
      if (run !== translationRun) return;
      if (buyerTranslation) {
        buyerTranslation.textContent = results[0].ok
          ? results[0].text
          : (snapshot.incomingText ? 'Translation failed: ' + (results[0].error || 'service unavailable') : 'No buyer message detected.');
        buyerTranslation.title = results[0].error || '';
      }
      if (replyTranslation) {
        replyTranslation.textContent = results[1].ok
          ? results[1].text
          : (outgoingText ? 'Translation failed: ' + (results[1].error || 'service unavailable') : 'No reply prepared.');
        replyTranslation.title = results[1].error || '';
      }
      translateVisibleBubbles(snapshot.root);
    };

    const buildEnglishReply = (question, conversationText) => {
      const value = String(question || '').toLowerCase();
      const listingPrice = (String(conversationText || '').match(/\$\s*\d+(?:\.\d{2})?/) || [])[0] || '';
      if (/scam|fraud/.test(value)) {
        return "I understand. This listing is for the 3D-printed item shown, and payment can be handled through the agreed Marketplace method. Let me know if you have a question about the item.";
      }
      if (/where|location|located|pickup|address|bathroom/.test(value)) {
        return "I’m located near Aiken, South Carolina. I can also ship anywhere in the U.S.";
      }
      if (/ship|shipping|deliver|mail|postal|zip code/.test(value)) {
        return "Yes, I can ship anywhere in the U.S. What ZIP code should I use for the shipping quote?";
      }
      if (/price|cost|how much|lowest|offer/.test(value)) {
        return listingPrice
          ? "The listed price is " + listingPrice + ". Would you need shipping or local pickup near Aiken, SC?"
          : "What item and quantity are you interested in? I can confirm the price and shipping.";
      }
      if (/include|come with|tool|battery|charger/.test(value)) {
        return "The sale is for the 3D-printed holder or insert shown. Tools, batteries, and chargers are not included unless the listing specifically says otherwise.";
      }
      if (/color|size|custom|make one|different/.test(value)) {
        return "Yes, I can make custom sizes and colors. Tell me what you need and I’ll confirm the price.";
      }
      if (/available|still have|still for sale|interested|hello|hi\b/.test(value)) {
        return "Hi! Yes, it’s still available. Would you prefer local pickup near Aiken, SC, or shipping?";
      }
      if (/thank|gracias|merci|obrigad|danke|grazie/.test(value)) {
        return "You’re welcome! Let me know if you’d like local pickup or shipping.";
      }
      return "Thanks for reaching out. Could you tell me what you’d like to know about the item?";
    };

    const prepareSmartReply = async () => {
      const snapshot = conversationSnapshot();
      if (!snapshot || !snapshot.text) {
        setSmartStatus('Open a buyer conversation so PrintFlow can prepare a reply.', '#fbbf24');
        return;
      }
      if (!snapshot.incomingText) {
        setSmartStatus('I can see the chat, but not the newest buyer bubble. Try Refresh.', '#fbbf24');
        return;
      }
      setSmartStatus('Reading the buyer’s latest question…');
      if (buyerTranslation) buyerTranslation.textContent = 'Translating…';

      const buyerResult = await translateEnglish(snapshot.incomingText);
      const language = (buyerResult.source || detectLanguage(snapshot.incomingText) || 'en').toLowerCase();
      const englishQuestion = buyerResult.ok ? buyerResult.text : snapshot.incomingText;
      const englishReply = buildEnglishReply(englishQuestion, snapshot.text);
      let outgoingReply = englishReply;

      if (language !== 'en') {
        try {
          const translatedReply = await window.pywebview.api.translate(englishReply, language);
          if (translatedReply && translatedReply.ok && translatedReply.text) {
            outgoingReply = translatedReply.text;
          } else if (replies[language]) {
            outgoingReply = replies[language];
          }
        } catch (_) {
          if (replies[language]) outgoingReply = replies[language];
        }
      }

      if (languageBox) languageBox.textContent = languageNames[language] || language.toUpperCase();
      if (buyerTranslation) {
        buyerTranslation.textContent = buyerResult.ok
          ? buyerResult.text
          : 'Translation failed: ' + (buyerResult.error || 'service unavailable');
        buyerTranslation.title = buyerResult.error || '';
      }
      if (replyTranslation) replyTranslation.textContent = englishReply;
      if (replyBox) {
        replyBox.value = outgoingReply;
        replyBox.dataset.conversationKey = snapshot.key;
      }
      translateVisibleBubbles(snapshot.root);

      const sentKey = localStorage.getItem('printflow-smart-last-sent') || '';
      if (sentKey === snapshot.key) {
        setSmartStatus('A smart reply was already sent for this visible conversation.', '#fbbf24');
      } else {
        setSmartStatus('Direct reply prepared from the buyer’s latest message. Review or edit it, then send.');
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
    let replyTranslateTimer = 0;
    replyBox.addEventListener('input', () => {
      clearTimeout(replyTranslateTimer);
      replyTranslateTimer = setTimeout(() => {
        const snapshot = conversationSnapshot();
        if (snapshot) refreshTranslations(snapshot, replyBox.value);
      }, 450);
    });
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
      } else if (snapshot) {
        translateVisibleBubbles(snapshot.root);
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

import base64
import ctypes
import hashlib
import json
import os
import re
import ssl
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
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
    AI_CACHE_VERSION = "marketplace-v2"

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


    @staticmethod
    def _unprotect_secret(value):
        value = str(value or "").strip()
        if not value:
            return ""
        if value.startswith("local:"):
            try:
                return base64.b64decode(value[6:]).decode("utf-8")
            except Exception:
                return ""
        if not value.startswith("dpapi:") or os.name != "nt":
            return ""
        try:
            encrypted = base64.b64decode(value[6:])

            class DATA_BLOB(ctypes.Structure):
                _fields_ = [
                    ("cbData", ctypes.c_uint),
                    ("pbData", ctypes.POINTER(ctypes.c_byte)),
                ]

            buf = ctypes.create_string_buffer(encrypted)
            in_blob = DATA_BLOB(
                len(encrypted), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte))
            )
            out_blob = DATA_BLOB()
            ok = ctypes.windll.crypt32.CryptUnprotectData(
                ctypes.byref(in_blob), None, None, None, None, 0x1,
                ctypes.byref(out_blob),
            )
            if not ok:
                return ""
            try:
                raw = ctypes.string_at(out_blob.pbData, out_blob.cbData)
            finally:
                ctypes.windll.kernel32.LocalFree(out_blob.pbData)
            return raw.decode("utf-8")
        except Exception:
            return ""

    def _openai_config(self):
        db_path = data_dir() / "printflow.db"
        if not db_path.exists():
            return "", "gpt-5.4-mini"
        try:
            with sqlite3.connect(db_path) as conn:
                rows = conn.execute(
                    "SELECT key, value FROM settings WHERE key IN (?, ?)",
                    ("openai_api_key_enc", "openai_model"),
                ).fetchall()
            settings = {str(key): str(value or "") for key, value in rows}
            key = self._unprotect_secret(settings.get("openai_api_key_enc", ""))
            model = settings.get("openai_model", "").strip() or "gpt-5.4-mini"
            return key, model
        except Exception:
            return "", "gpt-5.4-mini"

    @staticmethod
    def _openai_output_text(data):
        pieces = []
        for item in (data or {}).get("output", []) or []:
            if item.get("type") != "message":
                continue
            for part in item.get("content", []) or []:
                if part.get("type") == "output_text" and part.get("text"):
                    pieces.append(part["text"])
        return "\n".join(pieces).strip()

    @staticmethod
    def _ai_cache_key(model, conversation_id, cleaned):
        payload = {
            "version": Bridge.AI_CACHE_VERSION,
            "model": str(model or ""),
            "conversation_id": str(conversation_id or ""),
            "messages": cleaned,
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _read_ai_cache(cache_key):
        db_path = data_dir() / "printflow.db"
        if not db_path.exists():
            return None
        try:
            with sqlite3.connect(db_path, timeout=10) as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS messenger_ai_cache (
                        cache_key TEXT PRIMARY KEY,
                        result_json TEXT NOT NULL,
                        model TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        last_used_at TEXT NOT NULL
                    )
                    """
                )
                row = conn.execute(
                    "SELECT result_json FROM messenger_ai_cache WHERE cache_key=?",
                    (cache_key,),
                ).fetchone()
                if not row:
                    return None
                conn.execute(
                    "UPDATE messenger_ai_cache SET last_used_at=? WHERE cache_key=?",
                    (datetime.now().isoformat(timespec="seconds"), cache_key),
                )
            parsed = json.loads(row[0])
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None

    @staticmethod
    def _write_ai_cache(cache_key, model, result):
        db_path = data_dir() / "printflow.db"
        now = datetime.now().isoformat(timespec="seconds")
        try:
            with sqlite3.connect(db_path, timeout=10) as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS messenger_ai_cache (
                        cache_key TEXT PRIMARY KEY,
                        result_json TEXT NOT NULL,
                        model TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        last_used_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO messenger_ai_cache
                        (cache_key, result_json, model, created_at, last_used_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(cache_key) DO UPDATE SET
                        result_json=excluded.result_json,
                        model=excluded.model,
                        created_at=excluded.created_at,
                        last_used_at=excluded.last_used_at
                    """,
                    (
                        cache_key,
                        json.dumps(result, ensure_ascii=False),
                        str(model or ""),
                        now,
                        now,
                    ),
                )
                # Keep plenty of history while preventing an unbounded database.
                conn.execute(
                    """
                    DELETE FROM messenger_ai_cache
                    WHERE cache_key IN (
                        SELECT cache_key FROM messenger_ai_cache
                        ORDER BY last_used_at DESC
                        LIMIT -1 OFFSET 1000
                    )
                    """
                )
        except Exception:
            # Caching is a cost optimization. A cache write failure must not stop
            # the user from translating or replying.
            pass

    def ai_analyze(
        self, messages, listing_context="", conversation_id="", force_refresh=False
    ):
        api_key, model = self._openai_config()
        cleaned = []
        total_chars = 0
        for index, item in enumerate(messages or []):
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            role = "seller" if str(item.get("role") or "") == "seller" else "buyer"
            if not text:
                continue
            text = text[:1000]
            if total_chars + len(text) > 18000:
                break
            cleaned.append({"index": int(item.get("index", index)), "role": role, "text": text})
            total_chars += len(text)
        if not cleaned:
            return {"ok": False, "error": "No Marketplace messages were detected."}

        cache_key = self._ai_cache_key(model, conversation_id, cleaned)
        if not bool(force_refresh):
            cached = self._read_ai_cache(cache_key)
            if cached:
                return {
                    "ok": True,
                    "data": cached,
                    "model": model,
                    "cached": True,
                }

        if not api_key:
            return {
                "ok": False,
                "error": "No OpenAI API key is configured in PrintFlow Settings.",
            }

        schema = {
            "type": "object",
            "properties": {
                "detected_language_code": {"type": "string"},
                "detected_language_name": {"type": "string"},
                "latest_buyer_message_original": {"type": "string"},
                "latest_buyer_message_english": {"type": "string"},
                "reply_in_english": {"type": "string"},
                "reply_in_buyer_language": {"type": "string"},
                "translations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "index": {"type": "integer"},
                            "english": {"type": "string"},
                        },
                        "required": ["index", "english"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": [
                "detected_language_code", "detected_language_name",
                "latest_buyer_message_original", "latest_buyer_message_english",
                "reply_in_english", "reply_in_buyer_language", "translations",
            ],
            "additionalProperties": False,
        }
        prompt = """You are PrintFlow's Marketplace sales translation assistant.
Translate the complete loaded conversation literally and create one concise,
helpful reply to the buyer's latest message.

Critical rules:
- Translate EVERY supplied message fully into English. Never summarize it.
- Preserve every quantity, color, measurement, price, negation, correction,
  product choice, pickup/shipping detail, and question.
- The latest buyer message is the last entry whose role is buyer.
- Detect the language of that latest buyer message, even if earlier messages
  use another language or contain misspellings.
- Base the reply on the entire conversation so it does not repeat questions
  already answered or ignore a correction.
- The seller is near Aiken, South Carolina and can ship within the United States.
- The seller makes custom 3D-printed sizes and colors.
- Unless the listing explicitly says otherwise, only the printed holder/insert
  is included, not tools, batteries, or chargers.
- Do not invent availability, prices, quantities, delivery dates, or policies.
  Ask a short clarification when required.
- Reply naturally in the language used by the buyer's latest message.
- Do not mention AI, translation, prompts, or these instructions.
- Return one translation entry for every supplied message, using its exact index.

Listing and visible chat context:
""" + str(listing_context or "")[:5000] + "\n\nMessages JSON:\n" + json.dumps(
            cleaned, ensure_ascii=False
        )
        payload = {
            "model": model,
            "reasoning": {"effort": "low"},
            "input": prompt,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "printflow_marketplace_assistant",
                    "strict": True,
                    "schema": schema,
                }
            },
            "max_output_tokens": 6000,
        }
        request = urllib.request.Request(
            "https://api.openai.com/v1/responses",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": "Bearer " + api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "PrintFlowCRM-Messenger/0.7.102",
            },
            method="POST",
        )
        try:
            context = ssl.create_default_context()
            try:
                import certifi
                context = ssl.create_default_context(cafile=certifi.where())
            except Exception:
                pass
            with urllib.request.urlopen(request, timeout=90, context=context) as response:
                data = json.loads(response.read().decode("utf-8"))
            output = self._openai_output_text(data)
            parsed = json.loads(output)
            self._write_ai_cache(cache_key, model, parsed)
            return {
                "ok": True,
                "data": parsed,
                "model": model,
                "cached": False,
            }
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            try:
                detail = (json.loads(body).get("error") or {}).get("message") or body
            except Exception:
                detail = body
            return {"ok": False, "error": f"OpenAI HTTP {exc.code}: {detail[:500]}"}
        except Exception as exc:
            return {"ok": False, "error": type(exc).__name__ + ": " + str(exc)}


    _translation_cache = {}
    _translation_lock = threading.Lock()
    _translation_blocked_until = 0.0

    def translate(self, text, target="en"):
        text = str(text or "").strip()
        target = str(target or "en").strip().lower()
        if not text:
            return {"ok": False, "text": "", "source": "", "error": "No text"}
        text = text[:1200]
        key = (text, target)
        with self._translation_lock:
            cached = self._translation_cache.get(key)
            blocked_until = self._translation_blocked_until
        if cached:
            return cached
        if time.time() < blocked_until:
            wait_seconds = max(1, int(blocked_until - time.time()))
            return {
                "ok": False, "text": "", "source": "",
                "error": f"Google rate-limit cooldown ({wait_seconds}s remaining)",
            }

        normalized = " ".join(re.sub(r"[^\w\s]", " ", text.lower(), flags=re.UNICODE).split())
        offline_phrases = {
            "hola": ("Hello", "es"),
            "hola sigue disponible": ("Hi, is it still available?", "es"),
            "aún está disponible": ("Is it still available?", "es"),
            "aun esta disponible": ("Is it still available?", "es"),
            "sigue disponible": ("Is it still available?", "es"),
            "cuál es la ubicación": ("What is the location?", "es"),
            "cual es la ubicacion": ("What is the location?", "es"),
            "dónde está baño": ("Where is the bathroom?", "es"),
            "donde esta bano": ("Where is the bathroom?", "es"),
            "estafa es lo siento": ("It is a scam, sorry.", "es"),
            "ist das noch verfügbar": ("Is this still available?", "de"),
            "ist das noch verfugbar": ("Is this still available?", "de"),
            "noch verfügbar": ("Still available?", "de"),
            "noch verfugbar": ("Still available?", "de"),
            "est-ce toujours disponible": ("Is this still available?", "fr"),
            "ainda está disponível": ("Is this still available?", "pt"),
            "ainda esta disponivel": ("Is this still available?", "pt"),
            "è ancora disponibile": ("Is this still available?", "it"),
            "e ancora disponibile": ("Is this still available?", "it"),
            "kannst du das in blau machen": ("Can you make that in blue?", "de"),
            "kanst du das in blau machen": ("Can you make that in blue?", "de"),
            "kaanst du das in blau machen": ("Can you make that in blue?", "de"),
            "kannst du es in blau machen": ("Can you make it in blue?", "de"),
            "ich möchte zwei eins in rot und eins blau": ("I would like two. One in red and one in blue.", "de"),
            "ich mochte zwei eins in rot und eins blau": ("I would like two. One in red and one in blue.", "de"),
            "ich möchte zwei eins in rot und eins in blau": ("I would like two. One in red and one in blue.", "de"),
        }
        if target == "en" and normalized in offline_phrases:
            translated, source = offline_phrases[normalized]
            result = {"ok": True, "text": translated, "source": source, "error": ""}
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
            if isinstance(exc, urllib.error.HTTPError) and exc.code == 429:
                with self._translation_lock:
                    self._translation_blocked_until = time.time() + 90
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
        de:['hallo','verfügbar','verfugbar','ist das','kannst du','kanst du','kaanst du','ich möchte','ich mochte','zwei','eins in','in rot','in blau','blau','rot','machen','preis','versand','danke','interessiert'],
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
      const centerLine = composerRect.left + composerRect.width * 0.5;
      const messages = candidates.slice(-60).map((node, index) => {
        const r = node.getBoundingClientRect();
        return {
          index, node,
          role:(r.left + r.width / 2 < centerLine ? 'buyer' : 'seller'),
          text:String(node.innerText || '').replace(/\s+/g, ' ').trim()
        };
      }).filter(item => item.text);
      const signature = messages.map(item => item.role + ':' + item.text).join('|');
      return {
        composer, root, text, messages, incomingNode, incomingText,
        key:location.href + '|' + (signature || incomingText || text.slice(-500))
      };
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

    const setInlineCaption = (node, englishText) => {
      if (!node || !englishText) return;
      const original = String(node.innerText || '').replace(/\s+/g, ' ').trim();
      if (!original || englishText.toLowerCase() === original.toLowerCase()) return;
      let caption = node.parentElement ? node.parentElement.querySelector(':scope > .printflow-inline-translation') : null;
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
      caption.textContent = 'English: ' + englishText;
      node.dataset.printflowTranslatedText = original;
    };

    const translationQueue = [];
    let translationWorkerBusy = false;
    let smartPreparing = false;
    let aiTranslationActive = false;
    let aiFallbackActive = false;
    let aiRequestSerial = 0;

    const queueConversationTranslations = (root, skipNode=null) => {
      if (aiTranslationActive || smartPreparing) return;
      const now = Date.now();
      for (const node of messageCandidates(root)) {
        if (node === skipNode) continue;
        const original = String(node.innerText || '').replace(/\s+/g, ' ').trim();
        const retryAt = Number(node.dataset.printflowTranslationRetry || 0);
        if (!original || node.dataset.printflowTranslatedText === original ||
            node.dataset.printflowTranslationQueued === original || retryAt > now) continue;
        node.dataset.printflowTranslationQueued = original;
        translationQueue.push({node, original});
      }
    };

    const processTranslationQueue = async () => {
      if (translationWorkerBusy || smartPreparing || aiTranslationActive || !translationQueue.length) return;
      translationWorkerBusy = true;
      const item = translationQueue.shift();
      const node = item.node;
      const original = item.original;
      try {
        if (!node || !node.isConnected || String(node.innerText || '').replace(/\s+/g, ' ').trim() !== original) return;
        const result = await translateEnglish(original);
        if (result.ok) {
          if (result.source !== 'en' && result.text) setInlineCaption(node, result.text);
          else node.dataset.printflowTranslatedText = original;
          delete node.dataset.printflowTranslationRetry;
        } else {
          const language = detectLanguage(original);
          const intent = detectIntent(original);
          if (language !== 'en' && intent !== 'unknown') {
            setInlineCaption(node, intentEnglish(intent) + ' (offline interpretation)');
            delete node.dataset.printflowTranslatedText;
          }
          const cooldown = /429|cooldown|rate.limit/i.test(result.error || '') ? 95000 : 12000;
          node.dataset.printflowTranslationRetry = String(Date.now() + cooldown);
        }
      } finally {
        if (node) delete node.dataset.printflowTranslationQueued;
        translationWorkerBusy = false;
      }
    };

    setInterval(processTranslationQueue, 1800);


    let translationRun = 0;
    const refreshTranslations = async (snapshot, outgoingText) => {
      const run = ++translationRun;
      if (replyTranslation) replyTranslation.textContent = outgoingText ? 'Translating edited reply…' : 'No reply prepared.';
      const result = await translateEnglish(outgoingText);
      if (run !== translationRun) return;
      if (replyTranslation) {
        replyTranslation.textContent = result.ok
          ? result.text
          : (outgoingText ? 'Translation failed: ' + (result.error || 'service unavailable') : 'No reply prepared.');
        replyTranslation.title = result.error || '';
      }
    };

    const detectIntent = question => {
      const value = String(question || '').toLowerCase();
      if (/scam|fraud|estafa|betrug|arnaque|fraude|truffa/.test(value)) return 'scam';
      const hasTwo = /\b(two|2|both|pair|dos|zwei|beide|deux|dois|due)\b/.test(value);
      const hasRed = /\b(red|rot|rojo|rouge|vermelho|rosso)\b/.test(value);
      const hasBlue = /\b(blue|blau|azul|bleu|blu)\b/.test(value);
      if (hasTwo && hasRed && hasBlue) return 'multi_color';
      if (hasTwo || /you have posted|your listings|these two/.test(value)) return 'multi';
      if (hasBlue) return 'blue';
      if (/where|location|located|pickup|address|bathroom|ubicaci[oó]n|d[oó]nde|recoger|wo\b|standort|abholen|adresse|o[uù]|emplacement|retirer|localiza[cç][aã]o|retirada|dove|posizione|ritiro/.test(value)) return 'location';
      if (/ship|shipping|deliver|mail|postal|zip code|env[ií]o|enviar|versand|liefern|lieferung|livraison|exp[eé]dier|spedizione|spedire/.test(value)) return 'shipping';
      if (/price|cost|how much|lowest|offer|precio|cu[aá]nto|preis|kosten|combien|prix|pre[cç]o|quanto|prezzo|offerta/.test(value)) return 'price';
      if (/include|come with|tool|battery|charger|incluye|herramienta|bater[ií]a|cargador|enthalten|werkzeug|akku|ladeger[aä]t|compris|outil|batterie|chargeur|inclui|ferramenta|carregador|incluso|utensile/.test(value)) return 'included';
      if (/color|size|custom|make one|different|tama[nñ]o|personaliz|farbe|gr[oö][sß]e|anfertigen|couleur|taille|personnalis|cor\b|tamanho|personalizado|colore|misura/.test(value)) return 'custom';
      if (/available|still have|still for sale|interested|hello|hi\b|disponible|verf[uü]gbar|bonjour|ol[aá]|ciao/.test(value)) return 'availability';
      if (/thank|gracias|merci|obrigad|danke|grazie/.test(value)) return 'thanks';
      return 'unknown';
    };

    const intentEnglish = intent => ({
      scam:'The buyer is concerned this may be a scam.',
      blue:'The buyer is asking whether you can make it in blue.',
      multi_color:'The buyer wants two items: one red and one blue.',
      multi:'The buyer is interested in two items.',
      location:'The buyer is asking where you are located.',
      shipping:'The buyer is asking about shipping.',
      price:'The buyer is asking about the price.',
      included:'The buyer is asking what is included.',
      custom:'The buyer is asking about a custom size or color.',
      availability:'The buyer is asking whether the item is still available.',
      thanks:'The buyer is thanking you.',
      unknown:'The exact meaning could not be translated.'
    }[intent] || 'The exact meaning could not be translated.');

    const localizedReply = (intent, language, listingPrice) => {
      const price = listingPrice || '';
      const table = {
        en:{
          availability:"Hi! Yes, it’s still available. Would you prefer local pickup near Aiken, SC, or shipping?",
          blue:"Yes, I can make it in blue. Would you prefer local pickup near Aiken, SC, or shipping?",
          multi_color:"Perfect—I can make two: one red and one blue. Would you prefer local pickup near Aiken, SC, or shipping?",
          location:"I’m located near Aiken, South Carolina. I can also ship anywhere in the U.S.",
          shipping:"Yes, I can ship anywhere in the U.S. What ZIP code should I use for the shipping quote?",
          price:price ? "The listed price is "+price+". Would you need shipping or local pickup near Aiken, SC?" : "What item and quantity are you interested in? I can confirm the price and shipping.",
          multi:"Great! Which two items would you like, and do you need shipping or local pickup near Aiken, SC?",
          included:"The sale is for the 3D-printed holder or insert shown. Tools, batteries, and chargers are not included unless the listing specifically says otherwise.",
          custom:"Yes, I can make custom sizes and colors. Tell me what you need and I’ll confirm the price.",
          thanks:"You’re welcome! Let me know if you’d like local pickup or shipping.",
          scam:"I understand. This listing is for the 3D-printed item shown. Let me know what questions you have about the item.",
          unknown:"Thanks for reaching out. Could you tell me what you’d like to know about the item?"
        },
        es:{
          availability:"¡Hola! Sí, todavía está disponible. ¿Prefieres recogerlo cerca de Aiken, Carolina del Sur, o necesitas envío?",
          blue:"Sí, puedo hacerlo en azul. ¿Prefieres recogerlo cerca de Aiken, Carolina del Sur, o necesitas envío?",
          multi_color:"Perfecto, puedo hacer dos: uno rojo y uno azul. ¿Prefieres recogerlos cerca de Aiken, Carolina del Sur, o necesitas envío?",
          location:"Estoy cerca de Aiken, Carolina del Sur. También puedo enviarlo a cualquier parte de EE. UU.",
          shipping:"Sí, puedo enviarlo a cualquier parte de EE. UU. ¿Qué código postal debo usar para calcular el envío?",
          price:price ? "El precio publicado es "+price+". ¿Necesitas envío o recogida cerca de Aiken, Carolina del Sur?" : "¿Qué artículo y cantidad te interesan? Puedo confirmar el precio y el envío.",
          multi:"¡Perfecto! ¿Cuáles dos artículos quieres? ¿Necesitas envío o recogida cerca de Aiken, Carolina del Sur?",
          included:"La venta incluye solamente el soporte o inserto impreso en 3D. Las herramientas, baterías y cargadores no están incluidos.",
          custom:"Sí, puedo hacer tamaños y colores personalizados. Dime qué necesitas y confirmaré el precio.",
          thanks:"¡De nada! Avísame si prefieres recogida local o envío.",
          scam:"Entiendo. Este anuncio es para el artículo impreso en 3D que se muestra. Dime qué preguntas tienes.",
          unknown:"Gracias por escribir. ¿Qué te gustaría saber sobre el artículo?"
        },
        de:{
          availability:"Hallo! Ja, der Artikel ist noch verfügbar. Möchten Sie ihn in der Nähe von Aiken, South Carolina, abholen oder benötigen Sie Versand?",
          blue:"Ja, ich kann es in Blau anfertigen. Möchten Sie es in der Nähe von Aiken, South Carolina, abholen oder benötigen Sie Versand?",
          multi_color:"Perfekt, ich kann zwei anfertigen: einen in Rot und einen in Blau. Möchten Sie sie in der Nähe von Aiken, South Carolina, abholen oder benötigen Sie Versand?",
          location:"Ich befinde mich in der Nähe von Aiken, South Carolina. Ich kann auch überall in den USA versenden.",
          shipping:"Ja, ich kann überall in den USA versenden. Welche Postleitzahl soll ich für die Versandkosten verwenden?",
          price:price ? "Der angegebene Preis beträgt "+price+". Benötigen Sie Versand oder Abholung in der Nähe von Aiken, SC?" : "Für welchen Artikel und welche Menge interessieren Sie sich? Ich kann Preis und Versand bestätigen.",
          multi:"Super! Welche zwei Artikel möchten Sie? Benötigen Sie Versand oder Abholung in der Nähe von Aiken, SC?",
          included:"Der Verkauf umfasst nur den abgebildeten 3D-gedruckten Halter oder Einsatz. Werkzeuge, Akkus und Ladegeräte sind nicht enthalten.",
          custom:"Ja, ich kann individuelle Größen und Farben anfertigen. Sagen Sie mir, was Sie benötigen, dann bestätige ich den Preis.",
          thanks:"Gern geschehen! Sagen Sie mir, ob Sie Abholung oder Versand wünschen.",
          scam:"Ich verstehe. Dieses Angebot gilt für den abgebildeten 3D-gedruckten Artikel. Sagen Sie mir, welche Fragen Sie haben.",
          unknown:"Vielen Dank für Ihre Nachricht. Was möchten Sie über den Artikel wissen?"
        },
        fr:{
          availability:"Bonjour ! Oui, l’article est toujours disponible. Préférez-vous le récupérer près d’Aiken, en Caroline du Sud, ou le faire expédier ?",
          blue:"Oui, je peux le faire en bleu. Préférez-vous le retrait près d’Aiken ou la livraison ?",
          multi_color:"Parfait, je peux en faire deux : un rouge et un bleu. Préférez-vous le retrait près d’Aiken ou la livraison ?",
          location:"Je suis près d’Aiken, en Caroline du Sud. Je peux aussi expédier partout aux États-Unis.",
          shipping:"Oui, je peux expédier partout aux États-Unis. Quel code postal dois-je utiliser pour calculer les frais d’envoi ?",
          price:price ? "Le prix affiché est de "+price+". Avez-vous besoin d’une livraison ou d’un retrait près d’Aiken ?" : "Quel article et quelle quantité vous intéressent ? Je peux confirmer le prix et la livraison.",
          multi:"Parfait ! Quels sont les deux articles que vous souhaitez ? Avez-vous besoin d’une livraison ou d’un retrait près d’Aiken ?",
          included:"La vente comprend uniquement le support ou l’insert imprimé en 3D. Les outils, batteries et chargeurs ne sont pas inclus.",
          custom:"Oui, je peux réaliser des tailles et couleurs personnalisées. Dites-moi ce qu’il vous faut et je confirmerai le prix.",
          thanks:"Avec plaisir ! Dites-moi si vous préférez le retrait local ou la livraison.",
          scam:"Je comprends. Cette annonce concerne l’article imprimé en 3D présenté. Dites-moi quelles questions vous avez.",
          unknown:"Merci pour votre message. Que souhaitez-vous savoir sur l’article ?"
        },
        pt:{
          availability:"Olá! Sim, o item ainda está disponível. Você prefere retirar perto de Aiken, Carolina do Sul, ou precisa de envio?",
          blue:"Sim, posso fazê-lo em azul. Você prefere retirar perto de Aiken ou precisa de envio?",
          multi_color:"Perfeito, posso fazer dois: um vermelho e um azul. Você prefere retirar perto de Aiken ou precisa de envio?",
          location:"Estou perto de Aiken, Carolina do Sul. Também posso enviar para qualquer lugar dos EUA.",
          shipping:"Sim, posso enviar para qualquer lugar dos EUA. Qual CEP devo usar para calcular o frete?",
          price:price ? "O preço anunciado é "+price+". Você precisa de envio ou retirada perto de Aiken?" : "Qual item e quantidade você deseja? Posso confirmar o preço e o envio.",
          multi:"Ótimo! Quais dois itens você quer? Você precisa de envio ou retirada perto de Aiken?",
          included:"A venda inclui apenas o suporte ou encaixe impresso em 3D. Ferramentas, baterias e carregadores não estão incluídos.",
          custom:"Sim, posso fazer tamanhos e cores personalizados. Diga o que precisa e confirmarei o preço.",
          thanks:"De nada! Avise se prefere retirada local ou envio.",
          scam:"Entendo. Este anúncio é para o item impresso em 3D mostrado. Diga quais dúvidas você tem.",
          unknown:"Obrigado pela mensagem. O que você gostaria de saber sobre o item?"
        },
        it:{
          availability:"Ciao! Sì, l’articolo è ancora disponibile. Preferisci il ritiro vicino ad Aiken, South Carolina, oppure la spedizione?",
          blue:"Sì, posso realizzarlo in blu. Preferisci il ritiro vicino ad Aiken oppure la spedizione?",
          multi_color:"Perfetto, posso realizzarne due: uno rosso e uno blu. Preferisci il ritiro vicino ad Aiken oppure la spedizione?",
          location:"Mi trovo vicino ad Aiken, South Carolina. Posso anche spedire ovunque negli Stati Uniti.",
          shipping:"Sì, posso spedire ovunque negli Stati Uniti. Quale CAP devo usare per calcolare la spedizione?",
          price:price ? "Il prezzo indicato è "+price+". Ti serve la spedizione o il ritiro vicino ad Aiken?" : "Quale articolo e quantità ti interessano? Posso confermare prezzo e spedizione.",
          multi:"Perfetto! Quali due articoli desideri? Ti serve la spedizione o il ritiro vicino ad Aiken?",
          included:"La vendita comprende solo il supporto o inserto stampato in 3D. Utensili, batterie e caricabatterie non sono inclusi.",
          custom:"Sì, posso realizzare misure e colori personalizzati. Dimmi cosa ti serve e confermerò il prezzo.",
          thanks:"Prego! Fammi sapere se preferisci il ritiro locale o la spedizione.",
          scam:"Capisco. Questo annuncio riguarda l’articolo stampato in 3D mostrato. Dimmi quali domande hai.",
          unknown:"Grazie per il messaggio. Cosa vorresti sapere sull’articolo?"
        }
      };
      const languageTable = table[language] || table.en;
      return languageTable[intent] || languageTable.unknown;
    };

    const prepareSmartReply = async (forceRefresh = false) => {
      const snapshot = conversationSnapshot();
      if (!snapshot || !snapshot.text) {
        setSmartStatus('Open a buyer conversation so PrintFlow can prepare a reply.', '#fbbf24');
        return;
      }
      if (!snapshot.incomingText) {
        setSmartStatus('I can see the chat, but not the newest buyer bubble. Try Refresh.', '#fbbf24');
        return;
      }
      const requestSerial = ++aiRequestSerial;
      const consentKey = 'printflow-openai-chat-consent-v1';
      let aiConsent = localStorage.getItem(consentKey) || '';
      if (!aiConsent) {
        const allowed = window.confirm(
          'Enable full AI chat translation?\n\n' +
          'PrintFlow will send the loaded Marketplace messages and listing context to OpenAI for translation and reply preparation. ' +
          'This can include buyer names displayed with messages. Conversation links are not sent intentionally. ' +
          'This uses your configured OpenAI API key and API credit.'
        );
        aiConsent = allowed ? 'allowed' : 'denied';
        localStorage.setItem(consentKey, aiConsent);
      }

      if (aiConsent === 'allowed') {
        setSmartStatus('OpenAI is translating the full conversation…');
        if (buyerTranslation) buyerTranslation.textContent = 'Translating the complete message…';
        if (replyTranslation) replyTranslation.textContent = 'Preparing a conversation-aware reply…';
        smartPreparing = true;
        aiTranslationActive = false;
        aiFallbackActive = false;
        translationQueue.length = 0;
        let aiResult = null;
        try {
          const safeMessages = (snapshot.messages || []).map((item, index) => ({
            index, role:item.role, text:item.text
          }));
          const listingContext = document.title + '\n' + String(snapshot.text || '').slice(0, 1600);
          aiResult = await window.pywebview.api.ai_analyze(
            safeMessages, listingContext, location.href, Boolean(forceRefresh)
          );
        } catch (error) {
          aiResult = {ok:false, error:String(error || 'OpenAI request failed')};
        }
        smartPreparing = false;

        const currentSnapshot = conversationSnapshot();
        if (requestSerial !== aiRequestSerial || !currentSnapshot || currentSnapshot.key !== snapshot.key) return;

        if (aiResult && aiResult.ok && aiResult.data) {
          const data = aiResult.data;
          aiTranslationActive = true;
          aiFallbackActive = false;
          translationQueue.length = 0;
          if (languageBox) languageBox.textContent = data.detected_language_name || String(data.detected_language_code || '').toUpperCase() || 'Detected';
          if (buyerTranslation) {
            buyerTranslation.textContent = data.latest_buyer_message_english || 'No buyer message translation returned.';
            buyerTranslation.title = 'Full translation by OpenAI';
          }
          if (replyTranslation) replyTranslation.textContent = data.reply_in_english || '';
          if (replyBox) {
            replyBox.value = data.reply_in_buyer_language || data.reply_in_english || '';
            replyBox.dataset.conversationKey = snapshot.key;
          }

          const translations = Array.isArray(data.translations) ? data.translations : [];
          for (const translation of translations) {
            const item = (snapshot.messages || [])[Number(translation.index)];
            if (!item || !item.node || !item.node.isConnected) continue;
            const english = String(translation.english || '').trim();
            const original = String(item.text || '').trim();
            const oldCaption = item.node.parentElement
              ? item.node.parentElement.querySelector(':scope > .printflow-inline-translation')
              : null;
            if (!english || english.toLowerCase() === original.toLowerCase()) {
              if (oldCaption) oldCaption.remove();
              item.node.dataset.printflowTranslatedText = original;
            } else {
              setInlineCaption(item.node, english);
            }
          }

          const sentKey = localStorage.getItem('printflow-smart-last-sent') || '';
          if (sentKey === snapshot.key) {
            setSmartStatus('A reply was already sent for this visible conversation.', '#fbbf24');
          } else if (aiResult.cached) {
            setSmartStatus('Loaded saved translation and reply — no OpenAI charge.', '#86efac');
          } else {
            setSmartStatus('Full chat translated and saved locally for free reuse.', '#86efac');
          }
          return;
        }
        aiTranslationActive = false;
        aiFallbackActive = true;
        setSmartStatus(
          'OpenAI unavailable: ' + ((aiResult && aiResult.error) || 'unknown error') + '. Using fallback.',
          '#fbbf24'
        );
      }

      setSmartStatus('Reading the buyer’s latest question with fallback translation…');
      if (buyerTranslation) buyerTranslation.textContent = 'Translating…';

      smartPreparing = true;
      const detectedLanguage = detectLanguage(snapshot.incomingText) || 'en';
      const buyerResult = await translateEnglish(snapshot.incomingText);
      smartPreparing = false;
      const language = (
        detectedLanguage !== 'en' ? detectedLanguage : (buyerResult.source || 'en')
      ).toLowerCase();
      const intent = detectIntent(snapshot.incomingText + ' ' + (buyerResult.ok ? buyerResult.text : ''));
      const listingPrice = (String(snapshot.text || '').match(/\$\s*\d+(?:\.\d{2})?/) || [])[0] || '';
      const englishMeaning = buyerResult.ok ? buyerResult.text : intentEnglish(intent);
      const englishReply = localizedReply(intent, 'en', listingPrice);
      const outgoingReply = localizedReply(intent, language, listingPrice);

      if (languageBox) languageBox.textContent = languageNames[language] || language.toUpperCase();
      if (buyerTranslation) {
        buyerTranslation.textContent = buyerResult.ok ? buyerResult.text : englishMeaning + ' (offline interpretation)';
        buyerTranslation.title = buyerResult.error || '';
      }
      if (replyTranslation) replyTranslation.textContent = englishReply;
      if (replyBox) {
        replyBox.value = outgoingReply;
        replyBox.dataset.conversationKey = snapshot.key;
      }
      if (snapshot.incomingNode) {
        if (language !== 'en') setInlineCaption(snapshot.incomingNode, englishMeaning);
        else snapshot.incomingNode.dataset.printflowTranslatedText = snapshot.incomingText;
        if (!buyerResult.ok) {
          delete snapshot.incomingNode.dataset.printflowTranslatedText;
          const cooldown = /429|cooldown|rate.limit/i.test(buyerResult.error || '') ? 95000 : 12000;
          snapshot.incomingNode.dataset.printflowTranslationRetry = String(Date.now() + cooldown);
        }
      }
      queueConversationTranslations(snapshot.root, snapshot.incomingNode);

      const sentKey = localStorage.getItem('printflow-smart-last-sent') || '';
      if (sentKey === snapshot.key) {
        setSmartStatus('A smart reply was already sent for this visible conversation.', '#fbbf24');
      } else if (!buyerResult.ok) {
        setSmartStatus('Google is unavailable, so PrintFlow used offline language and intent handling.', '#fbbf24');
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
    document.getElementById('printflow-smart-refresh').onclick = () => prepareSmartReply(true);
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
    let conversationChangeTimer = 0;
    const watchConversation = () => {
      if (!document.getElementById('printflow-smart-reply')) return;
      const snapshot = conversationSnapshot();
      const key = snapshot ? snapshot.key : '';
      if (key && key !== lastConversation) {
        lastConversation = key;
        aiTranslationActive = false;
        clearTimeout(conversationChangeTimer);
        conversationChangeTimer = setTimeout(() => {
          const stableSnapshot = conversationSnapshot();
          if (stableSnapshot && stableSnapshot.key === lastConversation) prepareSmartReply();
        }, 700);
      }
      const consent = localStorage.getItem('printflow-openai-chat-consent-v1') || '';
      if (snapshot && !aiTranslationActive && (consent === 'denied' || aiFallbackActive)) {
        queueConversationTranslations(snapshot.root, snapshot.incomingNode);
      }
    };
    setInterval(watchConversation, 650);
    setTimeout(watchConversation, 350);
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

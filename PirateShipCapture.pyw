import ctypes
import json
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import webview


def data_dir():
    base=os.getenv("LOCALAPPDATA")
    root=(Path(base)/"PrintFlowCRM") if base else (Path.home()/".printflowcrm")
    root.mkdir(parents=True,exist_ok=True)
    return root


REQUEST_FILE=data_dir()/"pirateship_scan_request.json"
RESULT_FILE=data_dir()/"pirateship_scan_result.json"
STORAGE_DIR=data_dir()/"pirateship_browser"
STORAGE_DIR.mkdir(parents=True,exist_ok=True)
WINDOW_TITLE="PrintFlow CRM — Pirate Ship"
_MUTEX_HANDLE=None


def focus_existing_window():
    if os.name!="nt": return False
    try:
        hwnd=ctypes.windll.user32.FindWindowW(None,WINDOW_TITLE)
        if not hwnd: return False
        ctypes.windll.user32.ShowWindow(hwnd,9)
        ctypes.windll.user32.BringWindowToTop(hwnd)
        ctypes.windll.user32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


def enforce_single_instance():
    global _MUTEX_HANDLE
    if os.name!="nt": return True
    try:
        kernel32=ctypes.windll.kernel32
        _MUTEX_HANDLE=kernel32.CreateMutexW(None,False,"Local\\PrintFlowCRM_PirateShipBrowser")
        if kernel32.GetLastError()==183 and focus_existing_window(): return False
    except Exception:
        pass
    return True


class Bridge:
    def shipment_found(self,payload):
        try:
            if isinstance(payload,str): payload=json.loads(payload)
            payload=dict(payload or {})
            payload["captured_at"]=datetime.now().isoformat(timespec="seconds")
            tmp=Path(tempfile.gettempdir())/f"printflow-pirateship-{os.getpid()}.json"
            tmp.write_text(json.dumps(payload,ensure_ascii=False),encoding="utf-8")
            tmp.replace(RESULT_FILE)
            return True
        except Exception:
            return False


bridge=Bridge()
window=webview.create_window(WINDOW_TITLE,"https://ship.pirateship.com/",js_api=bridge,width=1320,height=900,min_size=(900,650))

INJECT=r'''
(() => {
  window.__PRINTFLOW_PS_REQUEST__ = window.__PRINTFLOW_PS_REQUEST__ || null;
  if (window.__PRINTFLOW_PS_SCANNER__) return;
  window.__PRINTFLOW_PS_SCANNER__ = true;

  const normalize = value => String(value || '').toLowerCase().replace(/[^a-z0-9]+/g,' ').trim();
  const visible = el => {
    if (!el || !el.getBoundingClientRect) return false;
    const r=el.getBoundingClientRect(), s=getComputedStyle(el);
    return r.width>1 && r.height>1 && s.display!=='none' && s.visibility!=='hidden';
  };
  const looksGreen = el => {
    for(let depth=0; el && depth<4; depth++,el=el.parentElement) {
      const s=getComputedStyle(el);
      const values=[s.color,s.backgroundColor,s.borderColor].join(' ');
      const nums=[...values.matchAll(/rgba?\((\d+),\s*(\d+),\s*(\d+)/g)];
      if(nums.some(m => Number(m[2])>85 && Number(m[2])>Number(m[1])*1.25 && Number(m[2])>Number(m[3])*1.18)) return true;
    }
    return false;
  };
  const stage = () => {
    const stages=['Purchased','Printed','Ready to Ship','In Transit','Delivered'];
    let active='';
    document.querySelectorAll('body *').forEach(el => {
      if(!visible(el) || el.children.length>2) return;
      const text=(el.innerText||el.textContent||'').replace(/\s+/g,' ').trim();
      const idx=stages.findIndex(x => x.toLowerCase()===text.toLowerCase());
      if(idx>=0 && looksGreen(el) && idx>=stages.indexOf(active)) active=stages[idx];
    });
    return active;
  };
  const trackingNumber = () => {
    const candidates=[];
    document.querySelectorAll('a,button,[role="link"],[data-clipboard-text]').forEach(el => {
      candidates.push(el.getAttribute('data-clipboard-text')||'');
      candidates.push(el.innerText||el.textContent||'');
      candidates.push(el.getAttribute('href')||'');
    });
    candidates.push(document.body ? document.body.innerText : '');
    for(const raw of candidates) {
      const value=String(raw||'').replace(/\s+/g,' ');
      const ups=value.match(/\b1Z[A-Z0-9]{16}\b/i); if(ups) return ups[0].toUpperCase();
      const usps=value.match(/\b(?:92|93|94|95)\d{18,20}\b/); if(usps) return usps[0];
      const fedex=value.match(/\b\d{12}\b|\b\d{15}\b/); if(fedex) return fedex[0];
    }
    return '';
  };
  let lastPayload='';
  const scan = async () => {
    const request=window.__PRINTFLOW_PS_REQUEST__;
    if(!request || !document.body) return;
    const body=normalize(document.body.innerText||'');
    const buyer=normalize(request.buyer_name||'');
    const postal=normalize(request.postal_code||'');
    const buyerParts=buyer.split(' ').filter(x=>x.length>1);
    if(buyerParts.length && !buyerParts.every(x=>body.includes(x))) return;
    if(postal && !body.includes(postal.split(' ')[0])) return;
    const tracking=trackingNumber();
    if(!tracking) return;
    const payload={request_id:request.request_id,order_id:request.order_id,order_no:request.order_no,
      buyer_name:request.buyer_name,tracking_no:tracking,pirateship_status:stage(),url:location.href};
    const encoded=JSON.stringify(payload);
    if(encoded===lastPayload) return;
    lastPayload=encoded;
    try { await window.pywebview.api.shipment_found(payload); } catch(_) {}
  };
  setInterval(scan,2500);
  scan();
})();
'''


def keep_scanner_alive(win):
    while True:
        time.sleep(2.0)
        try:
            request=None
            if REQUEST_FILE.exists(): request=json.loads(REQUEST_FILE.read_text(encoding="utf-8"))
            win.run_js("window.__PRINTFLOW_PS_REQUEST__ = "+json.dumps(request,ensure_ascii=False)+";")
            win.run_js(INJECT)
        except Exception:
            try:
                if not win.get_current_url(): break
            except Exception:
                break


if __name__=="__main__" and enforce_single_instance():
    webview.start(keep_scanner_alive,window,private_mode=False,storage_path=str(STORAGE_DIR))

import ctypes
import base64
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
LABEL_RESULT_FILE=data_dir()/"pirateship_label_result.json"
LABELS_DIR=data_dir()/"shipping_labels"
LABELS_DIR.mkdir(parents=True,exist_ok=True)
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

    def label_found(self,payload):
        """Save the original Pirate Ship PDF bytes without rasterizing its barcode."""
        try:
            if isinstance(payload,str): payload=json.loads(payload)
            payload=dict(payload or {})
            raw=str(payload.pop("pdf_base64","") or "")
            if not raw:return False
            if "," in raw:raw=raw.split(",",1)[1]
            pdf=base64.b64decode(raw,validate=False)
            if len(pdf)<100 or not pdf.startswith(b"%PDF"):return False
            order_no="".join(ch for ch in str(payload.get("order_no") or "shipping-label") if ch.isalnum() or ch in "-_")
            path=LABELS_DIR/f"{order_no}-4x6.pdf"
            tmp=Path(tempfile.gettempdir())/f"printflow-label-{os.getpid()}.pdf"
            tmp.write_bytes(pdf);tmp.replace(path)
            payload.update({"label_path":str(path),"label_size":"4x6","captured_at":datetime.now().isoformat(timespec="seconds")})
            meta=Path(tempfile.gettempdir())/f"printflow-label-result-{os.getpid()}.json"
            meta.write_text(json.dumps(payload,ensure_ascii=False),encoding="utf-8");meta.replace(LABEL_RESULT_FILE)
            return True
        except Exception:
            return False


bridge=Bridge()
webview.settings['ALLOW_DOWNLOADS']=True
window=webview.create_window(WINDOW_TITLE,"https://ship.pirateship.com/",js_api=bridge,width=1320,height=900,min_size=(900,650))

INJECT=r'''
(() => {
  window.__PRINTFLOW_PS_REQUEST__ = window.__PRINTFLOW_PS_REQUEST__ || null;
  if (window.__PRINTFLOW_PS_SCANNER__) return;
  window.__PRINTFLOW_PS_SCANNER__ = true;

  const panel=document.createElement('div');
  panel.id='printflow-pirateship-scanner';
  Object.assign(panel.style,{position:'fixed',top:'14px',right:'16px',zIndex:'2147483647',width:'320px',
    background:'#111827',color:'#fff',border:'2px solid #38bdf8',borderRadius:'10px',padding:'12px 14px',
    font:'14px Segoe UI,Arial,sans-serif',boxShadow:'0 8px 26px rgba(0,0,0,.42)'});
  panel.innerHTML='<div style="font-weight:700;color:#7dd3fc">PrintFlow Shipment Capture</div>'+
    '<div id="printflow-ps-target" style="font-size:12px;color:#cbd5e1;margin-top:5px">Waiting for an armed PrintFlow order…</div>'+
    '<button id="printflow-ps-capture" style="margin-top:9px;width:100%;background:#2563eb;color:white;border:0;border-radius:7px;padding:9px;font-weight:700;cursor:pointer">Capture This Shipment</button>'+
    '<button id="printflow-ps-label" style="margin-top:7px;width:100%;background:#0f766e;color:white;border:0;border-radius:7px;padding:9px;font-weight:700;cursor:pointer">Save / Refresh 4×6 Label PDF</button>'+
    '<div id="printflow-ps-status" style="font-size:12px;color:#93c5fd;margin-top:8px">Automatic tracking and label capture are active.</div>';
  document.documentElement.appendChild(panel);
  const setStatus=(text,color='#93c5fd')=>{const el=document.getElementById('printflow-ps-status');if(el){el.textContent=text;el.style.color=color;}};
  const updateTarget=()=>{const el=document.getElementById('printflow-ps-target'),r=window.__PRINTFLOW_PS_REQUEST__;if(el)el.textContent=r?`Target: ${r.buyer_name} • ${r.order_no}`:'Waiting for an armed PrintFlow order…';};

  const capturePdfBlob = async blob => {
    const request=window.__PRINTFLOW_PS_REQUEST__;
    if(!request || !blob || blob.size<100) return;
    try {
      const head=new Uint8Array(await blob.slice(0,5).arrayBuffer());
      if(String.fromCharCode(...head)!=='%PDF-') return;
      const reader=new FileReader();
      reader.onload=async()=>{
        try {
          const saved=await window.pywebview.api.label_found({request_id:request.request_id,order_id:request.order_id,
            order_no:request.order_no,buyer_name:request.buyer_name,pdf_base64:String(reader.result||'')});
          if(saved)setStatus('Tracking and 4×6 label PDF captured ✓','#86efac');
        } catch(_) {}
      };
      reader.readAsDataURL(blob);
    } catch(_) {}
  };
  // Pirate Ship can return its label through fetch, XHR, or a generated blob URL.
  // Observe all three and preserve the original PDF bytes for barcode-safe reprints.
  if(!window.__PRINTFLOW_PDF_HOOKS__){
    window.__PRINTFLOW_PDF_HOOKS__=true;
    const originalFetch=window.fetch;
    window.fetch=async(...args)=>{const response=await originalFetch(...args);try{const type=response.headers.get('content-type')||'';if(/pdf/i.test(type))capturePdfBlob(await response.clone().blob());}catch(_){}return response;};
    const originalOpen=XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open=function(...args){this.addEventListener('load',()=>{try{const type=this.getResponseHeader('content-type')||'';if(/pdf/i.test(type)){if(this.response instanceof Blob)capturePdfBlob(this.response);else if(this.response instanceof ArrayBuffer)capturePdfBlob(new Blob([this.response],{type:'application/pdf'}));}}catch(_){}});return originalOpen.apply(this,args);};
    const originalObjectUrl=URL.createObjectURL.bind(URL);
    URL.createObjectURL=blob=>{try{capturePdfBlob(blob);}catch(_){}return originalObjectUrl(blob);};
  }
  document.getElementById('printflow-ps-label').onclick=async()=>{
    if(!window.__PRINTFLOW_PS_REQUEST__){setStatus('Open Pirate Ship from the matching PrintFlow order first.','#fca5a5');return;}
    const links=[...document.querySelectorAll('a[href]')];
    const direct=links.find(a=>/pdf|label|download/i.test((a.getAttribute('href')||'')+' '+(a.innerText||'')));
    if(direct){
      try{setStatus('Downloading the original 4×6 label PDF…');const response=await fetch(direct.href,{credentials:'include'});await capturePdfBlob(await response.blob());return;}catch(_){}
    }
    const reprint=[...document.querySelectorAll('button,a,[role="button"]')].find(el=>/reprint label|download label|print label/i.test(el.innerText||el.textContent||''));
    if(reprint){setStatus('Opening Pirate Ship’s label download…');reprint.click();return;}
    setStatus('No label download is visible yet. Purchase or open the shipment label, then try again.','#fbbf24');
  };

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
  const scan = async (manual=false) => {
    const request=window.__PRINTFLOW_PS_REQUEST__;
    updateTarget();
    if(!request || !document.body) { if(manual)setStatus('Open this shipment from a PrintFlow order first.','#fca5a5'); return; }
    const body=normalize(document.body.innerText||'');
    const buyer=normalize(request.buyer_name||'');
    const postal=normalize(request.postal_code||'');
    const buyerParts=buyer.split(' ').filter(x=>x.length>1);
    if(buyerParts.length && !buyerParts.every(x=>body.includes(x))) { if(manual)setStatus(`This page does not match ${request.buyer_name}. Nothing captured.`,'#fca5a5'); return; }
    if(postal && !body.includes(postal.split(' ')[0])) { if(manual)setStatus('The shipment ZIP does not match the armed order. Nothing captured.','#fca5a5'); return; }
    const tracking=trackingNumber();
    if(!tracking) { if(manual)setStatus('No purchased-label tracking number found on this page yet.','#fbbf24'); return; }
    const payload={request_id:request.request_id,order_id:request.order_id,order_no:request.order_no,
      buyer_name:request.buyer_name,tracking_no:tracking,pirateship_status:stage(),url:location.href};
    const encoded=JSON.stringify(payload);
    if(encoded===lastPayload && !manual) return;
    lastPayload=encoded;
    try {
      const saved=await window.pywebview.api.shipment_found(payload);
      if(saved)setStatus(`Captured ${tracking} ✓`,'#86efac');
      else setStatus('PrintFlow could not save this shipment. Try again.','#fca5a5');
    } catch(_) { setStatus('PrintFlow could not save this shipment. Try again.','#fca5a5'); }
  };
  document.getElementById('printflow-ps-capture').onclick=()=>scan(true);
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

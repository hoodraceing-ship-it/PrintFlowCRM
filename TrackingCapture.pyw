import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import webview


def data_dir():
    base=os.getenv("LOCALAPPDATA")
    root=(Path(base)/"PrintFlowCRM") if base else (Path.home()/".printflowcrm")
    root.mkdir(parents=True,exist_ok=True)
    return root


REQUEST_FILE=data_dir()/"tracking_browser_request.json"
RESULT_FILE=data_dir()/"tracking_browser_result.json"


try:
    request=json.loads(REQUEST_FILE.read_text(encoding="utf-8"))
except Exception:
    request={}


class Bridge:
    def status_found(self,payload):
        try:
            if isinstance(payload,str):payload=json.loads(payload)
            payload=dict(payload or {})
            payload.update({
                "order_id":int(request.get("order_id") or 0),
                "tracking_no":str(request.get("tracking_no") or ""),
                "captured_at":datetime.now().isoformat(timespec="seconds"),
            })
            tmp=Path(tempfile.gettempdir())/f"printflow-tracking-{os.getpid()}.json"
            tmp.write_text(json.dumps(payload,ensure_ascii=False),encoding="utf-8")
            tmp.replace(RESULT_FILE)
            return True
        except Exception:
            return False


bridge=Bridge()
url=str(request.get("url") or "https://tools.usps.com/go/TrackConfirmAction")
window=webview.create_window("PrintFlow CRM — Shipment Tracking",url,js_api=bridge,width=1320,height=900,min_size=(900,650))

INJECT=r'''
(() => {
  if(window.__PRINTFLOW_TRACKING_MONITOR__)return;
  window.__PRINTFLOW_TRACKING_MONITOR__=true;
  const expected=%EXPECTED%;
  const normalize=value=>String(value||'').toUpperCase().replace(/[^A-Z0-9]/g,'');
  const visible=el=>{
    if(!el||!el.getBoundingClientRect)return false;
    const r=el.getBoundingClientRect(),s=getComputedStyle(el);
    return r.width>1&&r.height>1&&s.display!=='none'&&s.visibility!=='hidden'&&Number(s.opacity||1)>0;
  };
  const green=el=>{
    for(let depth=0;el&&depth<5;depth++,el=el.parentElement){
      const s=getComputedStyle(el),values=[s.color,s.backgroundColor,s.borderColor].join(' ');
      const colors=[...values.matchAll(/rgba?\((\d+),\s*(\d+),\s*(\d+)/g)];
      if(colors.some(m=>Number(m[2])>75&&Number(m[2])>Number(m[1])*1.12&&Number(m[2])>Number(m[3])*1.06))return true;
    }
    return false;
  };
  const banner=()=>{
    let panel=document.getElementById('printflow-tracking-status');
    if(panel)return panel;
    panel=document.createElement('div');panel.id='printflow-tracking-status';
    Object.assign(panel.style,{position:'fixed',top:'14px',right:'18px',zIndex:'2147483647',background:'#111827',color:'#bfdbfe',border:'2px solid #3b82f6',borderRadius:'9px',padding:'10px 14px',font:'600 13px Segoe UI,Arial,sans-serif',boxShadow:'0 7px 24px rgba(0,0,0,.35)'});
    panel.textContent='PrintFlow is checking this shipment…';document.documentElement.appendChild(panel);return panel;
  };
  let sent='';
  const scan=async()=>{
    if(!document.body)return;
    const panel=banner(),bodyText=(document.body.innerText||'').replace(/\s+/g,' ').trim();
    const bodyNumber=normalize(bodyText),numberMatches=expected&&bodyNumber.includes(expected);
    if(!numberMatches){panel.textContent='Waiting for the matching tracking details…';return;}
    const leaf=[...document.querySelectorAll('body *')].filter(el=>visible(el)&&el.children.length<=2);
    const deliveredPhrase=/\b(?:your item|your package|the package|shipment) (?:was |has been )?delivered\b/i.test(bodyText);
    const deliveredBadge=leaf.some(el=>{
      const text=(el.innerText||el.textContent||'').replace(/\s+/g,' ').trim();
      return (/^delivered(?:\b|,)/i.test(text)&&text.length<180&&green(el));
    });
    let status='';
    if(deliveredPhrase||deliveredBadge)status='delivered';
    else if(/\bout for delivery\b/i.test(bodyText))status='out_for_delivery';
    else if(/\b(?:in transit|on the way|moving through network)\b/i.test(bodyText))status='in_transit';
    if(!status){panel.textContent='Tracking loaded • waiting for a carrier status';return;}
    if(status===sent)return;sent=status;
    try{
      const saved=await window.pywebview.api.status_found({status:status,url:location.href,page_title:document.title});
      if(saved){panel.textContent=status==='delivered'?'PrintFlow updated this order to Delivered ✓':'PrintFlow verified this shipment is still moving';panel.style.borderColor=status==='delivered'?'#22c55e':'#3b82f6';panel.style.color=status==='delivered'?'#bbf7d0':'#bfdbfe';}
    }catch(_){panel.textContent='PrintFlow could not save this tracking result';panel.style.borderColor='#ef4444';}
  };
  setInterval(scan,1500);scan();
})();
'''.replace("%EXPECTED%",json.dumps("".join(ch for ch in str(request.get("tracking_no") or "").upper() if ch.isalnum())))


def keep_monitor_alive(win):
    while True:
        try:
            win.run_js(INJECT)
        except Exception:
            try:
                if not win.get_current_url():break
            except Exception:break
        import time
        time.sleep(1.5)


if __name__=="__main__":
    webview.start(keep_monitor_alive,window,private_mode=False,storage_path=str(data_dir()/"tracking_browser"))

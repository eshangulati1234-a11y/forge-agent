"""Decorative browser cursor. It never sends input or reads field contents."""

CURSOR_JS = r"""
(() => {
  const install = () => {
    if (document.querySelector('[data-forge-overlay]') || !document.documentElement) return;
    const host = document.createElement('div');
    host.setAttribute('data-forge-overlay', '');
    host.setAttribute('aria-hidden', 'true');
    host.style.cssText = 'all:initial!important;position:fixed!important;inset:0!important;' +
      'width:0!important;height:0!important;z-index:2147483647!important;pointer-events:none!important;';
    const shadow = host.attachShadow({mode: 'open'});
    shadow.innerHTML = `<style>
      :host, * { pointer-events:none!important; box-sizing:border-box; }
      #cursor { position:fixed;left:16px;top:16px;width:24px;height:30px;
        filter:drop-shadow(0 1px 2px #0008);transition:left 180ms ease,top 180ms ease; }
      #cursor::after { content:'Forge';position:absolute;left:20px;top:22px;
        background:#4338ca;color:white;border-radius:4px;padding:3px 6px;font:12px sans-serif; }
      #target { display:none;position:fixed;border:2px solid #6366f1;border-radius:5px;
        box-shadow:0 0 0 3px #6366f133; }
      #pulse { position:fixed;display:none;width:28px;height:28px;margin:-14px;
        border:3px solid #6366f1;border-radius:50%; }
    </style>
    <div id="target"></div><div id="cursor"><svg width="24" height="30" viewBox="0 0 24 30">
      <path d="M2 2 L2 24 L8 18 L13 28 L17 26 L12 16 L21 16 Z" fill="#6366f1" stroke="white" stroke-width="2"/>
    </svg></div><div id="pulse"></div>`;
    document.documentElement.append(host);
    let timer;
    window.__forgeVisualize = (el, click, duration) => {
      const r = el.getBoundingClientRect();
      if (!r.width || !r.height) return;
      const x = Math.max(0, Math.min(innerWidth - 1, r.left + r.width / 2));
      const y = Math.max(0, Math.min(innerHeight - 1, r.top + r.height / 2));
      const cursor = shadow.getElementById('cursor');
      const target = shadow.getElementById('target');
      const pulse = shadow.getElementById('pulse');
      cursor.style.left = x + 'px'; cursor.style.top = y + 'px';
      Object.assign(target.style, {display:'block',left:r.left+'px',top:r.top+'px',
        width:r.width+'px',height:r.height+'px'});
      clearTimeout(timer);
      timer = setTimeout(() => { target.style.display = 'none'; }, duration + 700);
      if (click) {
        Object.assign(pulse.style, {display:'block',left:x+'px',top:y+'px'});
        pulse.getAnimations().forEach(a => a.cancel());
        pulse.animate([{opacity:0,transform:'scale(.4)'},{opacity:1,offset:.4},
          {opacity:0,transform:'scale(1.8)'}], {duration:350,delay:Math.min(duration,180),fill:'forwards'});
      }
    };
  };
  if (document.documentElement) install();
  else document.addEventListener('DOMContentLoaded', install, {once:true});
})();
"""

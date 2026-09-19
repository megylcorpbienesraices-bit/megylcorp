(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  let voiceEnabled = localStorage.getItem('itm_sophia_voice') !== 'off';
  let eventWs = null, reconnectTimer = null;
  let recorder = null, mediaStream = null, chunks = [], recording = false, pointerHeld = false;

  function openPanel(open=true) {
    const p=$('sophiaPanel'), b=$('sophiaToggle'); if(!p||!b)return;
    p.classList.toggle('open', open); b.setAttribute('aria-expanded', open?'true':'false');
    if(open) setTimeout(()=>$('sophiaInput')?.focus(), 60);
  }
  function addMsg(text, who='assistant') {
    const host=$('sophiaMessages'); if(!host||!text)return;
    const d=document.createElement('div'); d.className=`sophia-msg ${who}`; d.textContent=String(text); host.appendChild(d); host.scrollTop=host.scrollHeight;
  }
  function updateWatches(watches) {
    const n=Array.isArray(watches)?watches.length:0;
    if($('sophiaWatchBadge')) $('sophiaWatchBadge').textContent=String(n);
    if($('sophiaWatchText')) $('sophiaWatchText').textContent=n?`${n} aviso${n===1?'':'s'} activo${n===1?'':'s'}`:'Sin avisos activos';
  }
  function setBusy(on) { $('sophiaSend')?.classList.toggle('busy',!!on); }

  async function speak(text) {
    if(!voiceEnabled || !text)return;
    try {
      const r=await fetch('/api/sophia/voice/synthesize',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:String(text).slice(0,900)})});
      if(!r.ok) return;
      const blob=await r.blob(); const url=URL.createObjectURL(blob); const a=new Audio(url);
      a.onended=()=>URL.revokeObjectURL(url); a.onerror=()=>URL.revokeObjectURL(url); await a.play();
    } catch(_) {}
  }

  async function sendMessage(text) {
    const input=$('sophiaInput'); const msg=String(text ?? input?.value ?? '').trim(); if(!msg)return;
    if(input) input.value=''; addMsg(msg,'user'); setBusy(true);
    try {
      const tf=$('traceCandle')?.value || '1m';
      const r=await fetch('/api/sophia/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:msg,timeframe:tf})});
      const data=await r.json().catch(()=>({}));
      const reply=data.reply || (r.ok?'Listo.':'No pude procesar esa petición.');
      addMsg(reply,'assistant'); updateWatches(data.watches || []); if(r.ok) speak(reply);
    } catch(_) { addMsg('No pude comunicarme con el motor local de Sophia.','assistant'); }
    finally { setBusy(false); }
  }

  function toast(event) {
    const host=$('sophiaAlerts'); if(!host)return;
    const d=document.createElement('div'); d.className='sophia-alert-toast';
    const msg=event.message || 'Condición detectada.'; d.innerHTML='<b>SOPHIA</b><span></span>'; d.querySelector('span').textContent=msg;
    host.appendChild(d); setTimeout(()=>d.classList.add('show'),20); setTimeout(()=>{d.classList.remove('show');setTimeout(()=>d.remove(),250)},7000);
    addMsg(msg,'assistant'); speak(msg);
  }

  function connectEvents() {
    if(eventWs){try{eventWs.close()}catch(_){}}
    const proto=location.protocol==='https:'?'wss:':'ws:';
    try {
      eventWs=new WebSocket(`${proto}//${location.host}/ws/sophia/events`);
      eventWs.onmessage=ev=>{try{const x=JSON.parse(ev.data);if(x.type==='SOPHIA_ALERT')toast(x);else if(x.type==='SOPHIA_READY')updateWatches(x.watches||[]);else if(x.type==='HEARTBEAT'&&$('sophiaWatchBadge'))$('sophiaWatchBadge').textContent=String(x.watch_count||0);}catch(_){}};
      eventWs.onclose=()=>{eventWs=null;reconnectTimer=setTimeout(connectEvents,1500)};
      eventWs.onerror=()=>{try{eventWs.close()}catch(_){}};
    } catch(_){ reconnectTimer=setTimeout(connectEvents,1800); }
  }

  async function loadStatus() {
    try { const r=await fetch('/api/sophia/status',{cache:'no-store'}); if(!r.ok)return; const s=await r.json(); updateWatches(s.watches||[]);
      const v=s.voice||{}; const stt=(v.stt||{}).configured; const tts=(v.tts||{}).configured;
      const line=$('sophiaRuntimeLine'); if(line) line.textContent=`SELF-HOSTED · SIN CRÉDITOS · ${stt?'MIC LISTO':'MIC PENDIENTE VPS'} · ${tts?'VOZ LISTA':'VOZ PENDIENTE VPS'}`;
    } catch(_){}
  }

  async function startRecording(e) {
    if(recording) return;
    pointerHeld = true;
    e?.preventDefault?.();
    if(!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder==='undefined') { addMsg('El navegador no habilitó el micrófono. Puedes seguir escribiéndome.','assistant'); return; }
    try {
      mediaStream=await navigator.mediaDevices.getUserMedia({audio:true});
      const preferred=['audio/webm;codecs=opus','audio/webm','audio/mp4']; let opts={};
      for(const mt of preferred){if(MediaRecorder.isTypeSupported?.(mt)){opts={mimeType:mt};break;}}
      chunks=[]; recorder=new MediaRecorder(mediaStream,opts); recording=true; $('sophiaMic')?.classList.add('recording');
      recorder.ondataavailable=ev=>{if(ev.data?.size)chunks.push(ev.data)};
      recorder.onstop=async()=>{
        recording=false; $('sophiaMic')?.classList.remove('recording'); mediaStream?.getTracks().forEach(t=>t.stop()); mediaStream=null;
        const type=recorder?.mimeType||'audio/webm'; const blob=new Blob(chunks,{type}); if(!blob.size)return;
        const ext=type.includes('mp4')?'.mp4':'.webm'; const fd=new FormData(); fd.append('file',blob,`speech${ext}`);
        const hint=$('sophiaMicHint'); if(hint)hint.textContent='Transcribiendo localmente…';
        try { const r=await fetch('/api/sophia/voice/transcribe',{method:'POST',body:fd}); const data=await r.json().catch(()=>({})); if(!r.ok)throw new Error(data.detail||'STT'); const text=String(data.text||'').trim(); if(text){if($('sophiaInput'))$('sophiaInput').value=text; await sendMessage(text);} }
        catch(_){addMsg('El reconocimiento de voz local se terminará de activar al instalar el modelo en el VPS. Puedes escribirme mientras tanto.','assistant');}
        finally{if(hint)hint.textContent='Mantén 🎙 para hablar · voz local, sin API de pago.';}
      };
      recorder.start(160);
      if(!pointerHeld) setTimeout(()=>{ if(recording){ try{recorder?.stop()}catch(_){} } }, 30);
    } catch(_){ addMsg('No pude acceder al micrófono. Revisa el permiso del navegador.','assistant'); }
  }
  function stopRecording(e){ pointerHeld=false; e?.preventDefault?.(); if(!recording)return; try{recorder?.stop()}catch(_){} }

  function init(){
    $('sophiaToggle')?.addEventListener('click',()=>openPanel(!$('sophiaPanel')?.classList.contains('open')));
    $('sophiaClose')?.addEventListener('click',()=>openPanel(false));
    $('sophiaSend')?.addEventListener('click',()=>sendMessage());
    $('sophiaInput')?.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();sendMessage();}});
    const vb=$('sophiaVoiceToggle'); if(vb){vb.setAttribute('aria-pressed',voiceEnabled?'true':'false');vb.textContent=voiceEnabled?'🔊':'🔇';vb.addEventListener('click',()=>{voiceEnabled=!voiceEnabled;localStorage.setItem('itm_sophia_voice',voiceEnabled?'on':'off');vb.setAttribute('aria-pressed',voiceEnabled?'true':'false');vb.textContent=voiceEnabled?'🔊':'🔇';});}
    $('sophiaClearWatches')?.addEventListener('click',async()=>{try{const r=await fetch('/api/sophia/watches/clear',{method:'POST'});const d=await r.json();updateWatches(d.watches||[]);addMsg(`Cancelé ${d.cancelled||0} avisos.`,'assistant')}catch(_){}});
    const mic=$('sophiaMic'); if(mic){mic.addEventListener('pointerdown',startRecording);mic.addEventListener('pointerup',stopRecording);mic.addEventListener('pointercancel',stopRecording);mic.addEventListener('pointerleave',e=>{if(recording)stopRecording(e)});}
    connectEvents(); loadStatus();
  }
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init,{once:true});else init();
})();

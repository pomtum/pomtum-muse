/* Copyright 2026 PomTum contributors. UI layout adapted from Meta's Apache-2.0 muse_ui.c. */
import './style.css';
import { Companion } from './avatar';
import { audioMotion } from './audio-motion';

const icons:Record<string,string>={
  chat:'<path d="M21 11.5a8.5 8.5 0 0 1-8.5 8.5H4l-2 2V11.5A8.5 8.5 0 0 1 10.5 3h2a8.5 8.5 0 0 1 8.5 8.5Z"/><path d="M7 9h10M7 13h7"/>',
  keyboard:'<rect x="2" y="5" width="20" height="14" rx="3"/><path d="M6 9h.1M10 9h.1M14 9h.1M18 9h.1M6 12h.1M10 12h.1M14 12h.1M18 12h.1M7 16h10"/>',
  settings:'<path d="M4 7h16M4 17h16"/><circle cx="8" cy="7" r="3"/><circle cx="16" cy="17" r="3"/>',
  close:'<path d="m6 6 12 12M18 6 6 18"/>',
  send:'<path d="m5 12 7-7 7 7M12 5v15"/>',
  stop:'<rect x="6" y="6" width="12" height="12" rx="3"/>',
  sound:'<path d="m11 4-6 5H2v6h3l6 5V4ZM15 8a6 6 0 0 1 0 8M18 5a10 10 0 0 1 0 14"/>'
};
const icon=(name:string)=>`<svg viewBox="0 0 24 24" aria-hidden="true">${icons[name]}</svg>`;
document.querySelector('#app')!.innerHTML=`
  <main class="companion" aria-label="Muse 本机对话">
    <div class="face" id="face">
      <svg class="progress-ring" viewBox="0 0 466 466" aria-hidden="true"><circle class="ring-track" cx="233" cy="233" r="229"/><circle id="ring-value" cx="233" cy="233" r="229" pathLength="100"/></svg>
      <div class="connection"><i id="connection-dot"></i><span id="connection">正在连接</span></div>
      <div class="presence"><span id="mood">CONNECTING</span></div>
      <div class="stage" id="stage" role="img" aria-label="Muse 官方 ESP32 角色动画"></div>
      <div class="level-meter" id="level-meter" aria-hidden="true">${'<i></i>'.repeat(14)}</div>
      <section class="caption-area"><p class="caption" id="caption"></p><button class="caption-more" id="caption-more" hidden>查看完整回复</button></section>
      <div class="key-hint"><span id="voice-label">按住侧边 AI 键说话</span></div>
    </div>
    <button class="icon-button speaker-button" id="speaker-button" aria-label="朗读回复" aria-pressed="true">${icon('sound')}<span class="mute-slash" aria-hidden="true"></span></button>
    <button id="stop-button" class="icon-button stop-button" aria-label="停止回复" hidden>${icon('stop')}</button>
    <div id="notice" role="status" hidden></div>
    <nav class="page-dots" aria-label="页面"><span class="page-dot active" aria-hidden="true"></span><button id="settings-button" aria-label="设置，或向左滑动"><span class="page-dot"></span></button></nav>
  </main>
  <dialog id="history-dialog"><div class="sheet-head"><div><span class="eyebrow">与你的 Muse</span><h2>对话记录</h2></div><button class="icon-button close-sheet" aria-label="关闭">${icon('close')}</button></div><div id="history" class="history"></div><button class="text-button" id="new-chat">清除本机记录</button></dialog>
  <dialog id="keyboard-dialog"><div class="sheet-head"><h2>说点什么</h2><button class="icon-button close-sheet" aria-label="关闭">${icon('close')}</button></div><form id="chat-form"><textarea id="message-input" maxlength="6000" rows="3" placeholder="想法、问题，或者今天的小事…" aria-label="消息"></textarea><div class="form-footer"><span>发送给你的 Muse</span><button class="send-button" type="submit" aria-label="发送">${icon('send')}</button></div></form></dialog>
  <dialog id="settings-dialog"><div class="sheet-head"><h2>设置</h2><button class="icon-button close-sheet" aria-label="关闭">${icon('close')}</button></div><label class="setting"><span><strong>朗读回复</strong><small>本机中文声音，离线合成</small></span><input id="speech-enabled" type="checkbox" role="switch"></label><p class="settings-note" id="voice-status">正在检查声音…</p><button class="menu-item" id="history-button">${icon('chat')}<span>对话记录</span></button><button class="menu-item" id="keyboard-button">${icon('keyboard')}<span>键盘输入</span></button><form id="sdk-token-form" class="token-setting" autocomplete="off"><label for="sdk-token">SDK 令牌</label><p id="sdk-token-status" class="settings-note" role="status">打开设置后检查配置</p><div class="token-entry"><input id="sdk-token" type="password" autocomplete="off" autocapitalize="off" spellcheck="false" maxlength="128" placeholder="粘贴你的 mgst_… 令牌" aria-describedby="sdk-token-hint"><button id="sdk-token-save" type="submit">保存</button></div><p id="sdk-token-hint" class="settings-note">仅保存在本机 SDK，不显示已有令牌。</p></form><p class="settings-note">轻触角色会有回应。按住侧边 AI 键说话，松开后发送。</p><div class="settings-actions"><button class="text-button" id="fullscreen-button">切换全屏</button><button class="text-button" id="exit-button">返回桌面</button></div></dialog>`;
const $=<T extends HTMLElement=HTMLElement>(id:string)=>document.getElementById(id) as T;
let avatar:Companion|null=null;
try{avatar=new Companion($('stage'))}catch{ $('stage').classList.add('unavailable');$('stage').textContent='角色暂时不可用，对话仍可继续'; }
const ioBase='http://127.0.0.1:17864';
type Entry={role:'user'|'assistant',text:string,id:string};
let history:Entry[]=[];
try{history=JSON.parse(localStorage.getItem('muse.history')||'[]').filter((m:Entry)=>typeof m.text==='string').slice(-40)}catch{history=[]}
let connected=false,pending=false,recording=false,currentRequest='',currentReply='',replyMessage='',speaking=false;
let speechEnabled=localStorage.getItem('muse.speech')!=='off',voiceReady=false;
let audio:HTMLAudioElement|null=null,audioContext:AudioContext|null=null,analyser:AnalyserNode|null=null,source:MediaElementAudioSourceNode|null=null;
let audioLevel=0;
let speechGeneration=0,speechAbort:AbortController|null=null;
let timeout=0,noticeTimer=0,captionTimer=0,captionRevision=0;
const completed=new Set<string>();

function notice(text:string){$('notice').textContent=text;$('notice').hidden=false;clearTimeout(noticeTimer);noticeTimer=window.setTimeout(()=>$('notice').hidden=true,6000)}
function persist(){history=history.slice(-40);localStorage.setItem('muse.history',JSON.stringify(history))}
function setCaption(text:string){
  clearTimeout(captionTimer);captionRevision++;
  $('caption').textContent=text.replace(/\s+/g,' ').trim();
  $('app').dataset.caption=text.trim()?'visible':'empty';
  requestAnimationFrame(updateCaptionOverflow);
}
function scheduleCaptionClear(delay:number){
  clearTimeout(captionTimer);const revision=captionRevision;
  captionTimer=window.setTimeout(()=>{
    if(revision!==captionRevision||pending||recording||speaking||speechAbort)return;
    setCaption('');syncState();
  },delay);
}
function updateCaptionOverflow(){const caption=$('caption');$('caption-more').hidden=caption.scrollHeight<=caption.clientHeight+2}
window.addEventListener('resize',updateCaptionOverflow);
function syncState(){
  const mood=recording?'listening':speaking?'speaking':pending?'thinking':connected?'idle':'offline';
  if(avatar)avatar.mood=mood;
  $('app').dataset.state=mood;
  $('mood').textContent=({listening:'LISTENING',speaking:'SPEAKING',thinking:'THINKING',idle:'READY',offline:'CONNECTING'})[mood];
  $('app').dataset.answer=pending||speaking||!!$('caption').textContent?'heard':'';
  $('speaker-button').setAttribute('aria-pressed',String(speechEnabled));
  $('speaker-button').setAttribute('aria-label',speechEnabled?'关闭朗读回复':'开启朗读回复');
  $('stop-button').hidden=!pending&&!speaking;
  $('voice-label').textContent=recording?'松开 AI 键，发送语音':pending?'Muse 正在思考':speaking?'按住 AI 键，可打断':'按住侧边 AI 键说话';
  $('connection').textContent=connected?'MUSE · 已连接':'MUSE · 正在重连';$('connection-dot').classList.toggle('online',connected);
  requestAnimationFrame(updateCaptionOverflow);
}
// The ESP32 full layout uses a 466 px square. Fit it without moving its elements.
function fitFace(){$('face').style.setProperty('--face-scale',String(Math.max(.5,Math.min(innerWidth-8,innerHeight-80)/466)))}
window.addEventListener('resize',fitFace);fitFace();
let recordStarted=0;
const meterSegments=Array.from($('level-meter').children) as HTMLElement[];
setInterval(()=>{
  const progress=recording?Math.min(1,(performance.now()-recordStarted)/15000):audio&&Number.isFinite(audio.duration)&&audio.duration>0?audio.currentTime/audio.duration:0;
  $('ring-value').style.strokeDasharray=`${pending?100/6:progress*100} 100`;
  const lit=Math.round(audioLevel*14);
  meterSegments.forEach((seg,i)=>seg.classList.toggle('lit',speaking&&Math.floor(Math.abs(2*i-13)/2)<(lit+1)/2));
},40);
function upsertReply(text:string,id=replyMessage||currentRequest){
  if(!text)return;const found=history.find(m=>m.role==='assistant'&&m.id===id);
  if(found)found.text=text;else history.push({role:'assistant',text,id});persist();renderHistory();setCaption(text);
}
function renderHistory(){
  const host=$('history');host.replaceChildren();
  if(!history.length){const p=document.createElement('p');p.className='empty';p.textContent='从一句你好开始吧。';host.append(p);return}
  for(const item of history){const entry=document.createElement('div');entry.className=`message ${item.role}`;const by=document.createElement('span');by.textContent=item.role==='user'?'你':'Muse';const p=document.createElement('p');p.textContent=item.text;entry.append(by,p);host.append(entry)}host.scrollTop=host.scrollHeight;
}
function openSheet(id:string){document.querySelectorAll<HTMLDialogElement>('dialog[open]').forEach(d=>d.close());renderHistory();$<HTMLDialogElement>(id).showModal();if(id==='settings-dialog')refreshSdkTokenStatus()}
document.querySelectorAll<HTMLButtonElement>('.close-sheet').forEach(b=>b.onclick=()=>b.closest('dialog')!.close());
document.querySelectorAll('dialog').forEach(d=>d.addEventListener('click',e=>{if(e.target===d){const r=d.getBoundingClientRect();if(e.clientY<r.top||e.clientY>r.bottom||e.clientX<r.left||e.clientX>r.right)d.close()}}));
$('history-button').onclick=()=>openSheet('history-dialog');$('caption-more').onclick=()=>openSheet('history-dialog');
$('settings-button').onclick=()=>openSheet('settings-dialog');
async function refreshSdkTokenStatus(){
  const status=$('sdk-token-status');
  status.textContent='正在检查本机配置…';
  try{const r=await fetch('/api/sdk-token',{cache:'no-store'});if(!r.ok)throw new Error();const data=await r.json();status.textContent=data.configured?'已配置 · 可输入新令牌更换':'尚未配置 SDK 令牌';}
  catch{status.textContent='无法读取配置，请先安装并启动 Muse SDK';}
}
$('settings-dialog').addEventListener('close',()=>{$<HTMLInputElement>('sdk-token').value=''});
$('sdk-token-form').onsubmit=async e=>{
  e.preventDefault();const input=$<HTMLInputElement>('sdk-token'),button=$<HTMLButtonElement>('sdk-token-save'),status=$('sdk-token-status');
  let token=input.value.trim();input.value='';
  if(!/^mgst_[A-Za-z0-9_-]{16,120}$/.test(token)){status.textContent='请输入完整的 mgst_ 开头的 SDK 令牌';token='';return;}
  button.disabled=true;status.textContent='正在保存到本机…';
  try{
    const r=await fetch('/api/sdk-token',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token}),cache:'no-store'});token='';
    if(!r.ok){status.textContent=r.status===400?'令牌格式无效，请重新复制完整令牌':'保存失败，请检查本机 SDK 服务';return;}
    const result=await r.json();status.textContent=result.saved?(result.pairingRequired?'已保存 · 请按仓库首页指南完成手机配对':'已安全保存 · 当前配对保持不变'):'保存失败，请稍后再试';
  }catch{status.textContent='保存失败，请检查本机 SDK 服务';}
  finally{token='';input.value='';button.disabled=false;}
};
let swipeStart:{x:number,y:number}|null=null;
document.querySelector('.companion')!.addEventListener('pointerdown',e=>{const p=e as PointerEvent;swipeStart={x:p.clientX,y:p.clientY}});
document.querySelector('.companion')!.addEventListener('pointerup',e=>{const p=e as PointerEvent;if(swipeStart&&swipeStart.x-p.clientX>70&&Math.abs(swipeStart.y-p.clientY)<60)openSheet('settings-dialog');swipeStart=null});
document.querySelector('.companion')!.addEventListener('pointercancel',()=>swipeStart=null);
$('keyboard-button').onclick=()=>{openSheet('keyboard-dialog');$<HTMLTextAreaElement>('message-input').focus()};
$('fullscreen-button').onclick=()=>{if(document.fullscreenElement)document.exitFullscreen();else document.documentElement.requestFullscreen().catch(()=>notice('请使用浏览器的全屏功能'))};
$<HTMLInputElement>('speech-enabled').checked=speechEnabled;
$('exit-button').onclick=()=>{stopAudio();fetch(ioBase+'/focus',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({active:false}),keepalive:true}).catch(()=>{});window.close();};
$('speech-enabled').onchange=e=>{speechEnabled=(e.target as HTMLInputElement).checked;localStorage.setItem('muse.speech',speechEnabled?'on':'off');if(!speechEnabled){stopAudio();scheduleCaptionClear(12000)}syncState()};
$('speaker-button').onclick=()=>$<HTMLInputElement>('speech-enabled').click();
$('new-chat').onclick=async()=>{await cancelTurn();history=[];persist();renderHistory();setCaption('本机记录已清除，Muse 的会话仍会继续。');scheduleCaptionClear(3000);syncState();$<HTMLDialogElement>('history-dialog').close()};

function closeMouth(){audioLevel=0;if(avatar){avatar.amplitude=0;avatar.mouthShape={open:0,wide:0,round:0}}}
function stopAudio(){
  speechGeneration++;speechAbort?.abort();speechAbort=null;
  if(audio){audio.onended=null;audio.onplaying=null;audio.onwaiting=null;audio.onpause=null;audio.pause();URL.revokeObjectURL(audio.src);audio=null}
  source?.disconnect();source=null;analyser?.disconnect();analyser=null;
  speaking=false;closeMouth();syncState();
}
async function speak(text:string){
  if(!speechEnabled||!voiceReady){scheduleCaptionClear(12000);return;}
  stopAudio();const generation=speechGeneration;const abort=new AbortController();speechAbort=abort;
  try{
    const response=await fetch(ioBase+'/tts',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:text.slice(0,2000)}),signal:abort.signal});
    if(!response.ok)throw new Error('语音暂时不可用，回复已显示');const blob=await response.blob();if(generation!==speechGeneration)return;
    audioContext ||= new AudioContext();await audioContext.resume();if(generation!==speechGeneration)return;
    const player=new Audio(URL.createObjectURL(blob));audio=player;
    const meter=audioContext.createAnalyser();meter.fftSize=1024;meter.smoothingTimeConstant=.35;analyser=meter;
    source=audioContext.createMediaElementSource(player);source.connect(meter);meter.connect(audioContext.destination);
    player.onplaying=()=>{if(generation===speechGeneration){speaking=true;syncState()}};
    player.onwaiting=player.onpause=()=>{if(generation===speechGeneration){speaking=false;closeMouth();syncState()}};
    player.onended=()=>{if(generation===speechGeneration){stopAudio();scheduleCaptionClear(3000)}};
    await player.play();if(generation!==speechGeneration)return;
    const data=new Uint8Array(meter.fftSize),frequency=new Uint8Array(meter.frequencyBinCount);
    const animate=()=>{
      if(generation!==speechGeneration)return;
      if(speaking&&!player.paused){
        meter.getByteTimeDomainData(data);meter.getByteFrequencyData(frequency);
        const motion=audioMotion(data,frequency,audioContext!.sampleRate);audioLevel=motion.level;
        if(avatar){avatar.amplitude=motion.level;avatar.mouthShape=motion.mouth}
      }else closeMouth();
      requestAnimationFrame(animate);
    };animate();
  }catch(e){if(generation===speechGeneration){stopAudio();scheduleCaptionClear(12000);if((e as Error).name!=='AbortError')notice((e as Error).message)}}
}
async function cancelTurn(){
  stopAudio();
  if(pending&&currentRequest){fetch('/api/cancel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({request_id:currentRequest})}).catch(()=>{});notice('已停止本机等待；云端任务可能仍在继续')}
  pending=false;currentRequest='';clearTimeout(timeout);setCaption('');syncState();
}
async function send(text:string,wav?:string){
  if(pending||recording)return;if(!connected){notice('尚未连接 Muse，请稍后再试');return}
  stopAudio();currentRequest=crypto.randomUUID();const request=currentRequest;currentReply='';replyMessage='';pending=true;
  history.push({role:'user',text:text||(wav?'语音消息':''),id:request});persist();renderHistory();setCaption('');syncState();
  timeout=window.setTimeout(()=>{if(currentRequest===request&&pending){cancelTurn();notice('回复等待较久，已停止本机等待，可以稍后再试')}},120000);
  try{
    const response=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text,request_id:request,...(wav?{audio_wav_base64:wav}:{})})});
    const result=await response.json();if(!response.ok||!result.accepted)throw new Error(result.error||'消息未发送成功');
  }catch(e){if(currentRequest===request){pending=false;clearTimeout(timeout);syncState();notice((e as Error).message)}}
}
$('chat-form').onsubmit=e=>{e.preventDefault();const input=$<HTMLTextAreaElement>('message-input');const text=input.value.trim();if(!text)return;if(pending){notice('请等待回复，或先停止本轮');return}input.value='';$<HTMLDialogElement>('keyboard-dialog').close();send(text)};
$('message-input').onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();$<HTMLFormElement>('chat-form').requestSubmit()}};
$('stop-button').onclick=()=>cancelTurn();
const keyEvents=new EventSource(ioBase+'/events');
keyEvents.onopen=()=>focusHeartbeat();
keyEvents.onmessage=e=>{
  let data:any;try{data=JSON.parse(e.data)}catch{return}
  if(data.type==='key'&&data.phase==='down'){
    if(pending)cancelTurn();else stopAudio();recording=true;recordStarted=performance.now();setCaption('');syncState();
  }else if(data.type==='audio'){
    recording=false;syncState();send('',data.audio_wav_base64);
  }else if(data.type==='key'&&data.phase==='up'){
    recording=false;syncState();
  }else if(data.type==='error'){
    recording=false;syncState();notice(data.message||'录音未完成，请再试一次');
  }
};
keyEvents.onerror=()=>{if(recording){recording=false;syncState()}};
function focusHeartbeat(){fetch(ioBase+'/focus',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({active:!document.hidden&&document.hasFocus()})}).catch(()=>{})}
window.addEventListener('focus',focusHeartbeat);window.addEventListener('blur',()=>{if(recording){recording=false;syncState()}focusHeartbeat()});document.addEventListener('visibilitychange',focusHeartbeat);setInterval(focusHeartbeat,3000);focusHeartbeat();
const events=new EventSource('/api/events');
events.onmessage=event=>{
  let data:any;try{data=JSON.parse(event.data)}catch{return}
  if(data.type==='status'){connected=!!(data.connected??data.registered);syncState();return}
  if(!currentRequest||data.request_id!==currentRequest)return;
  if(data.type==='message'&&data.role==='user'){
    const entry=history.find(m=>m.role==='user'&&m.id===currentRequest);if(entry&&data.text){entry.text=data.text;persist();renderHistory()}return;
  }
  if(data.type==='delta'){
    if(data.message_id&&replyMessage&&replyMessage!==data.message_id)currentReply='';
    replyMessage=data.message_id||replyMessage;currentReply+=data.text||data.delta||'';upsertReply(currentReply);
  }else if(data.type==='message'){
    const key=data.message_id||currentRequest;if(completed.has(key))return;completed.add(key);if(completed.size>128)completed.delete(completed.values().next().value!);
    replyMessage=data.message_id||replyMessage;currentReply=data.text||currentReply;upsertReply(currentReply);pending=false;clearTimeout(timeout);syncState();if(currentReply)speak(currentReply);
  }else if(data.type==='activity'){
    if(pending)$('mood').textContent=data.status==='failed'?'这次没能完成':'正在处理你的请求';
  }else if(data.type==='error'){pending=false;clearTimeout(timeout);scheduleCaptionClear(12000);syncState();notice(data.text||data.message||data.error||'连接暂时出了点问题')}
};
events.onerror=()=>{connected=false;syncState()};
async function refreshStatus(){try{const r=await fetch('/api/status');const data=await r.json();connected=!!(data.connected??data.registered);syncState()}catch{connected=false;syncState()}}
function refreshVoice(){fetch(ioBase+'/status').then(r=>r.ok?r.json():null).then(data=>{voiceReady=!!data?.available;$('voice-status').textContent=voiceReady?'中文 · 本机语音已就绪':'本机声音尚未就绪，文字对话可用'}).catch(()=>{voiceReady=false;$('voice-status').textContent='本机声音尚未就绪'})}
refreshVoice();setInterval(refreshVoice,12000);
refreshStatus();setInterval(refreshStatus,12000);syncState();
window.addEventListener('pagehide',()=>{fetch(ioBase+'/focus',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({active:false}),keepalive:true}).catch(()=>{});stopAudio();events.close();keyEvents.close()});
$('stage').addEventListener('renderlost',()=>notice('角色显示中断，重新打开界面可恢复'));
Object.defineProperty(window,'museDiagnostics',{get:()=>({build:'pomtum-muse-0.1.0',fps:avatar?.fps,connected,pending,recording,voiceReady,speaking,audioPlaying:!!audio&&!audio.paused,audioTime:audio?.currentTime??0,audioLevel,mouth:avatar?.mouthShape,captionLength:$('caption').textContent?.length??0,captionVisible:!!$('caption').textContent,viewport:[innerWidth,innerHeight]})});

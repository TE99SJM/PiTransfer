'use strict';
const $ = id => document.getElementById(id);
let csrf = '', device = null, currentPath = '', offset = 0, nasReady = false, busy = false, polling = false;
let selected = new Set(), devices = [];
const activeStates = new Set(['queued', 'copying', 'verifying']);
function node(tag, text, cls) { const n = document.createElement(tag); if (text !== undefined) n.textContent = text; if (cls) n.className = cls; return n; }
function notice(message) { $('notice').textContent = message; $('notice').hidden = !message; }
function bytes(n) { if (!Number.isFinite(n)) return ''; const units = ['B','KiB','MiB','GiB','TiB']; let i = 0; while (n >= 1024 && i < 4) { n /= 1024; i++; } return `${n.toFixed(i ? 1 : 0)} ${units[i]}`; }
function loginView() { csrf = ''; $('login').hidden = false; $('dashboard').hidden = true; $('logout').hidden = true; }
function appView() { $('login').hidden = true; $('dashboard').hidden = false; $('logout').hidden = false; }
async function api(path, data) {
  const options = {credentials:'same-origin', cache:'no-store'};
  if (data !== undefined) { options.method = 'POST'; options.headers = {'Content-Type':'application/json', 'X-CSRF-Token':csrf}; options.body = JSON.stringify(data); }
  const r = await fetch('/api/' + path, options); const result = await r.json();
  if (!r.ok) { if (r.status === 401) loginView(); throw new Error(result.error || 'Request failed'); }
  return result;
}
async function act(fn) { try { notice(''); await fn(); } catch (e) { notice(e.message); } }
function button(text, fn, cls='quiet') { const b=node('button',text,cls); b.addEventListener('click',()=>act(fn)); return b; }
function controls() { $('copy-selected').disabled = !device || !selected.size || !nasReady || busy; $('copy-selected').textContent = selected.size ? `Copy selected (${selected.size})` : 'Copy selected'; $('copy-all').disabled = !device || !nasReady || busy; }
function renderDrives() {
  $('drives').replaceChildren();
  if (!devices.length) $('drives').append(node('p','No USB storage detected.','empty'));
  for (const d of devices) {
    const card=node('div',undefined,'drive' + (device === d.id ? ' active':''));
    card.append(node('h3',d.label),node('p',`${bytes(d.size)} · ${d.filesystem.toUpperCase()} · ${d.node}`));
    if (d.reason) card.append(node('p',d.reason));
    const row=node('div',undefined,'drive-actions');
    const choose=button('Select drive',async()=>{ device=d.id; currentPath=''; offset=0; selected.clear(); await browse(); renderDrives(); }); choose.disabled=!d.usable || busy;
    row.append(choose);
    if (d.mounted) { const eject=button('Unmount',async()=>{ const r=await api('eject',{device:d.id}); if(device===d.id) resetBrowser(); notice(r.message); await refresh(); }); eject.disabled=busy; row.append(eject); }
    card.append(row); $('drives').append(card);
  }
}
function resetBrowser(){device=null;selected.clear();$('files-title').textContent='Choose a drive';$('breadcrumb').textContent='File names and folders only';$('files').replaceChildren(node('p','Select a connected drive.','empty'));$('up').hidden=true;$('previous').hidden=true;$('next').hidden=true;controls();}
async function browse() {
  const data=await api('browse',{device,path:currentPath,offset});
  $('files-title').textContent=devices.find(d=>d.id===device)?.label || 'Drive';
  $('breadcrumb').textContent='/' + currentPath; $('up').hidden=!currentPath; $('previous').hidden=offset===0; $('next').hidden=!data.more;
  $('files').replaceChildren();
  if (!data.items.length) $('files').append(node('p','This folder is empty.','empty'));
  data.items.forEach((entry,i)=>{
    const path=currentPath ? currentPath+'/'+entry.name : entry.name;
    const row=node('div',undefined,'file'); const checkbox=node('input');checkbox.type='checkbox';checkbox.id='file-'+i;checkbox.checked=selected.has(path);checkbox.disabled=entry.kind==='unsupported';
    checkbox.addEventListener('change',()=>{checkbox.checked?selected.add(path):selected.delete(path);controls();});
    const label=node('label',entry.name);label.htmlFor=checkbox.id;
    row.append(checkbox,label);
    if(entry.kind==='folder') row.append(button('Browse',async()=>{currentPath=path;offset=0;await browse();}));
    else row.append(node('small',entry.kind==='unsupported'?'Skipped':bytes(entry.size)));
    $('files').append(row);
  });controls();
}
function renderJobs(jobs) {
  busy=jobs.some(j=>activeStates.has(j.state));$('jobs').replaceChildren();
  if(!jobs.length) $('jobs').append(node('p','Your transfers will appear here.','empty'));
  for(const j of jobs){
    const row=node('div',undefined,'job'), top=node('div',undefined,'job-top');
    const date=new Date(j.created).toLocaleString();top.append(node('strong',date),node('span',j.state.replaceAll('_',' '),'pill '+j.state));row.append(top);
    row.append(node('p',`${j.files} files verified · ${bytes(j.bytes)} copied${j.skipped ? ' · '+j.skipped+' skipped':''}`));
    if(j.current)row.append(node('p',j.current,'path'));if(j.destination)row.append(node('p',j.destination,'path'));if(j.error)row.append(node('p',j.error));
    if(activeStates.has(j.state))row.append(button('Cancel transfer',async()=>{await api('cancel',{id:j.id});notice('Cancellation requested. Waiting for the current disk operation to return.');},'danger'));
    $('jobs').append(row);
  }controls();
}
async function refresh() {
  const s=await api('status');devices=s.devices;nasReady=s.nas.ready;
  $('nas-title').textContent=nasReady?'NAS ready':'NAS unavailable';$('nas-detail').textContent=nasReady?`${s.nas.destination} · ${bytes(s.nas.free)} available`:s.nas.error;
  $('nas-status').textContent=nasReady?'Encrypted SMB3':'Not connected';
  if(device&&!devices.some(d=>d.id===device))resetBrowser();renderJobs(s.jobs);renderDrives();
}
async function transfer(paths) { const name=devices.find(d=>d.id===device)?.label || 'drive'; if(!confirm(`Copy ${paths[0]===''?'the entire drive':paths.length+' selected item(s)'} from ${name} to a NEW NAS folder? Originals will stay on the drive.`))return; await api('transfer',{device,paths});selected.clear();await refresh(); }
$('login-form').addEventListener('submit',event=>{event.preventDefault();act(async()=>{const r=await api('login',{password:$('password').value});$('password').value='';csrf=r.csrf;appView();await refresh();});});
$('logout').addEventListener('click',()=>act(async()=>{await api('logout',{});loginView();}));
$('refresh').addEventListener('click',()=>act(refresh));
$('up').addEventListener('click',()=>act(async()=>{currentPath=currentPath.split('/').slice(0,-1).join('/');offset=0;await browse();}));
$('previous').addEventListener('click',()=>act(async()=>{offset=Math.max(0,offset-200);await browse();}));
$('next').addEventListener('click',()=>act(async()=>{offset+=200;await browse();}));
$('copy-selected').addEventListener('click',()=>act(()=>transfer([...selected])));
$('copy-all').addEventListener('click',()=>act(()=>transfer([''])));
(async()=>{try{const s=await api('session');csrf=s.csrf;appView();await refresh();}catch(e){loginView();}})();
setInterval(async()=>{if(!csrf||polling)return;polling=true;try{await refresh();}catch(e){notice(e.message);}finally{polling=false;}},5000);

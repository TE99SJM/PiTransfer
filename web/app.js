'use strict';
const $ = id => document.getElementById(id);
let csrf = '', device = null, currentPath = '', offset = 0, nasReady = false, busy = false, polling = false;
let selected = new Set(), devices = [];
let sizing = false, serverSizing = false, preparing = false, sizeResult = null, sizeKey = '', sizeJobId = null, nasFree = null;
let nasShare = '', nasFolder = '', nasBrowsePath = '', nasOffset = 0, nasLoading = false;
const activeStates = new Set(['queued', 'copying', 'verifying']);
const sizeStates = new Set(['queued', 'counting']);
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
function locked() { return busy || sizing || serverSizing || preparing; }
function controls() {
  const blocked = locked();
  $('copy-selected').disabled = !device || !selected.size || !nasReady || blocked;
  $('copy-selected').textContent = selected.size ? `Copy selected (${selected.size})` : 'Copy selected';
  $('copy-all').disabled = !device || !nasReady || blocked;
  $('check-size').disabled = !device || blocked;
  $('cancel-size').hidden = !(sizing || serverSizing);
  $('cancel-size').disabled = !sizeJobId;
  $('nas-choose').disabled = !nasReady || preparing || nasLoading;
  $('nas-use').disabled = !nasReady || preparing || nasLoading;
  for(const id of ['nas-up','nas-previous','nas-next']) $(id).disabled=nasLoading;
  for(const b of $('nas-folders').querySelectorAll('button')) b.disabled=nasLoading;
  for (const id of ['up', 'previous', 'next']) $(id).disabled = blocked;
  for (const input of $('files').querySelectorAll('input')) input.disabled = blocked || input.dataset.unsupported === 'true';
  for (const b of $('files').querySelectorAll('button')) b.disabled = blocked;
  for (const b of $('drives').querySelectorAll('button')) b.disabled = blocked || b.dataset.usable === 'false';
}
function invalidateSize() {
  sizeResult = null; sizeKey = '';
  $('transfer-size').textContent = 'Transfer size not calculated';
  $('size-detail').textContent = selected.size ? 'Check size totals your selected files and folders.' : 'Check size totals all accessible files on this device.';
}
function destinationName(folder=nasFolder) { return nasShare + (folder ? '/'+folder : '') + '/Pi-Imports'; }
function renderDestination() {
  $('nas-detail').textContent=nasReady?`${destinationName()} · ${bytes(nasFree)} available`:($('nas-detail').textContent || 'NAS unavailable');
}
async function browseNas(path=nasBrowsePath, pageOffset=0) {
  nasLoading=true;controls();
  try {
    const result=await api('nas/browse',{path,offset:pageOffset});
    nasBrowsePath=result.path;nasOffset=result.offset;
    $('nas-current').textContent=nasShare + (nasBrowsePath?'/'+nasBrowsePath:'');
    $('nas-up').hidden=!nasBrowsePath;
    $('nas-previous').hidden=nasOffset===0;$('nas-next').hidden=!result.more;
    $('nas-folders').replaceChildren();
    if(!result.items.length) $('nas-folders').append(node('p','No subfolders here. You can use this folder.','empty'));
    for(const item of result.items) {
      const row=node('div',undefined,'file');
      const label=node('span',item.name,'nas-folder-name');
      row.append(label,button('Browse',()=>browseNas(nasBrowsePath?nasBrowsePath+'/'+item.name:item.name)));
      $('nas-folders').append(row);
    }
    if(path===nasFolder) {nasFree=result.free;renderDestination();}
  } finally {nasLoading=false;controls();}
}
function sizeText(e) {
  return `${e.unknown_files ? 'At least ' : ''}${bytes(e.bytes)}${e.unknown_files ? ` + ${e.unknown_files} file(s) with unknown size` : ''}`;
}
function renderEstimate(e) {
  const scope = e.paths.length === 1 && e.paths[0] === '' ? 'All files' : 'Selected items';
  if (sizeStates.has(e.state)) {
    $('transfer-size').textContent = `${scope}: counting… ${e.files} files · ${bytes(e.bytes)} so far`;
    $('size-detail').textContent = 'Reading file sizes only. No files are being copied.';
  } else if (e.state === 'complete') {
    $('transfer-size').textContent = `${scope}: ${e.files} file(s) · ${sizeText(e)}`;
    const short = Number.isFinite(nasFree) && e.bytes + 16 * 1024 * 1024 > nasFree;
    $('size-detail').textContent = `${e.bytes.toLocaleString()} known bytes${e.skipped ? ` · ${e.skipped} unsupported item(s) will be skipped` : ''}. ${short ? 'Not enough free space on the NAS.' : 'Estimate based on current file sizes; files may change before copying.'}`;
  } else {
    $('transfer-size').textContent = e.state === 'cancelled' ? 'Size calculation cancelled' : 'Size calculation failed';
    $('size-detail').textContent = e.error || 'Try calculating again.';
  }
}
async function calculateSize(paths, force = false) {
  const requestedDevice = device, key = JSON.stringify([requestedDevice, [...paths].sort()]);
  if (!force && sizeResult?.state === 'complete' && sizeKey === key && Date.now()/1000 - sizeResult.completed_at < 60) return sizeResult;
  sizing = true; sizeJobId = null; sizeResult = null; sizeKey = key; controls();
  $('transfer-size').textContent = 'Starting size calculation…';
  $('size-detail').textContent = 'Reading file sizes only. No files are being copied.';
  try {
    let e = await api('estimate', {device: requestedDevice, paths});
    sizeJobId = e.id; controls();
    while (true) {
      if (device !== requestedDevice) throw new Error('The selected device changed. Calculate again.');
      renderEstimate(e);
      if (!sizeStates.has(e.state)) break;
      await new Promise(resolve => setTimeout(resolve, 500));
      const status = await api('estimate');
      if (!status.estimate || status.estimate.id !== sizeJobId) throw new Error('Another size calculation replaced this one. Calculate again.');
      e = status.estimate;
    }
    if (e.state !== 'complete') throw new Error(e.error || 'Size calculation did not finish.');
    sizeResult = e;
    return e;
  } catch (error) {
    $('transfer-size').textContent = 'Transfer size unavailable';
    $('size-detail').textContent = error.message;
    throw error;
  } finally {
    sizing = false; serverSizing = false; sizeJobId = null; controls();
  }
}
function renderDrives() {
  $('drives').replaceChildren();
  if (!devices.length) $('drives').append(node('p','No USB drives or supported cameras detected.','empty'));
  for (const d of devices) {
    const card=node('div',undefined,'drive' + (device === d.id ? ' active':''));
    card.append(node('h3',d.label),node('p',[bytes(d.size),d.filesystem.toUpperCase(),d.node].filter(Boolean).join(' · ')));
    if (d.reason) card.append(node('p',d.reason));
    const row=node('div',undefined,'drive-actions');
    const choose=button(d.kind==='camera'?'Select camera':'Select drive',async()=>{ device=d.id; currentPath=''; offset=0; selected.clear(); invalidateSize(); await browse(); renderDrives(); }); choose.dataset.usable=String(d.usable); choose.disabled=!d.usable || locked();
    row.append(choose);
    if (d.mounted) { const eject=button('Unmount',async()=>{ const r=await api('eject',{device:d.id}); if(device===d.id) resetBrowser(); notice(r.message); await refresh(); }); eject.disabled=locked(); row.append(eject); }
    card.append(row); $('drives').append(card);
  }
}
function resetBrowser(){device=null;selected.clear();invalidateSize();$('files-title').textContent='Choose a device';$('breadcrumb').textContent='File names and folders only';$('files').replaceChildren(node('p','Select a connected drive or camera.','empty'));$('up').hidden=true;$('previous').hidden=true;$('next').hidden=true;controls();}
async function browse() {
  const data=await api('browse',{device,path:currentPath,offset});
  $('files-title').textContent=devices.find(d=>d.id===device)?.label || 'Drive';
  $('breadcrumb').textContent='/' + currentPath; $('up').hidden=!currentPath; $('previous').hidden=offset===0; $('next').hidden=!data.more;
  $('files').replaceChildren();
  if (!data.items.length) $('files').append(node('p','This folder is empty.','empty'));
  data.items.forEach((entry,i)=>{
    const path=currentPath ? currentPath+'/'+entry.name : entry.name;
    const row=node('div',undefined,'file'); const checkbox=node('input');checkbox.type='checkbox';checkbox.id='file-'+i;checkbox.checked=selected.has(path);checkbox.dataset.unsupported=String(entry.kind==='unsupported');checkbox.disabled=entry.kind==='unsupported' || locked();
    checkbox.addEventListener('change',()=>{checkbox.checked?selected.add(path):selected.delete(path);invalidateSize();controls();});
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
    if(Number.isFinite(j.total_bytes)) {
      row.append(node('p',`Estimated total: ${j.total_files} file(s) · ${sizeText({bytes:j.total_bytes,unknown_files:j.unknown_files||0})}`));
      if(!j.unknown_files && j.total_bytes > 0) {
        const progress=node('progress');progress.max=j.total_bytes;progress.value=Math.min(j.bytes,j.total_bytes);progress.setAttribute('aria-label','Data copied; checksum verification continues separately');row.append(progress);
      }
    }
    if(j.current)row.append(node('p',j.current,'path'));if(j.destination)row.append(node('p',j.destination,'path'));if(j.error)row.append(node('p',j.error));
    if(activeStates.has(j.state))row.append(button('Cancel transfer',async()=>{await api('cancel',{id:j.id});notice('Cancellation requested. Waiting for the current disk operation to return.');},'danger'));
    $('jobs').append(row);
  }controls();
}
async function refresh() {
  const s=await api('status');devices=s.devices;nasReady=s.nas.ready;
  if(nasReady) {
    nasShare=s.nas.share || s.nas.destination.replace(/\/Pi-Imports$/,'');
    if(!nasFolder || nasFree===null) nasFree=s.nas.free;
  } else nasFree=null;
  serverSizing=Boolean(s.estimate && sizeStates.has(s.estimate.state));
  if(!sizing) {
    sizeJobId=serverSizing?s.estimate.id:null;
    if(serverSizing) renderEstimate(s.estimate);
    else if(sizeResult) renderEstimate(sizeResult);
  }
  $('nas-title').textContent=nasReady?'NAS ready':'NAS unavailable';
  if(nasReady) renderDestination(); else $('nas-detail').textContent=s.nas.error;
  $('nas-status').textContent=nasReady?'Encrypted SMB3':'Not connected';
  $('camera-status').textContent=s.camera_error || 'Cameras: choose MTP, PTP, or File Transfer mode. Webcam and charging modes do not expose stored files.';
  if(device&&!devices.some(d=>d.id===device))resetBrowser();renderJobs(s.jobs);renderDrives();
}
async function transfer(paths) {
  const requestedDevice=device, requestedFolder=nasFolder, name=devices.find(d=>d.id===device)?.label || 'device';
  preparing=true;controls();
  try {
    const estimate=await calculateSize(paths);
    await refresh();
    if(device!==requestedDevice) throw new Error('The device changed. Select it again.');
    if(!nasReady) throw new Error('The NAS is unavailable.');
    const destination=await api('nas/browse',{path:requestedFolder});
    nasFree=destination.free;renderDestination();renderEstimate(estimate);
    if(estimate.bytes+16*1024*1024>nasFree) throw new Error('Not enough free space on the NAS for this selection.');
    const skipped=estimate.skipped?`\n${estimate.skipped} unsupported item(s) will be skipped.`:'';
    if(!confirm(`Copy ${estimate.files} file(s), ${sizeText(estimate)}, from ${name} to a NEW folder inside ${destinationName(requestedFolder)}?${skipped}\nOriginals will stay on the device.`)) return;
    await api('transfer',{device:requestedDevice,paths,estimate_id:estimate.id,destination_folder:requestedFolder});
    selected.clear();invalidateSize();
    for(const input of $('files').querySelectorAll('input')) input.checked=false;
    notice('Transfer started on the Pi. You can close this window and check progress later.');
    await refresh();
  } finally {preparing=false;controls();}
}
$('login-form').addEventListener('submit',event=>{event.preventDefault();act(async()=>{const r=await api('login',{password:$('password').value});$('password').value='';csrf=r.csrf;appView();await refresh();});});
$('logout').addEventListener('click',()=>act(async()=>{await api('logout',{});loginView();}));
$('refresh').addEventListener('click',()=>act(refresh));
$('up').addEventListener('click',()=>act(async()=>{currentPath=currentPath.split('/').slice(0,-1).join('/');offset=0;await browse();}));
$('previous').addEventListener('click',()=>act(async()=>{offset=Math.max(0,offset-200);await browse();}));
$('next').addEventListener('click',()=>act(async()=>{offset+=200;await browse();}));
$('copy-selected').addEventListener('click',()=>act(()=>transfer([...selected])));
$('copy-all').addEventListener('click',()=>act(()=>transfer([''])));
$('check-size').addEventListener('click',()=>act(()=>calculateSize(selected.size?[...selected]:[''],true)));
$('cancel-size').addEventListener('click',()=>act(async()=>{if(sizeJobId)await api('estimate/cancel',{id:sizeJobId});}));
$('nas-choose').addEventListener('click',()=>act(async()=>{$('nas-browser').hidden=false;await browseNas(nasFolder);}));
$('nas-close').addEventListener('click',()=>{$('nas-browser').hidden=true;});
$('nas-up').addEventListener('click',()=>act(()=>browseNas(nasBrowsePath.split('/').slice(0,-1).join('/'))));
$('nas-previous').addEventListener('click',()=>act(()=>browseNas(nasBrowsePath,Math.max(0,nasOffset-200))));
$('nas-next').addEventListener('click',()=>act(()=>browseNas(nasBrowsePath,nasOffset+200)));
$('nas-use').addEventListener('click',()=>act(async()=>{
  const checked=await api('nas/browse',{path:nasBrowsePath});
  nasFolder=checked.path;nasFree=checked.free;renderDestination();if(sizeResult)renderEstimate(sizeResult);
  $('nas-browser').hidden=true;
}));
(async()=>{try{const s=await api('session');csrf=s.csrf;appView();await refresh();}catch(e){loginView();}})();
setInterval(async()=>{if(!csrf||polling)return;polling=true;try{await refresh();}catch(e){notice(e.message);}finally{polling=false;}},5000);

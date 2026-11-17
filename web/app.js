'use strict';
const $ = id => document.getElementById(id);
let csrf = '', device = null, currentPath = '', offset = 0, nasReady = false, busy = false, polling = false;
let selected = new Set(), devices = [], jobsState = [], jobsPolling = false;
let sizing = false, serverSizing = false, preparing = false, sizeResult = null, sizeKey = '', sizeJobId = null, nasFree = null;
let nasShare = '', nasFolder = '', nasBrowsePath = '', nasOffset = 0, nasLoading = false;
const activeStates = new Set(['queued', 'starting', 'counting', 'copying', 'verifying', 'resuming', 'finalizing', 'organizing', 'deleting']);
const importTimezone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
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
  for (const b of $('drives').querySelectorAll('button')) {
    b.disabled = b.classList.contains('queue-device')
      ? (!nasReady || sizing || serverSizing || preparing || jobsState.some(j=>j.device===b.dataset.device && activeStates.has(j.state)))
      : (blocked || b.dataset.usable === 'false');
  }
  for (const b of $('jobs').querySelectorAll('.resume-transfer, .delete-originals, .organize-transfer')) b.disabled = blocked || !nasReady;
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
    if(busy && d.usable) {
      const queued=jobsState.some(j=>j.device===d.id && activeStates.has(j.state));
      const queue=button(queued?'In transfer list':'Queue all files',()=>queueDevice(d),'secondary queue-device');
      queue.dataset.device=d.id;queue.disabled=queued||!nasReady||preparing||sizing||serverSizing;
      row.append(queue);
    }
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
function jobProgress(j) {
  if(j.state==='deleting') {
    const c=j.cleanup||{},total=c.total_bytes||0;
    return {percent:total?Math.min(99.9,(c.phase_bytes||0)/total*100):null,title:'Rechecking and deleting originals',detail:`${c.files||0} of ${c.total_files||0} originals deleted · ${bytes(c.phase_bytes||0)} checked`};
  }
  if(j.state==='organizing') return {percent:j.phase_total?Math.min(99.9,(j.phase_bytes||0)/j.phase_total*100):null,title:'Organizing by camera and date',detail:`${j.published_files||0} of ${j.files} files placed in date folders`};
  const done=['complete','complete_with_skips'].includes(j.state), stopped=['failed','cancelled','interrupted'].includes(j.state);
  const clamp=n=>Math.max(0,Math.min(done?100:99.9,n));
  if(done) return {percent:100,title:'Complete',detail:`${bytes(j.bytes)} · ${j.files} files verified`};
  if(j.state==='queued') return {percent:0,title:`Waiting${j.queue_position?' · queue position '+j.queue_position:''}`,detail:'Starts automatically after earlier transfers. Keep this device connected.'};
  let percent=null,title='Preparing transfer',detail='';
  if(j.state==='counting') {title='Counting files';detail=`${j.total_files||0} files · ${bytes(j.total_bytes||0)} found so far`;}
  else if(j.phase==='rechecking' || (j.copy_complete && j.state==='resuming')) {
    const total=j.phase_total ?? j.bytes, current=j.phase_bytes ?? j.reused_bytes ?? 0;
    title='Rechecking saved data';detail=`${bytes(current)} of ${bytes(total)} checked`;
    if(total>0) percent=clamp(current/total*100);
  } else if(Number.isFinite(j.total_bytes) && j.total_bytes>0 && !j.unknown_files) {
    title='Copying and verifying';detail=`${bytes(j.bytes)} of ${bytes(j.total_bytes)} copied · ${bytes(j.checked_bytes||0)} checked`;
    percent=clamp((j.bytes+(j.checked_bytes||0))/(2*j.total_bytes)*100);
  } else if(Number.isFinite(j.total_files) && j.total_files>0) {
    title='Files verified';detail=`${j.files} of ${j.total_files} files · ${bytes(j.bytes)} copied · some sizes unknown`;
    percent=clamp(j.files/j.total_files*100);
  } else {title='Working · total size unknown';detail=`${bytes(j.bytes)} copied · ${j.files} files verified`;}
  if(j.state==='finalizing') {title='Finishing transfer folder';percent=99.9;}
  if(stopped) title=j.state==='failed'?'Needs attention':j.state==='cancelled'?'Cancelled':'Interrupted';
  return {percent,title,detail,stopped};
}
function renderJobs(jobs) {
  jobsState=jobs;
  busy=jobs.some(j=>activeStates.has(j.state));$('jobs').replaceChildren();
  if(!jobs.length) $('jobs').append(node('p','Your transfers will appear here.','empty'));
  for(const j of jobs){
    const row=node('div',undefined,'job'), top=node('div',undefined,'job-top');row.dataset.jobId=j.id;
    const date=new Date(j.created).toLocaleString(), label=j.device_label || devices.find(d=>d.id===j.device)?.label || (j.device?.startsWith('camera:')?'USB camera':'USB drive');
    top.append(node('strong',label),node('span',j.state.replaceAll('_',' '),'pill '+j.state));row.append(top);
    row.append(node('p',`${date} · ${j.device_node || j.device}`,'path'));
    const state=jobProgress(j), summary=node('div',undefined,'progress-heading');
    summary.append(node('strong',state.title),node('span',state.percent===null?'':`${state.percent===100?'100':state.percent.toFixed(1)}%`));
    row.append(summary);
    const progress=node('progress',undefined,'transfer-progress');progress.max=100;
    if(state.percent!==null || state.stopped) progress.value=state.percent??0;
    progress.setAttribute('aria-label',`${label}: ${state.title}`);
    progress.setAttribute('aria-valuetext',state.detail || state.title);row.append(progress,node('p',state.detail,'progress-detail'));
    row.append(node('p',`${j.files} files verified · ${bytes(j.bytes)} copied${j.skipped ? ' · '+j.skipped+' skipped':''}`));
    if(j.reused_files) row.append(node('p',`${j.reused_files} saved files rechecked · ${bytes(j.reused_bytes)} reused`));
    if(Number.isFinite(j.total_bytes)) {
      row.append(node('p',`Estimated total: ${j.total_files} file(s) · ${sizeText({bytes:j.total_bytes,unknown_files:j.unknown_files||0})}`));
    }
    if(j.current)row.append(node('p',j.current,'path'));if(j.output_folders?.length) { for(const folder of j.output_folders) row.append(node('p',folder,'path')); } else if(j.destination)row.append(node('p',j.destination,'path'));if(j.error)row.append(node('p',j.error));
    if(activeStates.has(j.state))row.append(button(j.state==='queued'?'Remove from queue':j.state==='deleting'?'Stop deleting':'Cancel transfer',async()=>{await api('cancel',{id:j.id});notice(j.state==='queued'?'Removed from queue.':'Cancellation requested. Waiting for the current disk operation to return.');await refresh();},'danger'));
    if(j.can_resume) {
      row.append(node('p',j.resume_needs_source?'Resume checks saved files and retries unfinished files. Select the original camera or drive first.':'All files were copied. Resume rechecks the NAS copies and finishes the transfer; the source is not needed.'));
      row.append(button('Resume transfer',()=>resumeTransfer(j),'secondary resume-transfer'));
    } else if(['failed','cancelled','interrupted'].includes(j.state)) {
      row.append(node('p','This older transfer has no saved selection for resuming. Its NAS files are retained.'));
    }
    if(j.can_organize) row.append(button('Organize by date',()=>organizeTransfer(j),'secondary organize-transfer'));
    if(j.can_delete) row.append(button('Delete verified originals',()=>deleteOriginals(j),'danger delete-originals'));
    if(j.cleanup) {
      const c=j.cleanup;
      row.append(node('p',`Original deletion: ${c.state} · ${c.files||0} of ${c.total_files||0} deleted in this operation`,'cleanup-detail'));
      if(c.current && j.state==='deleting') row.append(node('p',c.current,'path'));
      if(c.error) row.append(node('p',c.error,'cleanup-error'));
    }
    $('jobs').append(row);
  }controls();
}
async function queueDevice(d) {
  preparing=true;controls();
  try {
    if(!confirm(`Queue all accessible files from ${d.label} into camera_date folders inside ${destinationName()}?\nThe Pi copies one device at a time. Size is checked when this transfer starts. Keep the device connected. Originals stay unchanged.`)) return;
    await api('queue',{device:d.id,destination_folder:nasFolder,import_timezone:importTimezone});
    notice(`${d.label} added to the transfer queue. You can close this window.`);
    await refresh();
  } finally {preparing=false;controls();}
}
async function resumeTransfer(job) {
  preparing=true;controls();
  try {
    const source=job.resume_needs_source?(device || job.device):null;
    if(job.resume_needs_source && !devices.some(d=>d.id===source && d.usable)) throw new Error('Reconnect and select the original camera or drive, then press Resume transfer.');
    const name=devices.find(d=>d.id===source)?.label || 'source';
    const message=job.resume_needs_source?`Resume using ${name}? Make sure this is the original camera/card. Saved files are checked against the source and NAS before reuse. Any unfinished file restarts from its beginning.`:'Recheck the saved NAS files and finish this transfer? No files will be downloaded from the camera or drive again.';
    if(!confirm(message+'\nThe existing transfer folder will be reused.')) return;
    await api('resume',{id:job.id,device:source});
    notice('Resume started on the Pi. You can close this window and check progress later.');
    await refresh();
  } finally {preparing=false;controls();}
}
async function organizeTransfer(job) {
  preparing=true;controls();
  try {
    if(!confirm(`Move this completed import into camera_date folders inside ${destinationName(job.destination_folder||'')}?\nEach NAS file is checked before moving. Existing filenames are protected. Recording dates use ${importTimezone}; missing dates fall back to the file timestamp or Unknown-date.`)) return;
    await api('organize',{id:job.id,import_timezone:importTimezone});
    notice('Organizing on the Pi. You can close this window.');await refresh();
  } finally {preparing=false;controls();}
}
async function deleteOriginals(job) {
  preparing=true;controls();
  try {
    const source=device || job.device;
    if(!devices.some(d=>d.id===source && d.usable)) throw new Error('Reconnect and select the original camera or drive, then try again.');
    const prepared=await api('delete/prepare',{id:job.id,device:source});
    const answer=prompt(`Permanently delete ${prepared.files} verified originals (${bytes(prepared.bytes)}) from ${prepared.source}?\nThe Pi rechecks each original and its NAS copy before deletion. Changed or missing files stop the operation. NAS copies are kept.\nType ${prepared.confirmation} to confirm.`);
    if(answer===null) return;
    if(answer!==prepared.confirmation) throw new Error('Confirmation did not match. Nothing was deleted.');
    await api('delete/confirm',{token:prepared.token,confirmation:answer});
    notice('Verification and original deletion started on the Pi. Stop deleting prevents later files from being deleted.');await refresh();
  } finally {preparing=false;controls();}
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
    if(!confirm(`Copy ${estimate.files} file(s), ${sizeText(estimate)}, from ${name} into camera_date folders inside ${destinationName(requestedFolder)}?${skipped}\nDifferent recording dates get separate folders. Date timezone: ${importTimezone}.\nOriginals will stay on the device.`)) return;
    await api('transfer',{device:requestedDevice,paths,estimate_id:estimate.id,destination_folder:requestedFolder,import_timezone:importTimezone});
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
setInterval(async()=>{if(!csrf||jobsPolling||polling)return;jobsPolling=true;try{const r=await api('jobs');renderJobs(r.jobs);}catch(e){notice(e.message);}finally{jobsPolling=false;}},1000);

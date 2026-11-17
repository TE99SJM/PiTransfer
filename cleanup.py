"""Explicit, per-transfer original deletion after fresh source/NAS verification."""
import contextlib
import hashlib
import json
import os
import secrets
import stat
import threading
import time

COMPLETE = {'complete', 'complete_with_skips'}


@contextlib.contextmanager
def inventory(engine, job):
    from core import READ, directory
    from organize import load_plan
    prefix, _, final = engine.destination_parts(job)
    if job['destination'] != prefix + final:
        raise ValueError('Only a finalized transfer can authorize deletion')
    with contextlib.ExitStack() as stack:
        nas = engine.hardware.nas(); stack.callback(os.close, nas)
        workspace = stack.enter_context(directory(nas, job['destination']))
        manifest = stack.enter_context(os.fdopen(os.open('checksums.jsonl', READ, dir_fd=workspace), 'rb'))
        info = os.fstat(manifest.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('Invalid verification manifest')
        records, _ = engine.read_manifest(manifest)
        verified = {p:r for p,r in records.items() if r['status']=='verified'}
        if (len(verified),sum(r['size'] for r in verified.values()),len(records)-len(verified)) != (job['files'],job['bytes'],job['skipped']):
            raise ValueError('Verification records do not match this transfer')
        if job.get('layout') == 'camera_date':
            if not job.get('publication_complete'):
                raise ValueError('Finish organizing before deleting originals')
            plan = load_plan(workspace,job,records)
            outputs = {e['source']:prefix+e['output'] for e in plan['files']}
        else:
            outputs = {p:job['destination']+'/data/'+p for p in verified}
        deleted = set()
        try:
            with os.fdopen(os.open('deleted.jsonl',READ,dir_fd=workspace),'rb') as audit:
                info=os.fstat(audit.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size>64*1024**2:
                    raise ValueError('Invalid deletion audit')
                for line in audit:
                    if len(line)>65536 or not line.endswith(b'\n'):
                        raise ValueError('Deletion audit is incomplete; review the last file before proceeding')
                    entry=json.loads(line); record=verified.get(entry.get('path'))
                    if (entry.get('status')!='deleted' or not record or entry['path'] in deleted
                            or (entry.get('size'),entry.get('sha256')) != (record['size'],record['sha256'])):
                        raise ValueError('Deletion audit does not match verified files')
                    deleted.add(entry['path'])
        except FileNotFoundError:
            pass
        entries=[dict(r,backup_path=outputs[p]) for p,r in verified.items() if p not in deleted]
        signature=hashlib.sha256(json.dumps([job['destination'],entries],sort_keys=True).encode()).hexdigest()
        yield nas, workspace, entries, deleted, signature


def choose(engine, job_id):
    job=next((j for j in engine.jobs if j['id']==job_id),None)
    if not job or job['state'] not in COMPLETE:
        raise ValueError('Only completed, verified transfers can delete originals')
    return job


def prepare(engine, job_id, device=None):
    if not engine.acquire_operation():
        raise RuntimeError('Wait for the current drive operation to finish')
    try:
        engine.cancel_event.clear()
        job=choose(engine,job_id)
        source=device or job['device']
        if not isinstance(source,str) or source.startswith('camera:') != job['device'].startswith('camera:'):
            raise ValueError('Select the original camera or drive for this transfer')
        with inventory(engine,job) as (_,_,entries,deleted,signature):
            if not entries: raise ValueError('All verified originals in this transfer were already deleted')
            count=len(entries); size=sum(e['size'] for e in entries)
        token=secrets.token_urlsafe(32)
        with engine.lock:
            engine.delete_tokens={k:v for k,v in engine.delete_tokens.items() if v['expires']>time.monotonic()}
            if len(engine.delete_tokens)>=32: engine.delete_tokens.pop(next(iter(engine.delete_tokens)))
            engine.delete_tokens[token]=dict(job_id=job_id,device=source,signature=signature,count=count,
                                              expires=time.monotonic()+300)
        label=next((d['label'] for d in engine.device_cache if d['id']==source),job.get('device_label','original device'))
        return dict(token=token,id=job_id,files=count,bytes=size,source=label,confirmation=f'DELETE {count}')
    finally:
        engine.release_operation()


def start(engine, token, confirmation):
    if not isinstance(token,str) or not isinstance(confirmation,str): raise ValueError('Invalid deletion confirmation')
    if not engine.acquire_operation(): raise RuntimeError('Wait for the current drive operation to finish')
    try:
        with engine.lock:
            request=engine.delete_tokens.pop(token,None)
            if not request or request['expires']<time.monotonic() or confirmation!=f'DELETE {request["count"]}':
                raise ValueError('Deletion confirmation is incorrect or expired; prepare it again')
            job=choose(engine,request['job_id'])
            engine.cancel_event.clear()
            job['cleanup_previous_state']=job['state']
            job.update(state='deleting',cleanup={'state':'checking','files':0,'total_files':request['count'],
                                                'bytes':0,'phase_bytes':0,'error':'','current':''})
            engine.active_job=job
            engine.save()
        threading.Thread(target=run,args=(engine,job,request),daemon=True).start()
        return dict(job)
    except Exception:
        engine.release_operation(transfer=True)
        raise


def run(engine,job,request):
    from core import READ, Cancelled, directory, parts
    cleanup=job['cleanup']
    def progress(amount):
        with engine.lock: cleanup['phase_bytes']+=amount
    try:
        with inventory(engine,job) as (nas,workspace,entries,deleted,signature):
            if signature!=request['signature'] or len(entries)!=request['count']:
                raise ValueError('Verified file list changed; confirm deletion again')
            flags=os.O_WRONLY|os.O_APPEND|os.O_CREAT|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC
            with os.fdopen(os.open('deleted.jsonl',flags,0o600,dir_fd=workspace),'ab') as audit:
                info=os.fstat(audit.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:
                    raise ValueError('Invalid deletion audit')
                cleanup['total_bytes']=2*sum(e['size'] for e in entries)
                source=engine.hardware.camera_source(request['device']) if request['device'].startswith('camera:') else None
                for entry in entries:
                    engine.check()
                    cleanup.update(current=entry['path'],state='verifying')
                    engine.checkpoint(job)
                    names=parts(entry['backup_path'])
                    # Keep the backup parent pinned throughout source verification/deletion.
                    with directory(nas,'/'.join(names[:-1])) as parent:
                        def verify_backup():
                            before=os.stat(names[-1],dir_fd=parent,follow_symlinks=False)
                            if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1:
                                raise ValueError('NAS backup is no longer a regular, unique file')
                            if engine.hash_file(parent,names[-1],progress)!=(entry['size'],entry['sha256']):
                                raise RuntimeError('NAS copy no longer matches; original was kept: '+entry['path'])
                            after=os.stat(names[-1],dir_fd=parent,follow_symlinks=False)
                            if (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns):
                                raise RuntimeError('NAS copy changed during verification; original was kept')
                            engine.check()
                            cleanup['state']='deleting'
                        if source:
                            source.delete_verified(entry['path'],entry,verify_backup,engine.check,progress)
                        else:
                            # The root helper validates and hashes both files itself,
                            # then briefly remounts only this managed source for unlink.
                            engine.hardware.delete_verified(request['device'],entry,engine.check,progress)
                    record={k:entry[k] for k in ('path','size','sha256')}
                    record.update(status='deleted',at=time.time(),device=request['device'])
                    engine.write_record(audit,record)
                    cleanup.update(files=cleanup['files']+1,bytes=cleanup['bytes']+entry['size'])
                    engine.checkpoint(job)
        cleanup.update(state='complete',current='')
    except Cancelled:
        cleanup.update(state='cancelled',error='Deletion stopped. Files already deleted are recorded; remaining originals were kept.')
    except Exception as exc:
        cleanup.update(state='failed',error=str(exc)[:800])
    finally:
        try:
            with engine.lock:
                job['state']=job.pop('cleanup_previous_state','complete')
                engine.save()
        finally:
            engine.release_operation(transfer=True)

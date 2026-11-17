"""Publish verified imports into flat camera/date folders, with restart recovery."""
import ctypes
import datetime as dt
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import unicodedata
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PLAN_LIMIT = 64 * 1024 * 1024


def timezone_name(value):
    if not isinstance(value, str) or len(value) > 100:
        raise ValueError('Invalid date timezone')
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError('Choose a valid timezone for recording dates') from None
    return value


def camera_name(value):
    text = unicodedata.normalize('NFKD', str(value)).encode('ascii', 'ignore').decode()
    text = re.sub(r'[^A-Za-z0-9_-]+', '_', text).strip('_-')[:70]
    if not text or text.upper() in {'CON','PRN','AUX','NUL',*[f'COM{i}' for i in range(10)],*[f'LPT{i}' for i in range(10)]}:
        return 'Camera'
    return text


def read_metadata(fd):
    try:
        result = subprocess.run([sys.executable, '-I', str(Path(__file__).with_name('metadata_worker.py')), str(fd)],
                                pass_fds=(fd,), capture_output=True, timeout=20)
        if result.returncode or len(result.stdout) > 32768:
            return {}
        value = json.loads(result.stdout)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}


def date_and_camera(metadata, mtime, label, zone='UTC'):
    zone = ZoneInfo(timezone_name(zone))
    model = next((v for tag in ('Model','CameraModelName') for k,v in metadata.items()
                  if k.split(':')[-1] == tag and isinstance(v,str) and v.strip()), label)
    make=next((v for k,v in metadata.items() if k.split(':')[-1]=='Make' and isinstance(v,str) and v.strip()),'')
    if make and not str(model).lower().startswith(make.lower()): model=make+' '+str(model)
    for tag in ('SubSecDateTimeOriginal','DateTimeOriginal','CreationDate','CreateDate','MediaCreateDate','TrackCreateDate'):
        for key,value in metadata.items():
            if key.split(':')[-1] != tag or not isinstance(value,str):
                continue
            text = re.sub(r'^(\d{4}):(\d{2}):(\d{2})', r'\1-\2-\3', value.strip())
            try:
                stamp = dt.datetime.fromisoformat(text.replace('Z','+00:00'))
                if stamp.year < 1971:
                    continue
                if stamp.tzinfo:
                    stamp = stamp.astimezone(zone)
                return stamp.date().isoformat(), camera_name(model), key
            except (ValueError, OverflowError):
                continue
    if isinstance(mtime,(int,float)) and mtime > 0:
        try:
            stamp = dt.datetime.fromtimestamp(mtime, zone)
            if stamp.year >= 1971:
                return stamp.date().isoformat(), camera_name(model), 'file timestamp'
        except (ValueError, OverflowError, OSError):
            pass
    return 'Unknown-date', camera_name(model), 'unknown'


def rename_exclusive(src_fd, src_name, dst_fd, dst_name):
    """Linux atomic rename that refuses even an empty existing destination."""
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, 'renameat2', None)
    if rename is None:
        raise RuntimeError('This system lacks safe no-overwrite rename support')
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(src_fd, os.fsencode(src_name), dst_fd, os.fsencode(dst_name), 1):
        error = ctypes.get_errno()
        if error in (errno.ENOSYS, errno.EOPNOTSUPP, errno.EINVAL):
            raise RuntimeError('The NAS does not support safe no-overwrite moves; verified files were retained')
        raise OSError(error, os.strerror(error), dst_name)


def sync_directory(fd):
    # Some SMB servers do not implement directory fsync. File data/plan fsync
    # and synchronous rename replies still precede subsequent operations.
    try: os.fsync(fd)
    except OSError as exc:
        if exc.errno not in (errno.EINVAL, errno.EOPNOTSUPP): raise


def exists(parent, name):
    try:
        return os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return None


def validate_output(value):
    if not isinstance(value,str) or len(value)>512:
        raise ValueError('Invalid organized output')
    names = value.split('/')
    if (len(names)!=2 or any(n in ('','.','..') or '\x00' in n or '\\' in n for n in names)
            or not re.fullmatch(r'[A-Za-z0-9_-]+_(?:\d{4}-\d{2}-\d{2}|Unknown-date)', names[0])):
        raise ValueError('Invalid organized output path')
    return names


def load_plan(workspace, job, records):
    from core import READ
    with os.fdopen(os.open('publish.json', READ, dir_fd=workspace),'rb') as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > PLAN_LIMIT or info.st_nlink!=1:
            raise ValueError('Invalid publication plan')
        plan = json.load(file)
    expected = {p:r for p,r in records.items() if r['status']=='verified'}
    if not isinstance(plan,dict) or plan.get('job_id')!=job['id'] or not isinstance(plan.get('files'),list) or len(plan['files'])!=len(expected):
        raise ValueError('Publication plan does not match the transfer')
    seen,outputs=set(),set()
    for entry in plan['files']:
        if not isinstance(entry,dict): raise ValueError('Invalid publication record')
        path=entry.get('source')
        record=expected.get(path)
        validate_output(entry.get('output'))
        key=entry['output'].casefold()
        if not record or path in seen or key in outputs or (entry.get('size'),entry.get('sha256'))!=(record['size'],record['sha256']):
            raise ValueError('Invalid publication record')
        seen.add(path);outputs.add(key)
    return plan


def build_plan(engine, imports, workspace, data, job, records):
    from core import READ, WRITE, directory, parts
    files,used=[],set()
    provider=getattr(engine.hardware,'file_metadata',read_metadata)
    for path,record in records.items():
        engine.check()
        if record['status']!='verified':
            continue
        names=parts(path)
        with directory(data,'/'.join(names[:-1])) as parent:
            with os.fdopen(os.open(names[-1],READ,dir_fd=parent),'rb') as media:
                info=os.fstat(media.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError('Organizing requires regular copied files')
                metadata=provider(media.fileno())
        date,camera,basis=date_and_camera(metadata,record.get("source_mtime",info.st_mtime),job.get('camera_name') or job.get('device_label','Camera'),job.get('import_timezone','UTC'))
        folder=camera+'_'+date
        try: os.mkdir(folder,0o700,dir_fd=imports)
        except FileExistsError: pass
        # Keep a safe basename; a deterministic suffix preserves collisions.
        basename=re.sub(r'[<>:"/\\|?*\x00-\x1f]','_',names[-1]).rstrip(' .') or 'file'
        if basename.split('.')[0].upper() in {'CON','PRN','AUX','NUL',*[f'COM{i}' for i in range(10)],*[f'LPT{i}' for i in range(10)]}: basename='_'+basename
        while len(basename.encode('utf-8'))>180: basename=basename[:-1]
        with directory(imports,folder) as target:
            candidate=basename
            collision=0
            while (folder+'/'+candidate).casefold() in used or exists(target,candidate):
                collision+=1
                stem,suffix=os.path.splitext(basename)
                token=hashlib.sha256(path.encode()).hexdigest()[:8]
                candidate=f'{stem}__{job["id"]}_{token}'+(f'_{collision}' if collision>1 else '')+suffix
            output=folder+'/'+candidate
        used.add(output.casefold())
        files.append(dict(source=path,output=output,size=record['size'],sha256=record['sha256'],date_source=basis))
        engine.update(job,current=path)
    plan={'job_id':job['id'],'files':files,'timezone':job.get('import_timezone','UTC')}
    payload=json.dumps(plan,ensure_ascii=True).encode()
    if len(payload)>PLAN_LIMIT: raise ValueError('Publication plan is too large')
    temporary='publish-'+os.urandom(6).hex()+'.tmp'
    with os.fdopen(os.open(temporary,WRITE,0o600,dir_fd=workspace),'wb') as file:
        file.write(payload);file.flush();os.fsync(file.fileno())
    rename_exclusive(workspace,temporary,workspace,'publish.json')
    sync_directory(workspace)
    return plan


def publish(engine, imports, workspace, data, manifest, job):
    from core import directory, parts
    manifest.seek(0)
    records,_=engine.read_manifest(manifest)
    files=[r for r in records.values() if r['status']=='verified']
    if (len(files),sum(r['size'] for r in files),len(records)-len(files))!=(job['files'],job['bytes'],job['skipped']):
        raise RuntimeError('Copied-file manifest does not match this completed transfer')
    engine.update(job,state='organizing',phase='organizing',phase_bytes=0,phase_total=job['bytes'],published_files=0)
    try: plan=load_plan(workspace,job,records)
    except FileNotFoundError: plan=build_plan(engine,imports,workspace,data,job,records)
    prefix=job['destination'].rsplit('/',1)[0]+'/'
    folders=sorted({prefix+e['output'].split('/')[0] for e in plan['files']})
    engine.checkpoint(job,output_folders=folders,publication_started=True)
    for index,entry in enumerate(plan['files']):
        engine.check();engine.update(job,current=entry['source'])
        source_parts=parts(entry['source']);folder,name=validate_output(entry['output'])
        with directory(data,'/'.join(source_parts[:-1])) as origin, directory(imports,folder) as target:
            present=exists(origin,source_parts[-1]);published=exists(target,name)
            if present and published:
                raise FileExistsError('Publication conflict; both files were retained: '+entry['output'])
            if not present and not published:
                raise FileNotFoundError('Copied file is missing from both locations: '+entry['source'])
            parent,leaf=(origin,source_parts[-1]) if present else (target,name)
            if engine.hash_file(parent,leaf,lambda n:engine.advance_phase(job,n))!=(entry['size'],entry['sha256']):
                raise RuntimeError('Checksum changed; file was not moved: '+entry['source'])
            if present:
                rename_exclusive(origin,source_parts[-1],target,name)
                sync_directory(target);sync_directory(origin)
        engine.checkpoint(job,published_files=index+1)
    engine.checkpoint(job,publication_complete=True)
    return folders

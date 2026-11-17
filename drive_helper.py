#!/usr/bin/python3
"""Narrow root helper. Installed root-owned; invoked with python3 -I."""
import contextlib
import fcntl
import hashlib
import json
import os
import pwd
import re
import selectors
import signal
import stat
import time
import subprocess
import sys
from pathlib import Path

ROOT = Path("/run/pi-nas-drop-drives")
SUPPORTED = {"vfat", "exfat", "ntfs", "ext4", "ext3", "ext2"}


def command(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=35, env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C"})
    if result.returncode:
        raise RuntimeError(result.stderr.strip()[:500] or "Device operation failed")
    return result.stdout


def flatten(node):
    yield node
    for child in node.get("children", []):
        yield from flatten(child)


def scan():
    tree = json.loads(command(["/usr/bin/lsblk", "--json", "--bytes", "--paths", "-o", "NAME,TYPE,TRAN,RM,SIZE,FSTYPE,LABEL,UUID,SERIAL,MAJ:MIN,MOUNTPOINTS"]))
    drives = []
    for disk in tree["blockdevices"]:
        if disk["type"] != "disk" or disk.get("tran") != "usb":
            continue
        nodes = list(flatten(disk))
        blocked = any(m and not str(m).startswith(str(ROOT) + "/") for node in nodes for m in node.get("mountpoints", []))
        for node in nodes:
            if node["type"] not in {"part", "disk"} or (not node.get("fstype") and node.get("children")):
                continue
            major = node["maj:min"]
            identity = "|".join(str(x or "") for x in (major, disk.get("serial"), node.get("uuid"), node.get("size"), node.get("fstype")))
            token = major + ":" + hashlib.sha256(identity.encode()).hexdigest()[:16]
            mounts = [x for x in node.get("mountpoints", []) if x]
            reason = "Drive is mounted elsewhere or contains a system mount" if blocked else "Unsupported or encrypted filesystem" if node.get("fstype") not in SUPPORTED else ""
            drives.append({"id": token, "node": node["name"], "label": node.get("label") or Path(node["name"]).name, "size": node["size"], "filesystem": node.get("fstype") or "unknown", "usable": not reason, "reason": reason, "mounted": bool(mounts), "major": major, "mounts": mounts})
    return drives


def validate_mount(device, target):
    data = json.loads(command(["/usr/bin/findmnt", "-J", "-M", str(target), "-o", "TARGET,OPTIONS,SOURCE"]))["filesystems"][0]
    # FUSE/ntfs-3g has a different mount major:minor, so verify the source node.
    source = os.stat(data["source"])
    identity = f"{os.major(source.st_rdev)}:{os.minor(source.st_rdev)}"
    if data["target"] != str(target) or not stat.S_ISBLK(source.st_mode) or identity != device["major"] or not {"ro", "nosuid", "nodev", "noexec"}.issubset(set(data["options"].split(","))):
        raise RuntimeError("Source mount does not meet read-only requirements")


def perform(action, token=None, request=None):
    devices = scan()
    if action == "list":
        return [{k: v for k, v in d.items() if k not in {"mounts", "major"}} for d in devices]
    if action not in {"mount", "eject", "delete"} or not token or not re.fullmatch(r"[0-9]+:[0-9]+:[0-9a-f]{16}", token):
        raise ValueError("Invalid operation")
    device = next((d for d in devices if d["id"] == token), None)
    if not device or not device["usable"]:
        raise ValueError("Drive is missing, changed, busy, or unsupported")
    node = device["node"]
    s = os.stat(node, follow_symlinks=False)
    if not stat.S_ISBLK(s.st_mode) or f"{os.major(s.st_rdev)}:{os.minor(s.st_rdev)}" != device["major"]:
        raise ValueError("Block device identity changed")
    ROOT.mkdir(mode=0o755, exist_ok=True)
    target = ROOT / token.replace(":", "-")
    if target.is_symlink():
        raise ValueError("Invalid mount directory")
    if device["mounts"]:
        if device["mounts"] != [str(target)]:
            raise ValueError("Drive is in use outside this app")
        validate_mount(device, target)
    if action == "eject":
        if device["mounts"]:
            command(["/usr/bin/umount", "--", str(target)])
        if target.exists():
            target.rmdir()
        return {"ok": True, "message": "Partition unmounted. Unmount every partition on this drive before unplugging."}
    if not device["mounts"]:
        account = pwd.getpwnam("pidrop")
        target.mkdir(mode=0o755, exist_ok=True)
        fs = device["filesystem"]
        options = "ro,nosuid,nodev,noexec"
        if fs in {"ext3", "ext4"}:
            options += ",noload"
        if fs in {"vfat", "exfat", "ntfs"}:
            options += f",uid={account.pw_uid},gid={account.pw_gid},umask=0077"
        # ntfs-3g supports older Raspberry Pi OS kernels as well as current ones.
        mount_type = "ntfs-3g" if fs == "ntfs" else fs
        command(["/usr/bin/mount", "-t", mount_type, "-o", options, "--", node, str(target)])
        validate_mount(device, target)
    if action == "delete":
        return delete_file(device, target, request)
    return {"path": str(target)}


DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
READ = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def relative(path):
    if not isinstance(path,str) or len(path)>4096 or not path or '\x00' in path:
        raise ValueError('Invalid deletion path')
    names=path.split('/')
    if any(n in ('','.','..') for n in names): raise ValueError('Invalid deletion path')
    return names


@contextlib.contextmanager
def parent_directory(root,names):
    fd=os.dup(root)
    try:
        for name in names[:-1]:
            child=os.open(name,DIRECTORY,dir_fd=fd);os.close(fd);fd=child
        yield fd
    finally: os.close(fd)


def signature(info):
    return (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns)


def fresh_hash(fd,check,emit):
    before=os.fstat(fd)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1:
        raise ValueError('Deletion requires regular files without hard links')
    digest=hashlib.sha256();count=0;last=0;pending=0
    while True:
        check()
        chunk=os.read(fd,1024*1024)
        if not chunk: break
        digest.update(chunk);count+=len(chunk);pending+=len(chunk)
        if time.monotonic()-last>=.2:
            emit({'bytes':pending});pending=0;last=time.monotonic()
    if pending: emit({'bytes':pending})
    if signature(before)!=signature(os.fstat(fd)) or count!=before.st_size:
        raise RuntimeError('File changed during verification; original was kept')
    return (count,digest.hexdigest()),before


def nas_root(config):
    target=config['nas_mount']
    data=json.loads(command(['/usr/bin/findmnt','-J','-M',target,'-o','TARGET,FSTYPE,OPTIONS,SOURCE']))['filesystems'][0]
    options=set(data['options'].split(','))
    if (data['target']!=target or data['fstype']!='cifs' or data['source']!=config['nas_share']
            or not {'rw','seal','cache=none'}.issubset(options) or not any(o.startswith('vers=3') for o in options)):
        raise RuntimeError('The configured encrypted NAS is not mounted; originals were kept')
    fd=os.open(target,DIRECTORY)
    if not os.path.ismount(target) or os.fstat(fd).st_dev!=os.stat(target).st_dev:
        os.close(fd);raise RuntimeError('NAS mount changed')
    return fd


def delete_file(device,target,request,config_path='/etc/pi-nas-drop/config.json',emit=None,confirm=None,check=None):
    """Hash both files while RO, authorize, unlink one, restore RO even on errors."""
    emit=emit or (lambda result: print(json.dumps(result),flush=True))
    confirm=confirm or (lambda: json.loads(sys.stdin.buffer.readline(1024)))
    def connected():
        with selectors.DefaultSelector() as selector:
            selector.register(sys.stdin,selectors.EVENT_READ)
            if selector.select(0):
                raise RuntimeError('Deletion cancelled before authorization')
    check=check or connected
    if (not isinstance(request,dict) or type(request.get('size')) is not int or request['size']<0
            or not isinstance(request.get('sha256'),str) or not re.fullmatch('[0-9a-f]{64}',request['sha256'])):
        raise ValueError('Invalid verification receipt')
    names=relative(request.get('path'));backup=relative(request.get('backup_path'))
    expected=request['size'],request['sha256']
    config=json.loads(Path(config_path).read_text())
    validate_mount(device,target)
    rw_attempted=False
    try:
        with contextlib.ExitStack() as stack:
            src=os.open(target,DIRECTORY);stack.callback(os.close,src)
            nas=nas_root(config);stack.callback(os.close,nas)
            source_parent=stack.enter_context(parent_directory(src,names))
            backup_parent=stack.enter_context(parent_directory(nas,backup))
            source=os.open(names[-1],READ,dir_fd=source_parent);stack.callback(os.close,source)
            dest=os.open(backup[-1],READ,dir_fd=backup_parent);stack.callback(os.close,dest)
            source_hash,before=fresh_hash(source,check,emit)
            if source_hash!=expected: raise RuntimeError('Original changed; it was kept')
            backup_hash,saved=fresh_hash(dest,check,emit)
            if backup_hash!=expected: raise RuntimeError('NAS checksum changed; original was kept')
            check();emit({'ready':True})
            if confirm()!={'delete':True}: raise ValueError('Deletion was not confirmed')
            if (signature(os.stat(backup[-1],dir_fd=backup_parent,follow_symlinks=False))!=signature(saved)
                    or signature(os.fstat(dest))!=signature(saved)):
                raise RuntimeError('NAS copy changed after verification; original was kept')
            rw_attempted=True
            try:
                command(['/usr/bin/mount','-o','remount,rw,nosuid,nodev,noexec','--',str(target)])
                if signature(os.stat(names[-1],dir_fd=source_parent,follow_symlinks=False))!=signature(before):
                    raise RuntimeError('Original changed after verification; it was kept')
                os.unlink(names[-1],dir_fd=source_parent)
                os.fsync(source_parent)
            finally:
                # Do not leave a source writable after an error or cancellation.
                command(['/usr/bin/mount','-o','remount,ro,nosuid,nodev,noexec','--',str(target)])
                validate_mount(device,target)
                rw_attempted=False
    finally:
        if rw_attempted:
            # All handles are now closed. Fail closed if restoring RO failed.
            command(['/usr/bin/umount','--',str(target)])
    return {'deleted':True}


if __name__ == "__main__":
    try:
        if os.geteuid() != 0 or len(sys.argv) not in {2, 3}:
            raise ValueError("This helper requires the installed sudo rule")
        with open("/run/pi-nas-drop-helper.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if sys.argv[1] == 'delete':
                def interrupted(signum, frame):
                    raise RuntimeError('Deletion interrupted; restoring source protection')
                signal.signal(signal.SIGTERM, interrupted)
                signal.signal(signal.SIGINT, interrupted)
                request=json.loads(sys.stdin.buffer.readline(16385))
                print(json.dumps(perform(*sys.argv[1:],request=request)),flush=True)
            else:
                print(json.dumps(perform(*sys.argv[1:])))
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)

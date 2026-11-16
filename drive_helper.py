#!/usr/bin/python3
"""Narrow root helper. Installed root-owned; invoked with python3 -I."""
import fcntl
import hashlib
import json
import os
import pwd
import re
import stat
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


def perform(action, token=None):
    devices = scan()
    if action == "list":
        return [{k: v for k, v in d.items() if k not in {"mounts", "major"}} for d in devices]
    if action not in {"mount", "eject"} or not token or not re.fullmatch(r"[0-9]+:[0-9]+:[0-9a-f]{16}", token):
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
    return {"path": str(target)}


if __name__ == "__main__":
    try:
        if os.geteuid() != 0 or len(sys.argv) not in {2, 3}:
            raise ValueError("This helper requires the installed sudo rule")
        with open("/run/pi-nas-drop-helper.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            print(json.dumps(perform(*sys.argv[1:])))
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)

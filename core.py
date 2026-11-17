"""Metadata-only browsing and bounded-memory, verified copy operations."""
import contextlib
import hashlib
import json
import os
import re
import stat
import subprocess
import threading
import time
import uuid
from pathlib import Path

from camera import Cameras
from camera_worker import components as camera_parts, safe_name

CHUNK = 1024 * 1024
DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
READ = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
WRITE = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
ACTIVE = {"queued", "copying", "verifying"}


def parts(path):
    if not isinstance(path, str) or len(path) > 4096 or "\x00" in path:
        raise ValueError("Invalid path")
    if path == "":
        return []
    result = path.split("/")
    if any(p in ("", ".", "..") for p in result):
        raise ValueError("Invalid path")
    return result


@contextlib.contextmanager
def directory(root_fd, path=""):
    fd = os.dup(root_fd)
    try:
        for part in parts(path):
            child = os.open(part, DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def listing(root_fd, path="", offset=0):
    """Returns names and metadata only. No regular-file data is read here."""
    if offset < 0 or offset > 10000000:
        raise ValueError("Invalid offset")
    items = []
    more = False
    with directory(root_fd, path) as fd, os.scandir(fd) as entries:
        for i, entry in enumerate(entries):
            if i < offset:
                continue
            if len(items) == 200:
                more = True
                break
            s = entry.stat(follow_symlinks=False)
            kind = "folder" if stat.S_ISDIR(s.st_mode) else "file" if stat.S_ISREG(s.st_mode) else "unsupported"
            items.append({"name": entry.name, "kind": kind, "size": s.st_size if kind == "file" else None})
    return {"items": items, "more": more, "offset": offset}


class Hardware:
    def __init__(self, config):
        self.config = config
        self.cameras = Cameras()
        self.camera_error = ""

    def helper(self, action, device=None):
        args = ["/usr/bin/sudo", "-n", "/usr/bin/python3", "-I", "/opt/pi-nas-drop/drive_helper.py", action]
        if device is not None:
            if not re.fullmatch(r"[0-9]+:[0-9]+:[0-9a-f]{16}", device):
                raise ValueError("Invalid device")
            args.append(device)
        result = subprocess.run(args, capture_output=True, text=True, timeout=45, check=False)
        if result.returncode:
            raise RuntimeError(result.stderr.strip()[:600] or "Drive operation failed")
        return json.loads(result.stdout)

    def devices(self):
        devices = self.helper("list")
        try:
            devices.extend(self.cameras.devices())
            self.camera_error = ""
        except (OSError, ValueError, RuntimeError) as exc:
            self.camera_error = str(exc)
        return devices

    def camera_source(self, device):
        return self.cameras.source(device)

    def source(self, device):
        mounted = self.helper("mount", device)
        return os.open(mounted["path"], DIRECTORY)

    def eject(self, device):
        if isinstance(device, str) and device.startswith("camera:"):
            return {"ok": True, "message": "Camera sessions are closed. You can unplug the camera."}
        return self.helper("eject", device)

    def nas(self):
        target = self.config["nas_mount"]
        result = subprocess.run(["/usr/bin/findmnt", "-J", "-M", target, "-o", "TARGET,FSTYPE,OPTIONS,SOURCE"], capture_output=True, text=True, timeout=5)
        if result.returncode:
            raise RuntimeError("NAS is not mounted. No files will be copied to the Pi.")
        fs = json.loads(result.stdout)["filesystems"][0]
        options = set(fs["options"].split(","))
        if fs["target"] != target or fs["fstype"] != "cifs" or not {"rw", "seal", "cache=none"}.issubset(options):
            raise RuntimeError("NAS requires an encrypted SMB3 mount with cache=none.")
        if not any(x.startswith("vers=3") for x in options) or fs["source"] != self.config["nas_share"]:
            raise RuntimeError("NAS mount does not match the configured SMB3 destination.")
        fd = os.open(target, DIRECTORY)
        if not os.path.ismount(target) or os.fstat(fd).st_dev != os.stat(target).st_dev:
            os.close(fd)
            raise RuntimeError("NAS mount changed; try again.")
        return fd

    def nas_status(self):
        try:
            fd = self.nas()
            try:
                v = os.fstatvfs(fd)
                return {"ready": True, "free": v.f_bavail * v.f_frsize, "destination": self.config["nas_share"] + "/Pi-Imports"}
            finally:
                os.close(fd)
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            return {"ready": False, "error": str(exc)}


class Cancelled(Exception):
    pass


class Transfers:
    def __init__(self, hardware, history_path):
        self.hardware = hardware
        self.history_path = Path(history_path)
        self.lock = threading.RLock()
        self.operation = threading.Lock()
        self.cancel_event = threading.Event()
        self.device_cache = []
        self.device_cache_time = 0
        try:
            self.jobs = json.loads(self.history_path.read_text())[-30:]
        except (OSError, ValueError):
            self.jobs = []
        interrupted = False
        for job in self.jobs:
            if job["state"] in ACTIVE:
                job.update(state="interrupted", error="Service stopped. Originals are unchanged; incomplete NAS folder retained. Start a new copy.")
                interrupted = True
        if interrupted:
            self.save()

    def save(self):
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.history_path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.jobs, ensure_ascii=True))
        os.replace(temp, self.history_path)

    def snapshot(self):
        with self.lock:
            return [dict(j) for j in reversed(self.jobs)]

    def devices(self):
        # Discovery must not contend with a camera copy or metadata session.
        if not self.operation.acquire(blocking=False):
            return list(self.device_cache)
        try:
            if time.monotonic() - self.device_cache_time >= 4:
                self.device_cache = self.hardware.devices()
                self.device_cache_time = time.monotonic()
            return list(self.device_cache)
        finally:
            self.operation.release()

    def browse(self, device, path, offset):
        if not self.operation.acquire(blocking=False):
            raise RuntimeError("Wait for the drive operation to finish.")
        try:
            if isinstance(device, str) and device.startswith("camera:"):
                camera_parts(path)
                return self.hardware.camera_source(device).listing(path, offset)
            fd = self.hardware.source(device)
            try:
                return listing(fd, path, offset)
            finally:
                os.close(fd)
        finally:
            self.operation.release()

    def eject(self, device):
        if not self.operation.acquire(blocking=False):
            raise RuntimeError("Wait for the transfer to finish before ejecting.")
        try:
            return self.hardware.eject(device)
        finally:
            self.operation.release()

    def start(self, device, paths):
        if not isinstance(paths, list) or not paths or len(paths) > 200:
            raise ValueError("Choose up to 200 files or folders, or copy the entire drive.")
        paths = sorted(set(paths))
        for p in paths:
            parts(p)
            if isinstance(device, str) and device.startswith("camera:"):
                camera_parts(p)
        # Parent selection already includes its descendants.
        paths = [p for p in paths if not any(p != q and (q == "" or p.startswith(q + "/")) for q in paths)]
        if not self.operation.acquire(blocking=False):
            raise RuntimeError("A drive operation is already in progress.")
        try:
            with self.lock:
                job = {"id": uuid.uuid4().hex[:12], "device": device, "state": "queued", "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "files": 0, "bytes": 0, "skipped": 0, "current": "", "destination": ""}
                self.jobs = self.jobs[-29:] + [job]
                self.cancel_event.clear()
                self.save()
            threading.Thread(target=self.run, args=(job, paths), daemon=True).start()
        except Exception:
            self.operation.release()
            raise
        return dict(job)

    def cancel(self, job_id):
        with self.lock:
            if not self.jobs or self.jobs[-1]["id"] != job_id or self.jobs[-1]["state"] not in ACTIVE:
                raise ValueError("No active matching transfer")
            self.cancel_event.set()

    def check(self):
        if self.cancel_event.is_set():
            raise Cancelled("Transfer cancelled. Originals are unchanged; incomplete NAS folder retained.")

    def update(self, job, **fields):
        with self.lock:
            job.update(fields)

    def run(self, job, paths):
        try:
            with contextlib.ExitStack() as stack:
                is_camera = isinstance(job["device"], str) and job["device"].startswith("camera:")
                if is_camera:
                    src = self.hardware.camera_source(job["device"])
                else:
                    src = self.hardware.source(job["device"])
                    stack.callback(os.close, src)
                nas = self.hardware.nas()
                stack.callback(os.close, nas)
                try:
                    os.mkdir("Pi-Imports", 0o700, dir_fd=nas)
                except FileExistsError:
                    pass
                imports = stack.enter_context(directory(nas, "Pi-Imports"))
                stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + "-" + job["id"]
                incomplete = ".incomplete-" + stamp
                os.mkdir(incomplete, 0o700, dir_fd=imports)
                dest = stack.enter_context(directory(imports, incomplete))
                os.mkdir("data", 0o700, dir_fd=dest)
                data = stack.enter_context(directory(dest, "data"))
                manifest = stack.enter_context(os.fdopen(os.open("checksums.jsonl", WRITE, 0o600, dir_fd=dest), "w", encoding="utf-8"))
                self.update(job, state="copying", destination="Pi-Imports/" + incomplete)
                with self.lock:
                    self.save()
                for path in paths:
                    self.check()
                    if is_camera:
                        self.copy_camera_selection(src, data, path, job, manifest)
                    elif not path:
                        self.copy_dir(src, data, "", job, manifest, 0)
                    else:
                        components = parts(path)
                        with directory(src, "/".join(components[:-1])) as parent:
                            out = os.dup(data)
                            try:
                                for comp in components[:-1]:
                                    try:
                                        os.mkdir(comp, 0o700, dir_fd=out)
                                    except FileExistsError:
                                        pass
                                    next_fd = os.open(comp, DIRECTORY, dir_fd=out)
                                    os.close(out)
                                    out = next_fd
                                self.copy_entry(parent, out, components[-1], path, job, manifest, 0)
                            finally:
                                os.close(out)
                self.check()
                manifest.flush()
                os.fsync(manifest.fileno())
                # Commit only after every selected regular file has passed read-back.
                final = "transfer-" + stamp
                os.rename(incomplete, final, src_dir_fd=imports, dst_dir_fd=imports)
                self.update(job, state="complete_with_skips" if job["skipped"] else "complete", current="", destination="Pi-Imports/" + final)
        except Cancelled as exc:
            self.update(job, state="cancelled", error=str(exc))
        except Exception as exc:
            self.update(job, state="failed", error=str(exc)[:800])
        finally:
            try:
                with self.lock:
                    self.save()
            finally:
                self.operation.release()

    def copy_camera_selection(self, source, dest, path, job, manifest):
        names = camera_parts(path)
        if not names:
            self.copy_camera_dir(source, dest, "", job, manifest, 0)
            return
        out = os.dup(dest)
        try:
            for name in names[:-1]:
                try:
                    os.mkdir(name, 0o700, dir_fd=out)
                except FileExistsError:
                    pass
                child = os.open(name, DIRECTORY, dir_fd=out)
                os.close(out)
                out = child
            self.copy_camera_entry(source, out, names[-1], path, job, manifest, len(names) - 1)
        finally:
            os.close(out)

    def copy_camera_dir(self, source, dest, path, job, manifest, depth):
        if depth > 64:
            raise ValueError("Camera directory nesting exceeds 64 levels")
        offset = 0
        while True:
            self.check()
            page = source.listing(path, offset)
            for entry in page["items"]:
                self.check()
                name = safe_name(entry["name"])
                child_path = path + "/" + name if path else name
                camera_parts(child_path)
                self.copy_camera_entry(source, dest, name, child_path, job, manifest, depth)
            if not page["more"]:
                break
            if not page["items"]:
                raise RuntimeError("Camera returned an invalid directory page")
            offset += len(page["items"])

    def copy_camera_entry(self, source, dest, name, path, job, manifest, depth):
        self.check()
        safe_name(name)
        camera_parts(path)
        info = source.stat(path)
        if info["kind"] == "folder":
            os.mkdir(name, 0o700, dir_fd=dest)
            with directory(dest, name) as child:
                self.copy_camera_dir(source, child, path, job, manifest, depth + 1)
        elif info["kind"] == "file":
            self.copy_camera_file(source, dest, name, path, info, job, manifest)
        else:
            raise ValueError("Unsupported camera item")

    def copy_camera_file(self, source, dest, name, path, info, job, manifest):
        size = info.get("size")
        if size is not None and (type(size) is not int or size < 0):
            raise ValueError("Invalid camera file size")
        self.check_space(dest, size or 0)
        self.update(job, state="copying", current=path)
        digest = hashlib.sha256()
        copied = 0
        with os.fdopen(os.open(name, WRITE, 0o600, dir_fd=dest), "wb") as output:
            with source.stream(path, info, self.check) as chunks:
                for chunk in chunks:
                    self.check()
                    if size is not None and copied + len(chunk) > size:
                        raise RuntimeError("Camera sent more data than expected: " + path)
                    if size is None:
                        self.check_space(dest, len(chunk))
                    output.write(chunk)
                    digest.update(chunk)
                    copied += len(chunk)
                    self.update(job, bytes=job["bytes"] + len(chunk))
            output.flush()
            os.fsync(output.fileno())
        if size is not None and copied != size:
            raise RuntimeError("Camera transfer was incomplete: " + path)
        mtime = info.get("mtime")
        times = (mtime * 1000000000, mtime * 1000000000) if mtime is not None else None
        self.verify_copy(dest, name, path, copied, digest, times, job, manifest)

    @staticmethod
    def check_space(dest, size):
        space = os.fstatvfs(dest)
        if size + 16 * CHUNK > space.f_bavail * space.f_frsize:
            raise RuntimeError("Not enough free space on NAS; originals are unchanged.")

    def copy_dir(self, src, dest, prefix, job, manifest, depth):
        if depth > 64:
            raise ValueError("Directory nesting exceeds 64 levels")
        with os.scandir(src) as entries:
            for entry in entries:
                self.check()
                name = entry.name
                path = prefix + "/" + name if prefix else name
                self.copy_entry(src, dest, name, path, job, manifest, depth)

    def copy_entry(self, src, dest, name, path, job, manifest, depth):
        self.check()
        before = os.stat(name, dir_fd=src, follow_symlinks=False)
        if stat.S_ISDIR(before.st_mode):
            os.mkdir(name, 0o700, dir_fd=dest)
            with directory(src, name) as s, directory(dest, name) as d:
                self.copy_dir(s, d, path, job, manifest, depth + 1)
        elif stat.S_ISREG(before.st_mode):
            self.copy_file(src, dest, name, path, job, manifest)
        else:
            self.update(job, skipped=job["skipped"] + 1)
            manifest.write(json.dumps({"path": path, "status": "skipped", "reason": "Symlink or special file"}) + "\n")

    def copy_file(self, src, dest, name, path, job, manifest):
        self.update(job, state="copying", current=path)
        with os.fdopen(os.open(name, READ, dir_fd=src), "rb") as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ValueError("Source changed type")
            self.check_space(dest, before.st_size)
            digest = hashlib.sha256()
            copied = 0
            with os.fdopen(os.open(name, WRITE, 0o600, dir_fd=dest), "wb") as output:
                while True:
                    self.check()
                    chunk = source.read(CHUNK)
                    if not chunk:
                        break
                    output.write(chunk)
                    digest.update(chunk)
                    copied += len(chunk)
                    self.update(job, bytes=job["bytes"] + len(chunk))
                output.flush()
                os.fsync(output.fileno())
            after = os.fstat(source.fileno())
            if copied != before.st_size or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise RuntimeError("Source changed during copy: " + path)
        self.verify_copy(dest, name, path, copied, digest, (before.st_atime_ns, before.st_mtime_ns), job, manifest)

    def verify_copy(self, dest, name, path, copied, digest, times, job, manifest):
        self.update(job, state="verifying")
        check = hashlib.sha256()
        with os.fdopen(os.open(name, READ, dir_fd=dest), "rb") as output:
            if not stat.S_ISREG(os.fstat(output.fileno()).st_mode):
                raise ValueError("Destination changed type")
            while True:
                self.check()
                chunk = output.read(CHUNK)
                if not chunk:
                    break
                check.update(chunk)
        if digest.digest() != check.digest():
            raise RuntimeError("Checksum mismatch: " + path)
        if times is not None:
            os.utime(name, ns=times, dir_fd=dest, follow_symlinks=False)
        manifest.write(json.dumps({"path": path, "size": copied, "sha256": digest.hexdigest(), "status": "verified"}) + "\n")
        self.update(job, files=job["files"] + 1, state="copying")

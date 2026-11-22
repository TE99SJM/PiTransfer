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
ACTIVE = {"queued", "starting", "counting", "copying", "verifying", "resuming", "finalizing", "organizing", "deleting"}
RETRYABLE = {"failed", "cancelled", "interrupted"}
ESTIMATING = {"queued", "counting"}


def parts(path):
    if not isinstance(path, str) or len(path) > 4096 or "\x00" in path:
        raise ValueError("Invalid path")
    if path == "":
        return []
    result = path.split("/")
    if any(p in ("", ".", "..") for p in result):
        raise ValueError("Invalid path")
    return result


def selection(device, paths):
    if not isinstance(paths, list) or not paths or len(paths) > 200:
        raise ValueError("Choose up to 200 files or folders, or copy all files.")
    for path in paths:
        parts(path)
        if isinstance(device, str) and device.startswith("camera:"):
            camera_parts(path)
    paths = sorted(set(paths))
    return [p for p in paths if not any(p != q and (q == "" or p.startswith(q + "/")) for q in paths)]


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


def folder_listing(root_fd, path="", offset=0):
    """NAS directories only; never opens or returns regular-file contents."""
    parts(path)
    if type(offset) is not int or not 0 <= offset <= 10000000:
        raise ValueError("Invalid offset")
    items, seen, more = [], 0, False
    with directory(root_fd, path) as fd, os.scandir(fd) as entries:
        for entry in entries:
            if entry.name.startswith((".pitransfer-", ".incomplete-")) or not entry.is_dir(follow_symlinks=False):
                continue
            if seen < offset:
                seen += 1
                continue
            if len(items) == 200:
                more = True
                break
            items.append({"name": entry.name})
        space = os.fstatvfs(fd)
    return {"items": items, "more": more, "offset": offset, "path": path,
            "free": space.f_bavail * space.f_frsize}


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

    def devices(self, exclude=None):
        devices = self.helper("list")
        try:
            devices.extend(self.cameras.devices(exclude))
            self.camera_error = ""
        except (OSError, ValueError, RuntimeError) as exc:
            self.camera_error = str(exc)
        return devices

    def camera_source(self, device):
        return self.cameras.source(device)

    def source(self, device):
        mounted = self.helper("mount", device)
        return os.open(mounted["path"], DIRECTORY)

    def delete_verified(self, device, entry, check, progress, verified=None):
        if not isinstance(device,str) or not re.fullmatch(r"[0-9]+:[0-9]+:[0-9a-f]{16}",device):
            raise ValueError("Invalid device")
        request={k:entry[k] for k in ("path","size","sha256","backup_path")}
        command=["/usr/bin/sudo","-n","/usr/bin/python3","-I","/opt/pi-nas-drop/drive_helper.py","delete",device]
        process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        authorized=False;deleted=False;pending=b""
        def before_authorization():
            if not authorized: check()
        try:
            process.stdin.write((json.dumps(request)+"\n").encode());process.stdin.flush()
            for chunk in self.cameras._chunks(process,before_authorization):
                pending+=chunk
                while b"\n" in pending:
                    line,pending=pending.split(b"\n",1);response=json.loads(line)
                    if set(response)=={"bytes"} and type(response["bytes"]) is int and response["bytes"]>=0:
                        progress(response["bytes"])
                    elif response=={"ready":True} and not authorized:
                        if verified is not None: verified()
                        check()
                        process.stdin.write(b'{"delete":true}\n');process.stdin.flush()
                        authorized=True;process.stdin.close()
                    elif response=={"deleted":True} and authorized and not deleted:
                        deleted=True
                    else: raise ValueError("Invalid drive deletion response")
                if len(pending)>65536: raise ValueError("Invalid drive deletion response")
            if pending or not deleted: raise RuntimeError("Deletion was not acknowledged; inspect the source before retrying")
        finally:
            process.stdin.close()
            if process.poll() is None and not authorized:
                # No authorization was sent, so the root helper cannot remount RW.
                process.terminate()
            # Once authorized, allow the helper to restore RO. Never SIGKILL it.
            process.wait()
            process.stdout.close();process.stderr.close()

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
                return {"ready": True, "free": v.f_bavail * v.f_frsize, "share": self.config["nas_share"],
                        "destination": self.config["nas_share"] + "/Pi-Imports"}
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
        self.active_job = None
        self.queue = []
        self.discovering = False
        self.cancel_event = threading.Event()
        self.device_cache = []
        self.device_cache_time = 0
        self.size_job = None
        self.size_cancel = threading.Event()
        self.resume_records = None
        self.resume_seen = set()
        self.resume_pending = None
        self.delete_tokens = {}
        try:
            self.jobs = json.loads(self.history_path.read_text())[-30:]
        except (OSError, ValueError):
            self.jobs = []
        interrupted = False
        for job in self.jobs:
            if job["state"] == "deleting":
                job["state"] = job.pop("cleanup_previous_state", "complete")
                job.setdefault("cleanup", {}).update(state="interrupted", error="Deletion interrupted. Recheck before trying again; no deletion resumes automatically.")
                interrupted = True
            elif job["state"] in ACTIVE:
                job.update(state="interrupted", error="Service stopped. Originals and incomplete NAS folder retained. Use Resume when available.")
                interrupted = True
        if interrupted:
            self.save()

    def save(self):
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.history_path.with_suffix(".tmp")
        with temp.open("w") as output:
            json.dump(self.jobs, output, ensure_ascii=True)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, self.history_path)
        fd = os.open(self.history_path.parent, DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def snapshot(self):
        with self.lock:
            positions = {j[0]["id"]: i + 1 for i, j in enumerate(self.queue)}
            return [dict(dict(j, cleanup=dict(j["cleanup"])) if "cleanup" in j else j, can_resume=bool(self.resume_mode(j)), active_device=self.job_device(j),
                         can_delete=j["state"] in {"complete", "complete_with_skips"} and j["files"] > 0 and j.get("cleanup", {}).get("state") != "complete",
                         can_organize=j["state"] in {"complete", "complete_with_skips"} and j.get("layout") != "camera_date",
                         queue_position=positions.get(j["id"], 0),
                         resume_needs_source=self.resume_mode(j) == "copy") for j in reversed(self.jobs)]

    @staticmethod
    def job_device(job):
        return job.get("cleanup", {}).get("device", job["device"]) if job.get("state") == "deleting" else job.get("device")

    def acquire_operation(self):
        with self.lock:
            return not self.discovering and self.operation.acquire(blocking=False)

    def release_operation(self, transfer=False):
        with self.lock:
            if transfer:
                self.active_job = None
            self.operation.release()
            self.kick_queue()

    def kick_queue(self):
        # Called with self.lock held. One worker owns copy/resume state at a time.
        if not self.queue or self.discovering or not self.operation.acquire(False):
            return
        job, paths, resuming = self.queue.pop(0)
        self.active_job = job
        job["state"] = "starting"
        self.cancel_event.clear()
        try:
            threading.Thread(target=self.run, args=(job, paths, resuming), daemon=True).start()
        except Exception as exc:
            job.update(state="failed", error=str(exc))
            self.active_job = None
            self.operation.release()
            self.save()

    @staticmethod
    def destination_parts(job):
        folder = job.get("destination_folder", "")
        parts(folder)
        prefix = (folder + "/" if folder else "") + "Pi-Imports/"
        destination = job.get("destination", "")
        if not destination.startswith(prefix):
            raise ValueError("Invalid saved transfer destination")
        name = destination[len(prefix):]
        match = re.fullmatch(r"(?:\.incomplete-|transfer-|\.pitransfer-)([0-9]{8}-[0-9]{6}-[0-9a-f]{12})", name)
        if not match or match[1].rsplit("-", 1)[1] != job["id"]:
            raise ValueError("Invalid saved transfer folder")
        return (prefix, ("transfer-" if job.get("legacy_workspace") else ".incomplete-") + match[1],
                (".pitransfer-" if job.get("layout") == "camera_date" else "transfer-") + match[1])

    @classmethod
    def resume_mode(cls, job):
        if job.get("state") not in RETRYABLE:
            return None
        if job.get("copy_complete"):
            return "finalize"
        # Older versions did not save selections. Recover only their explicit
        # final-rename failure, never infer a partial selection from copied files.
        try:
            _, incomplete, final = cls.destination_parts(job)
            if (job.get("state") == "failed" and not job.get("paths")
                    and f"{incomplete!r} -> {final!r}" in job.get("error", "")):
                return "finalize"
        except (ValueError, KeyError, TypeError):
            pass
        return "copy" if job.get("resume_version") == 1 and job.get("paths") else None

    def resume(self, job_id, device=None):
        if not self.acquire_operation():
            raise RuntimeError("Wait for the current drive operation to finish.")
        try:
            with self.lock:
                job = next((j for j in self.jobs if j["id"] == job_id), None)
                mode = self.resume_mode(job or {})
                if not mode:
                    raise ValueError("This transfer has no safe resume checkpoint.")
                if mode == "copy":
                    device = device or job["device"]
                    if not isinstance(device, str) or device.startswith("camera:") != job["device"].startswith("camera:"):
                        raise ValueError("Select the same kind of source used for this transfer.")
                    paths = selection(device, job["paths"])
                else:
                    paths = []
                # Validate history before changing the saved job.
                if job.get("destination"):
                    self.destination_parts(job)
                job.update(state="queued", error="", resumed_at=time.time(),
                           attempts=job.get("attempts", 1) + 1)
                if mode == "finalize":
                    job["copy_complete"] = True
                else:
                    job["device"] = device
                    source = next((d for d in self.device_cache if d['id'] == device), None)
                    job['device_history_key'] = source.get('history_key') if source else None
                self.cancel_event.clear()
                self.active_job = job
                self.save()
            threading.Thread(target=self.run, args=(job, paths, True), daemon=True).start()
        except Exception:
            self.release_operation(transfer=True)
            raise
        return dict(job)

    def devices(self):
        with self.lock:
            if (self.discovering or time.monotonic() - self.device_cache_time < 4
                    or self.operation.locked() and self.active_job is None):
                return list(self.device_cache)
            self.discovering = True
            active = self.active_job
            active_device = self.job_device(active) if active else None
            if active and active.get("state") == "deleting" and not str(active_device).startswith("camera:"):
                # The disk helper holds its mount lock throughout verification.
                # Do not block status threads behind a multi-GB checksum read.
                self.discovering = False
                return list(self.device_cache)
            excluded = []
            if active and str(active_device).startswith("camera:"):
                token = active_device.split(":")
                excluded = [f"usb:{token[1]},{token[2]}"]
        try:
            found = self.hardware.devices(exclude=excluded) if excluded else self.hardware.devices()
            with self.lock:
                if excluded:
                    found += [d for d in self.device_cache if d["id"] == active_device and not any(f["id"] == d["id"] for f in found)]
                self.device_cache = found
                self.device_cache_time = time.monotonic()
                # Enrich old records only when the exact connection ID matches.
                # Stable serial-derived keys are for history display, never USB
                # access, queue locking, resume approval or deletion authority.
                changed = False
                for job in self.jobs:
                    source = next((d for d in found if d['id'] == job['device']), None)
                    if not source:
                        continue
                    if source.get('history_key') and not job.get('device_history_key'):
                        job['device_history_key'] = source['history_key']; changed = True
                    if job.get('device_label') in (None, '', 'USB camera', 'USB drive') and source.get('label'):
                        job['device_label'] = source['label']; changed = True
                if changed:
                    self.save()
                return list(self.device_cache)
        finally:
            with self.lock:
                self.discovering = False
                self.kick_queue()

    def browse(self, device, path, offset):
        if not self.acquire_operation():
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
            self.release_operation()

    def eject(self, device):
        if not self.acquire_operation():
            raise RuntimeError("Wait for the transfer to finish before ejecting.")
        try:
            return self.hardware.eject(device)
        finally:
            self.release_operation()

    def estimate_snapshot(self):
        with self.lock:
            return dict(self.size_job, paths=list(self.size_job["paths"])) if self.size_job else None

    def browse_nas(self, path, offset=0):
        fd = self.hardware.nas()
        try:
            return folder_listing(fd, path, offset)
        finally:
            os.close(fd)

    def estimate(self, device, paths):
        paths = selection(device, paths)
        if not self.acquire_operation():
            raise RuntimeError("Wait for the current copy, browse, or size calculation to finish.")
        try:
            with self.lock:
                job = {"id": uuid.uuid4().hex, "device": device, "paths": paths, "state": "queued",
                       "bytes": 0, "files": 0, "unknown_files": 0, "skipped": 0}
                self.size_job = job
                self.size_cancel.clear()
            threading.Thread(target=self.run_estimate, args=(job,), daemon=True).start()
        except Exception:
            self.release_operation()
            raise
        return dict(job)

    def cancel_estimate(self, job_id):
        with self.lock:
            if not self.size_job or self.size_job["id"] != job_id:
                raise ValueError("No matching size calculation")
            if self.size_job["state"] in ESTIMATING:
                self.size_cancel.set()

    def run_estimate(self, job):
        def check():
            if self.size_cancel.is_set():
                raise Cancelled("Size calculation cancelled. No files were copied.")
        def progress(totals):
            check()
            self.update(job, **{key: totals[key] for key in ("bytes", "files", "unknown_files", "skipped")})
        outcome = {"state": "complete"}
        try:
            self.update(job, state="counting")
            check()
            if isinstance(job["device"], str) and job["device"].startswith("camera:"):
                self.hardware.camera_source(job["device"]).estimate(job["paths"], check, progress)
            else:
                fd = self.hardware.source(job["device"])
                try:
                    self.estimate_disk(fd, job["paths"], check, progress)
                finally:
                    os.close(fd)
            check()
        except Cancelled as exc:
            outcome = {"state": "cancelled", "error": str(exc)}
        except Exception as exc:
            outcome = {"state": "failed", "error": str(exc)[:800]}
        finally:
            with self.lock:
                job.update(outcome, completed_at=time.time())
                self.release_operation()

    @staticmethod
    def estimate_disk(root_fd, paths, check, progress):
        totals = {"bytes": 0, "files": 0, "unknown_files": 0, "skipped": 0}
        def walk(fd, depth):
            if depth > 64:
                raise ValueError("Directory nesting exceeds 64 levels")
            with os.scandir(fd) as entries:
                for entry in entries:
                    visit(fd, entry.name, depth)
        def visit(fd, name, depth):
            check()
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                with directory(fd, name) as child:
                    walk(child, depth + 1)
            elif stat.S_ISREG(info.st_mode):
                totals["files"] += 1
                totals["bytes"] += info.st_size
            else:
                totals["skipped"] += 1
            progress(totals)
        for path in paths:
            check()
            names = parts(path)
            if not names:
                walk(root_fd, 0)
            else:
                with directory(root_fd, "/".join(names[:-1])) as parent:
                    visit(parent, names[-1], len(names) - 1)
        progress(totals)

    def start(self, device, paths, estimate_id=None, destination_folder="", enqueue=False, layout="original", import_timezone="UTC"):
        from organize import timezone_name
        if layout not in {"original", "camera_date"}:
            raise ValueError("Invalid import layout")
        timezone_name(import_timezone)
        paths = selection(device, paths)
        parts(destination_folder)
        estimate = None
        if estimate_id is not None:
            estimate = self.estimate_snapshot()
            if (not estimate or estimate["id"] != estimate_id or estimate["state"] != "complete"
                    or estimate["device"] != device or estimate["paths"] != paths
                    or time.time() - estimate["completed_at"] > 300):
                raise ValueError("Size estimate changed or expired. Calculate the size again.")
        with self.lock:
            if not enqueue and (self.operation.locked() or self.discovering):
                raise RuntimeError("A drive operation is already in progress. Use Queue all files for another device.")
            if len(self.queue) >= 10:
                raise RuntimeError("The queue is full (10 waiting transfers).")
            if any(self.job_device(j) == device and j["state"] in ACTIVE for j in self.jobs):
                raise RuntimeError("This device already has an active or queued transfer.")
            source = next((d for d in self.device_cache if d["id"] == device), None)
            if enqueue and (not source or not source.get("usable")):
                raise ValueError("Refresh and choose a connected, supported device before queuing.")
            if enqueue and paths != [""]:
                raise ValueError("Queued devices copy all accessible files. Wait until idle to select individual files.")
            try:
                job = {"id": uuid.uuid4().hex[:12], "device": device, "state": "queued", "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "files": 0, "bytes": 0, "skipped": 0, "current": "", "destination": ""}
                job["destination_folder"] = destination_folder
                job.update(layout=layout, import_timezone=import_timezone)
                job.update(paths=paths, resume_version=1, attempts=1, checked_bytes=0,
                           device_label=source["label"] if source else ("USB camera" if str(device).startswith("camera:") else "USB drive"),
                           device_node=source.get("node", "") if source else "",
                           device_history_key=source.get("history_key") if source else None,
                           needs_count=enqueue and estimate is None)
                if estimate:
                    job.update(total_bytes=estimate["bytes"], total_files=estimate["files"],
                               unknown_files=estimate["unknown_files"])
                while len(self.jobs) >= 30:
                    self.jobs.remove(next(j for j in self.jobs if j["state"] not in ACTIVE))
                self.jobs.append(job)
                self.save()
                self.queue.append((job, paths, False))
                self.kick_queue()
            except Exception:
                if 'job' in locals() and job in self.jobs and job["state"] == "queued":
                    self.jobs.remove(job)
                raise
        return dict(job)

    def organize(self, job_id, import_timezone="UTC"):
        from organize import timezone_name
        timezone_name(import_timezone)
        if not self.acquire_operation():
            raise RuntimeError("Wait for the current drive operation to finish.")
        try:
            with self.lock:
                job = next((j for j in self.jobs if j["id"] == job_id), None)
                if not job or job["state"] not in {"complete", "complete_with_skips"} or job.get("layout") == "camera_date":
                    raise ValueError("Choose a completed import that has not been organized yet")
                self.destination_parts(job)
                job.update(layout="camera_date", import_timezone=import_timezone, legacy_workspace=True,
                           copy_complete=True, state="organizing", error="")
                self.cancel_event.clear()
                self.active_job = job
                self.save()
            threading.Thread(target=self.run, args=(job, [], True), daemon=True).start()
            return dict(job)
        except Exception:
            self.release_operation(transfer=True)
            raise

    def prepare_delete(self, job_id, device=None):
        from cleanup import prepare
        return prepare(self, job_id, device)

    def delete_verified(self, token, confirmation):
        from cleanup import start
        return start(self, token, confirmation)

    def cancel(self, job_id):
        with self.lock:
            for index, (job, _, _) in enumerate(self.queue):
                if job["id"] == job_id:
                    self.queue.pop(index)
                    job.update(state="cancelled", error="Removed from queue. No files were copied.")
                    self.save()
                    return
            if not self.active_job or self.active_job["id"] != job_id:
                raise ValueError("No active matching transfer")
            self.cancel_event.set()

    def check(self):
        if self.cancel_event.is_set():
            raise Cancelled("Operation cancelled. Verified NAS files are retained.")

    def update(self, job, **fields):
        with self.lock:
            job.update(fields)

    def advance_phase(self, job, amount):
        with self.lock:
            job["phase_bytes"] = job.get("phase_bytes", 0) + amount

    def checkpoint(self, job, **fields):
        with self.lock:
            job.update(fields)
            self.save()

    def make_folder(self, parent, name):
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
        except FileExistsError:
            if self.resume_records is None:
                raise
            # The caller opens it with O_DIRECTORY | O_NOFOLLOW next.

    def read_manifest(self, manifest, allow_tail=False):
        records, end = {}, 0
        while True:
            self.check()
            line = manifest.readline(65537)
            if not line:
                break
            if len(line) > 65536 or manifest.tell() > 64 * 1024 * 1024 or len(records) >= 100000:
                raise ValueError("Resume manifest exceeds supported limits (64 MiB / 100,000 entries).")
            if not line.endswith(b"\n"):
                if not allow_tail:
                    raise ValueError("Incomplete verification manifest; cannot finalize this transfer.")
                break
            record = json.loads(line)
            path = record.get("path")
            if not parts(path) or path in records:
                raise ValueError("Invalid or duplicate path in verification manifest")
            if record.get("status") == "verified":
                if (type(record.get("size")) is not int or record["size"] < 0
                        or not isinstance(record.get("sha256"), str)
                        or not re.fullmatch(r"[0-9a-f]{64}", record["sha256"])):
                    raise ValueError("Invalid verification record")
            elif record.get("status") != "skipped":
                raise ValueError("Invalid verification record")
            records[path] = record
            end = manifest.tell()
        return records, end

    def hash_file(self, parent, name, on_chunk=None):
        digest, size = hashlib.sha256(), 0
        with os.fdopen(os.open(name, READ, dir_fd=parent), "rb") as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ValueError("Resume requires a regular file")
            while True:
                self.check()
                chunk = source.read(CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                digest.update(chunk)
                if on_chunk:
                    on_chunk(len(chunk))
            after = os.fstat(source.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise RuntimeError("File changed during resume verification")
        return size, digest.hexdigest()

    def verified_record(self, path):
        record = (self.resume_records or {}).get(path)
        if record and record["status"] != "verified":
            raise RuntimeError("Source item changed type since the previous attempt: " + path)
        return record

    def reuse(self, dest, name, path, record, source_hash, job):
        expected = (record["size"], record["sha256"])
        if source_hash != expected:
            raise RuntimeError("Source no longer matches the saved transfer. Select the original camera/card: " + path)
        if self.hash_file(dest, name, lambda n: self.advance_phase(job, n)) != expected:
            raise RuntimeError("Saved NAS file is missing or changed; it was not overwritten: " + path)
        self.resume_seen.add(path)
        self.update(job, files=job["files"] + 1, bytes=job["bytes"] + record["size"],
                    reused_files=job["reused_files"] + 1, reused_bytes=job["reused_bytes"] + record["size"],
                    checked_bytes=job.get("checked_bytes", 0) + record["size"])

    def prepare_file(self, dest, name, path, job):
        if self.resume_records is not None:
            try:
                existing = os.stat(name, dir_fd=dest, follow_symlinks=False)
            except FileNotFoundError:
                existing = None
            if existing:
                if path != self.resume_pending or not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1:
                    raise RuntimeError("Unexpected file in the incomplete transfer; left unchanged: " + path)
                # Only the recorded unfinished file belongs to this retry.
                # Unlink rather than truncate so a hard-link target cannot be changed.
                os.unlink(name, dir_fd=dest)
            self.resume_seen.add(path)
        self.checkpoint(job, current=path, pending_path=path, state="copying")

    def verify_saved(self, data, records, job):
        expected = (job["files"], job["bytes"], job["skipped"])
        files = total = skipped = 0
        self.update(job, state="resuming", reused_files=0, reused_bytes=0,
                    phase="rechecking", phase_bytes=0, phase_total=expected[1])
        for path, record in records.items():
            self.check()
            if record["status"] == "skipped":
                skipped += 1
                continue
            self.update(job, current=path)
            names = parts(path)
            with directory(data, "/".join(names[:-1])) as parent:
                if self.hash_file(parent, names[-1], lambda n: self.advance_phase(job, n)) != (record["size"], record["sha256"]):
                    raise RuntimeError("Saved NAS checksum mismatch; cannot finalize: " + path)
            files += 1
            total += record["size"]
            self.update(job, reused_files=files, reused_bytes=total)
        if (files, total, skipped) != expected:
            raise RuntimeError("Verification manifest does not match the saved completed transfer.")

    def check_resume_source(self, src, data, job, is_camera):
        # Check every completed file before replacing any unfinished file.
        # A wrong reconnect must not add new data to the saved transfer first.
        self.update(job, phase="rechecking", phase_bytes=0,
                    phase_total=2 * sum(r["size"] for r in self.resume_records.values() if r["status"] == "verified"))
        for path, record in self.resume_records.items():
            self.check()
            if record["status"] != "verified":
                continue
            self.update(job, state="resuming", current=path)
            names = parts(path)
            if is_camera:
                info = src.stat(path)
                if info["kind"] != "file":
                    raise RuntimeError("Source file changed type: " + path)
                digest, count = hashlib.sha256(), 0
                with src.stream(path, info, self.check) as chunks:
                    for chunk in chunks:
                        self.check()
                        digest.update(chunk)
                        count += len(chunk)
                        self.advance_phase(job, len(chunk))
                source_hash = (count, digest.hexdigest())
            else:
                with directory(src, "/".join(names[:-1])) as parent:
                    source_hash = self.hash_file(parent, names[-1], lambda n: self.advance_phase(job, n))
            with directory(data, "/".join(names[:-1])) as parent:
                self.reuse(parent, names[-1], path, record, source_hash, job)
        # Traversal must still encounter each previously recorded path.
        self.resume_seen.clear()
        self.update(job, phase="transfer")

    def count_queued(self, job, paths):
        self.update(job, state="counting", phase="counting", total_bytes=0, total_files=0, unknown_files=0)
        def progress(totals):
            self.check()
            self.update(job, total_bytes=totals["bytes"], total_files=totals["files"], unknown_files=totals["unknown_files"])
        if str(job["device"]).startswith("camera:"):
            self.hardware.camera_source(job["device"]).estimate(paths, self.check, progress)
        else:
            fd = self.hardware.source(job["device"])
            try:
                self.estimate_disk(fd, paths, self.check, progress)
            finally:
                os.close(fd)
        self.checkpoint(job, needs_count=False, phase="transfer", state="starting")

    def run(self, job, paths, resuming=False):
        self.resume_records = {} if resuming else None
        self.resume_seen = set()
        self.resume_pending = job.get("pending_path") if resuming else None
        try:
            if job.get("needs_count"):
                self.count_queued(job, paths)
            self.update(job, phase="transfer")
            with contextlib.ExitStack() as stack:
                is_camera = isinstance(job["device"], str) and job["device"].startswith("camera:")
                nas = self.hardware.nas()
                stack.callback(os.close, nas)
                target = stack.enter_context(directory(nas, job.get("destination_folder", "")))
                if not resuming and "total_bytes" in job:
                    self.check_space(target, job["total_bytes"])
                try:
                    os.mkdir("Pi-Imports", 0o700, dir_fd=target)
                except FileExistsError:
                    pass
                imports = stack.enter_context(directory(target, "Pi-Imports"))
                prefix = job.get("destination_folder", "")
                prefix = (prefix + "/" if prefix else "") + "Pi-Imports/"
                if not job.get("destination"):
                    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + "-" + job["id"]
                    self.checkpoint(job, destination=prefix + ".incomplete-" + stamp)
                prefix, incomplete, final = self.destination_parts(job)
                already_renamed = False
                try:
                    os.stat(final, dir_fd=imports, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    if not resuming or not job.get("copy_complete"):
                        raise FileExistsError("Final transfer folder already exists; left unchanged.")
                    try:
                        os.stat(incomplete, dir_fd=imports, follow_symlinks=False)
                    except FileNotFoundError:
                        already_renamed = True
                    else:
                        raise FileExistsError("Both transfer folders exist; neither was changed.")
                if not job.get("copy_complete"):
                    self.make_folder(imports, incomplete)
                # Close every handle inside the transfer before renaming it.
                # SMB servers may deny a directory rename while children are open.
                with contextlib.ExitStack() as files:
                    dest = files.enter_context(directory(imports, final if already_renamed else incomplete))
                    if not job.get("copy_complete"):
                        self.make_folder(dest, "data")
                    data = files.enter_context(directory(dest, "data"))
                    flags = READ if job.get("copy_complete") else (os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
                    if not resuming:
                        flags |= os.O_EXCL
                    manifest = files.enter_context(os.fdopen(os.open("checksums.jsonl", flags, 0o600, dir_fd=dest), "rb" if job.get("copy_complete") else "r+b"))
                    info = os.fstat(manifest.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise ValueError("Invalid verification manifest file")
                    if resuming:
                        self.resume_records, end = self.read_manifest(manifest, allow_tail=not job.get("copy_complete"))
                    if job.get("copy_complete"):
                        if job.get("layout") == "camera_date":
                            from organize import publish
                            publish(self, imports, dest, data, manifest, job)
                        else:
                            self.verify_saved(data, self.resume_records, job)
                    else:
                        if resuming:
                            manifest.seek(end)
                            manifest.truncate()
                            manifest.flush()
                            os.fsync(manifest.fileno())
                        if is_camera:
                            src = self.hardware.camera_source(job["device"])
                        else:
                            src = self.hardware.source(job["device"])
                            files.callback(os.close, src)
                        if self.resume_pending and self.resume_pending not in (self.resume_records or {}):
                            if is_camera:
                                pending_info = src.stat(self.resume_pending)
                                regular = pending_info["kind"] == "file"
                            else:
                                names = parts(self.resume_pending)
                                with directory(src, "/".join(names[:-1])) as parent:
                                    regular = stat.S_ISREG(os.stat(names[-1], dir_fd=parent, follow_symlinks=False).st_mode)
                            if not regular:
                                raise RuntimeError("The unfinished source file changed type. Select the original camera/card.")
                        self.update(job, state="resuming" if resuming else "copying", files=0, bytes=0, skipped=0, reused_files=0, reused_bytes=0, checked_bytes=0)
                        if resuming:
                            self.check_resume_source(src, data, job, is_camera)
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
                        if resuming and (set(self.resume_records) - self.resume_seen
                                         or self.resume_pending and self.resume_pending not in self.resume_seen):
                            raise RuntimeError("Source is missing previously selected files. Select the original camera/card.")
                        self.check()
                        manifest.flush()
                        os.fsync(manifest.fileno())
                        self.checkpoint(job, copy_complete=True, pending_path=None, state="finalizing")
                        if job.get("layout") == "camera_date":
                            from organize import publish
                            publish(self, imports, dest, data, manifest, job)
                self.check()
                self.update(job, state="finalizing", current="")
                if not already_renamed:
                    try:
                        os.stat(final, dir_fd=imports, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        raise FileExistsError("Final transfer folder appeared during copying; left unchanged.")
                    try:
                        from organize import rename_exclusive
                        rename_exclusive(imports, incomplete, imports, final)
                    except PermissionError as exc:
                        raise RuntimeError("All files verified, but the NAS denied the final folder rename. Check NAS rename permissions, then Resume; no files need copying again.") from exc
                self.update(job, state="complete_with_skips" if job["skipped"] else "complete", current="", destination=prefix + final)
        except Cancelled as exc:
            self.update(job, state="cancelled", error=str(exc))
        except Exception as exc:
            self.update(job, state="failed", error=str(exc)[:800])
        finally:
            self.resume_records = None
            self.resume_pending = None
            self.resume_seen = set()
            try:
                with self.lock:
                    self.save()
            finally:
                self.release_operation(transfer=True)

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
            self.make_folder(dest, name)
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
        record = self.verified_record(path)
        if record:
            self.resume_seen.add(path)
            return
        self.prepare_file(dest, name, path, job)
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
            self.make_folder(dest, name)
            with directory(src, name) as s, directory(dest, name) as d:
                self.copy_dir(s, d, path, job, manifest, depth + 1)
        elif stat.S_ISREG(before.st_mode):
            self.copy_file(src, dest, name, path, job, manifest)
        else:
            record = (self.resume_records or {}).get(path)
            if record and record["status"] != "skipped":
                raise RuntimeError("Source item changed type since the previous attempt: " + path)
            self.resume_seen.add(path)
            self.update(job, skipped=job["skipped"] + 1)
            if not record:
                self.write_record(manifest, {"path": path, "status": "skipped", "reason": "Symlink or special file"})

    def copy_file(self, src, dest, name, path, job, manifest):
        record = self.verified_record(path)
        if record:
            self.resume_seen.add(path)
            return
        self.update(job, state="copying", current=path)
        with os.fdopen(os.open(name, READ, dir_fd=src), "rb") as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ValueError("Source changed type")
            self.prepare_file(dest, name, path, job)
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
                self.update(job, checked_bytes=job.get("checked_bytes", 0) + len(chunk))
        if digest.digest() != check.digest():
            raise RuntimeError("Checksum mismatch: " + path)
        if times is not None:
            os.utime(name, ns=times, dir_fd=dest, follow_symlinks=False)
        self.write_record(manifest, {"path": path, "size": copied, "sha256": digest.hexdigest(), "status": "verified", "source_mtime": times[1] / 1e9 if times else None})
        self.checkpoint(job, files=job["files"] + 1, state="copying", pending_path=None)

    @staticmethod
    def write_record(manifest, record):
        manifest.write((json.dumps(record) + "\n").encode())
        manifest.flush()
        os.fsync(manifest.fileno())

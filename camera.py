"""Camera metadata and bounded streams; native USB code stays out of the server."""
import contextlib
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import time


class Cameras:
    def __init__(self):
        self.command = [sys.executable, "-I", str(Path(__file__).with_name("camera_worker.py"))]
        self.metadata_timeout = 45
        self.idle_timeout = 120

    def request(self, action, **fields):
        try:
            result = subprocess.run(self.command, input=json.dumps(dict(action=action, **fields)),
                                    capture_output=True, text=True, timeout=self.metadata_timeout)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("Camera did not respond. Check its USB mode and reconnect it.") from exc
        if result.returncode:
            raise RuntimeError(result.stderr.strip()[:1000] or "Camera operation failed")
        return json.loads(result.stdout)

    def devices(self):
        return self.request("devices")

    def source(self, device):
        return CameraSource(self, device)

    def stream(self, device, path, expected, check):
        return self.stream_request({"action": "download", "device": device,
                                    "path": path, "expected": expected}, check)

    @contextlib.contextmanager
    def stream_request(self, request, check):
        process = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
        try:
            process.stdin.write(json.dumps(request).encode())
            process.stdin.close()
            yield self._chunks(process, check, request.get("action") == "estimate")
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            for pipe in (process.stdin, process.stdout, process.stderr):
                pipe.close()

    def _chunks(self, process, check, estimating=False):
        error = bytearray()
        deadline = time.monotonic() + self.idle_timeout
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            selector.register(process.stderr, selectors.EVENT_READ)
            while selector.get_map():
                check()
                if time.monotonic() > deadline:
                    raise RuntimeError("Camera size calculation stalled. No files were copied." if estimating else
                                       "Camera transfer stalled. Incomplete NAS copy retained.")
                for key, _ in selector.select(timeout=0.25):
                    chunk = os.read(key.fileobj.fileno(), 1024 * 1024)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    elif key.fileobj is process.stdout:
                        yield chunk
                        # Exclude time the caller spent writing/verifying NAS data.
                        deadline = time.monotonic() + self.idle_timeout
                    else:
                        error.extend(chunk[:max(0, 1000 - len(error))])
            while process.poll() is None:
                check()
                if time.monotonic() > deadline:
                    raise RuntimeError("Camera did not finish its size calculation" if estimating else
                                       "Camera did not finish its transfer")
                try:
                    process.wait(timeout=0.25)
                except subprocess.TimeoutExpired:
                    pass
        if process.returncode:
            raise RuntimeError(error.decode("utf-8", errors="replace").strip() or
                               ("Camera disconnected or its driver failed during size calculation." if estimating else
                                "Camera disconnected or its driver failed. Incomplete NAS copy retained."))


class CameraSource:
    def __init__(self, backend, device):
        self.backend, self.device = backend, device

    def listing(self, path="", offset=0):
        return self.backend.request("list", device=self.device, path=path, offset=offset)

    def stat(self, path):
        return self.backend.request("stat", device=self.device, path=path)

    def estimate(self, paths, check, progress):
        pending = b""
        complete = False
        with self.backend.stream_request({"action": "estimate", "device": self.device, "paths": paths}, check) as chunks:
            for chunk in chunks:
                pending += chunk
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    totals = json.loads(line)
                    if any(type(totals.get(k)) is not int or totals[k] < 0 for k in ("bytes", "files", "unknown_files", "skipped")):
                        raise ValueError("Invalid camera size metadata")
                    complete = totals.get("complete") is True
                    progress(totals)
                if len(pending) > 65536:
                    raise ValueError("Invalid camera size response")
        if pending or not complete:
            raise RuntimeError("Camera size calculation did not finish")

    def stream(self, path, expected, check):
        return self.backend.stream(self.device, path, expected, check)

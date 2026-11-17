import contextlib
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from camera import Cameras
from camera_worker import components, discover, entries, identity, open_camera, safe_name
from core import Cancelled, Hardware, Transfers
from tests.test_transfer import FakeHardware

DEVICE = "camera:001:003:0123456789abcdef"


class FakeCamera:
    def __init__(self):
        self.files = {"DCIM/100GOPRO/clip.MP4": b"original-camera-bytes" * 100000,
                      "DCIM/100GOPRO/photo.RAW": b"raw", "note.txt": b"sidecar"}
        self.folders = {"", "DCIM", "DCIM/100GOPRO", "empty"}
        self.opened = []
        self.closed = []
        self.short = False
        self.extra = False
        self.disconnect = False
        self.unknown_size = False

    def listing(self, path="", offset=0):
        result = []
        for kind, values in (("folder", self.folders), ("file", self.files)):
            for p in sorted(values):
                parent, _, name = p.rpartition("/")
                if p and parent == path:
                    result.append({"name": name, "kind": kind, "size": None})
        return {"items": result[offset:offset + 200], "more": len(result) > offset + 200, "offset": offset}

    def stat(self, path):
        if path in self.folders:
            return {"kind": "folder"}
        return {"kind": "file", "size": None if self.unknown_size else len(self.files[path]), "mtime": 1700000000}

    @contextlib.contextmanager
    def stream(self, path, expected, check):
        self.opened.append(path)
        def chunks():
            data = self.files[path]
            if self.short:
                data = data[:-1]
            if self.extra:
                data += b"extra"
            for offset in range(0, len(data), 65536):
                check()
                yield data[offset:offset + 65536]
                if self.disconnect:
                    raise RuntimeError("Camera disconnected")
        try:
            yield chunks()
        finally:
            self.closed.append(path)


class CameraTransfersTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.hw = FakeHardware(self.temp.name)
        self.camera = FakeCamera()
        self.hw.camera_source = lambda device: self.camera
        self.engine = Transfers(self.hw, Path(self.temp.name) / "history.json")

    def tearDown(self):
        self.temp.cleanup()

    def run_job(self, paths=None):
        self.engine.start(DEVICE, [""] if paths is None else paths)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.engine.operation.acquire(False):
                self.engine.operation.release()
                return self.engine.snapshot()[0]
            time.sleep(.01)
        self.fail("Camera job did not finish")

    def output(self, job):
        return self.hw.dst / job["destination"]

    def test_all_files_nested_folders_and_checksums(self):
        originals = dict(self.camera.files)
        job = self.run_job()
        self.assertEqual(job["state"], "complete")
        self.assertEqual(job["files"], 3)
        self.assertEqual(job["bytes"], sum(map(len, originals.values())))
        for path, payload in originals.items():
            dest = self.output(job) / "data" / path
            self.assertEqual(dest.read_bytes(), payload)
            self.assertEqual(int(dest.stat().st_mtime), 1700000000)
        self.assertTrue((self.output(job) / "data/empty").is_dir())
        checks = [json.loads(line) for line in (self.output(job) / "checksums.jsonl").read_text().splitlines()]
        for item in checks:
            self.assertEqual(item["sha256"], hashlib.sha256(originals[item["path"]]).hexdigest())
        self.assertEqual(self.camera.files, originals)
        self.assertEqual(self.camera.opened, self.camera.closed)

    def test_browsing_reads_no_file_bytes(self):
        result = self.engine.browse(DEVICE, "DCIM/100GOPRO", 0)
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual(self.camera.opened, [])
        self.assertNotIn("original-camera-bytes", json.dumps(result))

    def test_nested_selection_and_parent_deduplication(self):
        for paths in (["DCIM/100GOPRO/photo.RAW"], ["DCIM", "DCIM/100GOPRO/photo.RAW"]):
            job = self.run_job(paths)
            self.assertEqual(job["state"], "complete")
            self.assertEqual((self.output(job) / "data/DCIM/100GOPRO/photo.RAW").read_bytes(), b"raw")
            self.assertFalse((self.output(job) / "data/note.txt").exists())

    def test_paginated_copy_does_not_skip_files(self):
        self.camera.files = {f"{i:03}.RAW": b"x" for i in range(205)}
        self.camera.folders = {""}
        job = self.run_job()
        self.assertEqual(job["state"], "complete")
        self.assertEqual(job["files"], 205)

    def test_zero_length_and_unknown_size_objects(self):
        self.camera.files["zero.bin"] = b""
        self.camera.unknown_size = True
        job = self.run_job()
        self.assertEqual(job["state"], "complete")
        self.assertEqual((self.output(job) / "data/zero.bin").read_bytes(), b"")

    def test_disconnect_truncation_and_extra_data_never_complete(self):
        for flag in ("disconnect", "short", "extra"):
            setattr(self.camera, flag, True)
            job = self.run_job(["DCIM/100GOPRO/clip.MP4"])
            self.assertEqual(job["state"], "failed", flag)
            self.assertIn(".incomplete-", job["destination"])
            self.assertEqual(job["files"], 0)
            setattr(self.camera, flag, False)

    def test_checksum_mismatch_retains_incomplete(self):
        update = self.engine.update
        def corrupt(job, **fields):
            update(job, **fields)
            if fields.get("state") == "verifying":
                (self.output(job) / "data/note.txt").write_bytes(b"corrupt")
        with patch.object(self.engine, "update", side_effect=corrupt):
            job = self.run_job(["note.txt"])
        self.assertEqual(job["state"], "failed")
        self.assertIn("Checksum mismatch", job["error"])

    def test_cancel_closes_stream_and_keeps_source(self):
        originals = dict(self.camera.files)
        update = self.engine.update
        def cancel(job, **fields):
            update(job, **fields)
            if fields.get("bytes", 0):
                self.engine.cancel_event.set()
        with patch.object(self.engine, "update", side_effect=cancel):
            job = self.run_job(["DCIM/100GOPRO/clip.MP4"])
        self.assertEqual(job["state"], "cancelled")
        self.assertEqual(self.camera.opened, self.camera.closed)
        self.assertEqual(self.camera.files, originals)

    def test_missing_nas_never_downloads(self):
        self.hw.missing = True
        job = self.run_job()
        self.assertEqual(job["state"], "failed")
        self.assertEqual(self.camera.opened, [])
        self.assertEqual(list(self.hw.dst.iterdir()), [])

    def test_unsafe_camera_names_and_paths_cannot_escape(self):
        for path in ("../outside", "/etc/passwd", "DCIM/../secret", "DCIM\\secret", "nul\x00", "a//b"):
            with self.assertRaises(ValueError):
                self.engine.start(DEVICE, [path])
        self.camera.listing = lambda *args: {"items": [{"name": "../outside"}], "more": False}
        job = self.run_job()
        self.assertEqual(job["state"], "failed")
        self.assertFalse((Path(self.temp.name) / "outside").exists())

    def test_destination_symlink_refused(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (self.hw.dst / "Pi-Imports").symlink_to(outside)
        job = self.run_job()
        self.assertEqual(job["state"], "failed")
        self.assertEqual(self.camera.opened, [])
        self.assertEqual(list(outside.iterdir()), [])

    def test_status_does_not_probe_camera_during_transfer(self):
        self.engine.device_cache_time = -10
        first = self.engine.devices()
        self.engine.operation.acquire()
        try:
            with patch.object(self.hw, "devices", side_effect=AssertionError("USB probed while busy")):
                self.assertEqual(self.engine.devices(), first)
        finally:
            self.engine.operation.release()


class CameraWorkerTest(unittest.TestCase):
    def test_metadata_only_listing_and_duplicate_rejection(self):
        camera = Mock()
        camera.folder_list_folders.return_value = [("DCIM", 0)]
        camera.folder_list_files.return_value = [("clip.MP4", 0)]
        self.assertEqual([e["name"] for e in entries(camera, "")], ["DCIM", "clip.MP4"])
        self.assertEqual([call[0] for call in camera.method_calls], ["folder_list_folders", "folder_list_files"])
        camera.folder_list_files.return_value = [("DCIM", 0)]
        with self.assertRaises(ValueError):
            entries(camera, "")

    def test_name_and_depth_validation(self):
        for name in ("..", ".", "", "a/b", "a\\b", "a\n", "\x00", "x" * 256):
            with self.assertRaises(ValueError):
                safe_name(name)
        with self.assertRaises(ValueError):
            components("/".join(["a"] * 65))
        self.assertEqual(safe_name("photo 你好.RAW"), "photo 你好.RAW")

    def test_usb_fingerprint_changes_when_device_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            dev = root / "1-2"
            dev.mkdir()
            for name, value in {"busnum": "1", "devnum": "3", "idVendor": "1234", "idProduct": "5678", "serial": "first"}.items():
                (dev / name).write_text(value)
            first = identity("Camera", "usb:001,003", root)
            self.assertEqual(identity("Camera", "usb:001,003", root), first)
            (dev / "serial").write_text("second")
            self.assertNotEqual(identity("Camera", "usb:001,003", root), first)
            self.assertIsNone(identity("Camera", "disk:/etc", root))
            self.assertIsNone(identity("Camera", "usb:001,004", root))

    def test_discovery_accepts_only_identified_usb_cameras(self):
        gp = SimpleNamespace(Camera=SimpleNamespace(autodetect=lambda: [("GoPro", "usb:001,003"), ("GoPro", "usb:001,003"), ("Directory", "disk:/etc")]))
        with patch("camera_worker.identity", side_effect=lambda model, port: DEVICE if port.startswith("usb:") else None):
            devices = discover(gp)
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["id"], DEVICE)

    def test_stale_camera_never_opened(self):
        gp = Mock()
        with patch("camera_worker.discover", return_value=[]):
            with self.assertRaises(RuntimeError), open_camera(gp, DEVICE):
                pass
        gp.Camera.assert_not_called()
        with self.assertRaises(ValueError), open_camera(gp, "disk:/etc"):
            pass

    def test_camera_failure_does_not_hide_usb_drives(self):
        hw = Hardware({})
        with patch.object(hw, "helper", return_value=[{"id": "usb"}]), patch.object(hw.cameras, "devices", side_effect=RuntimeError("Camera unavailable")):
            self.assertEqual(hw.devices(), [{"id": "usb"}])
            self.assertEqual(hw.camera_error, "Camera unavailable")


class CameraProcessTest(unittest.TestCase):
    def setUp(self):
        self.backend = Cameras()

    def use_script(self, script):
        self.backend.command = [sys.executable, "-c", script]

    def test_binary_pipe_stream_and_bounded_chunks(self):
        self.use_script("import sys; sys.stdin.read(); sys.stdout.buffer.write(b'\\x00\\xff'*1200000)")
        with self.backend.stream(DEVICE, "clip.MP4", {}, lambda: None) as chunks:
            data = bytearray()
            for chunk in chunks:
                self.assertLessEqual(len(chunk), 1024 * 1024)
                data.extend(chunk)
        self.assertEqual(data, b"\x00\xff" * 1200000)

    def test_worker_failure_after_bytes_is_not_success(self):
        self.use_script("import sys; sys.stdin.read(); sys.stdout.buffer.write(b'partial'); sys.stderr.write('disconnected'); sys.exit(1)")
        with self.assertRaisesRegex(RuntimeError, "disconnected"):
            with self.backend.stream(DEVICE, "clip.MP4", {}, lambda: None) as chunks:
                list(chunks)

    def test_noisy_stderr_does_not_deadlock(self):
        self.use_script("import sys; sys.stdin.read(); sys.stderr.write('x'*2000000); sys.stdout.buffer.write(b'ok')")
        with self.backend.stream(DEVICE, "clip", {}, lambda: None) as chunks:
            self.assertEqual(b"".join(chunks), b"ok")

    def test_timeout_kills_child(self):
        self.use_script("import sys,time; sys.stdin.read(); time.sleep(30)")
        self.backend.idle_timeout = .1
        real_popen = subprocess.Popen
        children = []
        def spawn(*args, **kwargs):
            p = real_popen(*args, **kwargs)
            children.append(p)
            return p
        with patch("camera.subprocess.Popen", side_effect=spawn), self.assertRaisesRegex(RuntimeError, "stalled"):
            with self.backend.stream(DEVICE, "clip", {}, lambda: None) as chunks:
                list(chunks)
        self.assertIsNotNone(children[0].poll())

    def test_cancel_kills_child_and_closes_pipes(self):
        self.use_script("import sys,time; sys.stdin.read(); time.sleep(30)")
        real_popen = subprocess.Popen
        children = []
        def spawn(*args, **kwargs):
            p = real_popen(*args, **kwargs)
            children.append(p)
            return p
        def cancel():
            raise Cancelled("cancelled")
        with patch("camera.subprocess.Popen", side_effect=spawn), self.assertRaises(Cancelled):
            with self.backend.stream(DEVICE, "clip", {}, cancel) as chunks:
                list(chunks)
        self.assertIsNotNone(children[0].poll())
        self.assertTrue(all(pipe.closed for pipe in (children[0].stdin, children[0].stdout, children[0].stderr)))

    def test_metadata_timeout(self):
        self.use_script("import time; time.sleep(30)")
        self.backend.metadata_timeout = .1
        with self.assertRaisesRegex(RuntimeError, "did not respond"):
            self.backend.devices()

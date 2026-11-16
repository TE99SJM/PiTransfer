import hashlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from core import ACTIVE, DIRECTORY, Hardware, Transfers, listing, parts


class FakeHardware:
    def __init__(self, root):
        self.root = Path(root)
        self.src = self.root / "usb"
        self.dst = self.root / "nas"
        self.src.mkdir()
        self.dst.mkdir()
        self.missing = False

    def source(self, device):
        return os.open(self.src, DIRECTORY)

    def nas(self):
        if self.missing:
            raise RuntimeError("NAS is not mounted")
        return os.open(self.dst, DIRECTORY)

    def devices(self):
        return [{"id": "8:1:0123456789abcdef", "node": "/dev/sda1", "label": "CAMERA CARD", "size": 128000000000, "filesystem": "exfat", "usable": True, "reason": "", "mounted": True}]

    def nas_status(self):
        return {"ready": True, "free": 500000000000, "destination": "//nas.example/incoming/Pi-Imports"}

    def eject(self, device):
        return {"ok": True}


class TransfersTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.hw = FakeHardware(self.temp.name)
        self.engine = Transfers(self.hw, Path(self.temp.name) / "state" / "history.json")

    def tearDown(self):
        self.temp.cleanup()

    def run_job(self, paths=None):
        self.engine.start("usb", [""] if paths is None else paths)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if self.engine.operation.acquire(False):
                self.engine.operation.release()
                return self.engine.snapshot()[0]
            time.sleep(.01)
        self.fail("Transfer did not finish")

    def output(self, job):
        return self.hw.dst / job["destination"]

    def test_nested_copy_and_sha256_manifest_preserve_source(self):
        p = self.hw.src / "Camera"
        p.mkdir()
        (p / "empty").mkdir()
        content = b'\x00\xff' * 600000
        (p / "clip with spaces.mp4").write_bytes(content)
        (p / ".hidden").write_text("private")
        before = (p / "clip with spaces.mp4").stat()
        job = self.run_job()
        self.assertEqual(job["state"], "complete")
        self.assertEqual(job["files"], 2)
        self.assertEqual(job["bytes"], len(content) + 7)
        out = self.output(job)
        self.assertEqual((out / "data/Camera/clip with spaces.mp4").read_bytes(), content)
        self.assertTrue((out / "data/Camera/empty").is_dir())
        self.assertEqual((p / "clip with spaces.mp4").read_bytes(), content)
        self.assertEqual((p / "clip with spaces.mp4").stat().st_mtime_ns, before.st_mtime_ns)
        manifest = [json.loads(x) for x in (out / "checksums.jsonl").read_text().splitlines()]
        self.assertTrue(any(x["sha256"] == hashlib.sha256(content).hexdigest() for x in manifest))

    def test_selected_files_and_deduplicated_parent(self):
        (self.hw.src / "a").mkdir()
        (self.hw.src / "a/x").write_text("yes")
        (self.hw.src / "excluded").write_text("no")
        job = self.run_job(["a", "a/x", "a"])
        self.assertEqual(job["state"], "complete")
        self.assertEqual(job["files"], 1)
        self.assertFalse((self.output(job) / "data/excluded").exists())

    def test_individual_nested_selection(self):
        (self.hw.src / "a").mkdir()
        (self.hw.src / "a/x").write_text("yes")
        job = self.run_job(["a/x"])
        self.assertEqual((self.output(job) / "data/a/x").read_text(), "yes")

    def test_symlink_and_fifo_skipped_not_followed(self):
        outside = Path(self.temp.name) / "secret"
        outside.write_text("secret")
        (self.hw.src / "link").symlink_to(outside)
        os.mkfifo(self.hw.src / "fifo")
        job = self.run_job()
        self.assertEqual(job["state"], "complete_with_skips")
        self.assertEqual(job["skipped"], 2)
        self.assertEqual(list((self.output(job) / "data").iterdir()), [])

    def test_metadata_does_not_open_regular_file(self):
        (self.hw.src / "file").write_text("secret contents")
        fd = self.hw.source("usb")
        try:
            with patch("core.os.open", side_effect=AssertionError("Opened file")):
                result = listing(fd)
            self.assertEqual(result["items"][0]["name"], "file")
            self.assertNotIn("secret", json.dumps(result))
        finally:
            os.close(fd)

    def test_invalid_and_symlink_paths_refused(self):
        for path in ("../secret", "/etc", "a/../b", "a//b", ".", "a/", "a\x00"):
            with self.assertRaises(ValueError):
                parts(path)
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (outside / "secret").write_text("secret")
        (self.hw.src / "escape").symlink_to(outside)
        with self.assertRaises(OSError):
            self.engine.browse("usb", "escape", 0)
        job = self.run_job(["escape/secret"])
        self.assertEqual(job["state"], "failed")

    def test_destination_symlink_refused(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (self.hw.dst / "Pi-Imports").symlink_to(outside)
        (self.hw.src / "f").write_text("payload")
        job = self.run_job()
        self.assertEqual(job["state"], "failed")
        self.assertEqual(list(outside.iterdir()), [])

    def test_new_jobs_never_overwrite_previous_transfer(self):
        (self.hw.src / "f").write_text("first")
        first = self.run_job()
        (self.hw.src / "f").write_text("second")
        second = self.run_job()
        self.assertEqual((self.output(first) / "data/f").read_text(), "first")
        self.assertEqual((self.output(second) / "data/f").read_text(), "second")

    def test_missing_nas_does_not_write_any_payload(self):
        (self.hw.src / "f").write_text("payload")
        self.hw.missing = True
        job = self.run_job()
        self.assertEqual(job["state"], "failed")
        self.assertEqual(list(self.hw.dst.iterdir()), [])
        self.assertTrue((self.hw.src / "f").exists())

    def test_checksum_failure_retains_incomplete(self):
        (self.hw.src / "f").write_text("original")
        update = self.engine.update
        def corrupt(job, **fields):
            update(job, **fields)
            if fields.get("state") == "verifying":
                (self.output(job) / "data/f").write_text("CORRUPT!")
        with patch.object(self.engine, "update", side_effect=corrupt):
            job = self.run_job()
        self.assertEqual(job["state"], "failed")
        self.assertIn("Checksum mismatch", job["error"])
        self.assertIn(".incomplete-", job["destination"])
        self.assertEqual((self.hw.src / "f").read_text(), "original")

    def test_cancel_retains_original_and_incomplete(self):
        (self.hw.src / "f").write_bytes(b'x' * 2000000)
        update = self.engine.update
        def cancel(job, **fields):
            update(job, **fields)
            if fields.get("bytes", 0):
                self.engine.cancel_event.set()
        with patch.object(self.engine, "update", side_effect=cancel):
            job = self.run_job()
        self.assertEqual(job["state"], "cancelled")
        self.assertIn(".incomplete-", job["destination"])
        self.assertEqual((self.hw.src / "f").stat().st_size, 2000000)

    def test_busy_operations_refused(self):
        self.engine.operation.acquire()
        try:
            for call in (lambda: self.engine.start("usb", [""]), lambda: self.engine.eject("usb"), lambda: self.engine.browse("usb", "", 0)):
                with self.assertRaises(RuntimeError):
                    call()
        finally:
            self.engine.operation.release()

    def test_restart_marks_active_job_interrupted(self):
        self.engine.history_path.parent.mkdir()
        self.engine.history_path.write_text(json.dumps([{"id":"test", "state":"copying"}]))
        restarted = Transfers(self.hw, self.engine.history_path)
        self.assertEqual(restarted.snapshot()[0]["state"], "interrupted")

    def test_paginated_metadata(self):
        for i in range(205):
            (self.hw.src / str(i)).touch()
        result = self.engine.browse("usb", "", 0)
        self.assertEqual(len(result["items"]), 200)
        self.assertTrue(result["more"])
        self.assertEqual(len(self.engine.browse("usb", "", 200)["items"]), 5)

    def test_encrypted_mount_required(self):
        hw = Hardware({"nas_mount": "/mnt/pidropnas", "nas_share":"//nas/share"})
        from types import SimpleNamespace
        for options in ("rw,vers=3.0", "rw,vers=3.0,seal", "rw,vers=2.1,seal,cache=none"):
            result = SimpleNamespace(returncode=0, stdout=json.dumps({"filesystems":[{"target":"/mnt/pidropnas", "source":"//nas/share", "fstype":"cifs", "options":options}]}))
            with patch("core.subprocess.run", return_value=result), self.assertRaises(RuntimeError):
                hw.nas()


if __name__ == "__main__":
    unittest.main()

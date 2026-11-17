"""Resume preserves completed data and refuses changed or unrelated files."""
import errno
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from core import ACTIVE, Transfers
from tests.test_transfer import FakeHardware
from tests.test_cameras import DEVICE, FakeCamera


class ResumeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.hw = FakeHardware(self.temp.name)
        self.history = Path(self.temp.name) / 'history.json'
        self.engine = Transfers(self.hw, self.history)
        (self.hw.src / 'first').write_bytes(b'first original')
        (self.hw.src / 'second').write_bytes(b'second original' * 100000)

    def tearDown(self):
        self.temp.cleanup()

    def wait(self):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.engine.operation.acquire(False):
                self.engine.operation.release()
                return self.engine.snapshot()[0]
            time.sleep(.005)
        self.fail('Job did not finish')

    def output(self, job):
        return self.hw.dst / job['destination']

    def partial(self, camera=False):
        update = self.engine.update
        def cancel(job, **fields):
            update(job, **fields)
            if fields.get('bytes', 0) > len(b'first original') and job['files'] >= 1:
                self.engine.cancel_event.set()
        if camera:
            self.camera = FakeCamera()
            self.camera.files = {'first': b'first original', 'second': b'second original' * 100000}
            self.camera.folders = {''}
            self.hw.camera_source = lambda device: self.camera
        with patch.object(self.engine, 'update', side_effect=cancel):
            self.engine.start(DEVICE if camera else 'usb', ['first', 'second'])
            job = self.wait()
        self.assertEqual(job['state'], 'cancelled', job)
        self.assertEqual(job['files'], 1)
        self.assertTrue(job['can_resume'])
        self.assertTrue(job['resume_needs_source'])
        return job

    def rename_failure(self, legacy=False):
        from organize import rename_exclusive
        rename = rename_exclusive
        def deny(src_fd, src, dst_fd, dst):
            if str(src).startswith('.incomplete-'):
                # Reproduce the SMB final-rename permission error.
                raise PermissionError(errno.EACCES, 'Permission denied', src, None, dst)
            return rename(src_fd, src, dst_fd, dst)
        with patch('organize.rename_exclusive', side_effect=deny):
            self.engine.start('usb', ['first', 'second'])
            job = self.wait()
        self.assertEqual(job['state'], 'failed', job)
        self.assertTrue(job['copy_complete'])
        self.assertEqual(job['files'], 2)
        if legacy:
            saved = self.engine.jobs[0]
            _, incomplete, final = self.engine.destination_parts(saved)
            for key in ('copy_complete', 'paths', 'resume_version', 'pending_path', 'attempts'):
                saved.pop(key, None)
            saved['error'] = str(PermissionError(errno.EACCES, 'Permission denied', incomplete, None, final))
            self.engine.save()
            self.engine = Transfers(self.hw, self.history)
            job = self.engine.snapshot()[0]
        return job

    def test_partial_disk_resume_reuses_verified_file_and_same_folder(self):
        job = self.partial()
        original_folder = self.output(job)
        first = original_folder / 'data/first'
        inode, modified = first.stat().st_ino, first.stat().st_mtime_ns
        original_sources = {p.name: p.read_bytes() for p in self.hw.src.iterdir()}
        self.engine.resume(job['id'])
        result = self.wait()
        self.assertEqual(result['state'], 'complete', result)
        self.assertEqual(result['id'], job['id'])
        self.assertEqual(result['reused_files'], 1)
        self.assertEqual(result['files'], 2)
        self.assertEqual(result['bytes'], sum(map(len, original_sources.values())))
        self.assertEqual(len(list((self.hw.dst/'Pi-Imports').iterdir())), 1)
        self.assertEqual((self.output(result)/'data/first').stat().st_ino, inode)
        self.assertEqual((self.output(result)/'data/first').stat().st_mtime_ns, modified)
        for name, content in original_sources.items():
            self.assertEqual((self.output(result)/'data'/name).read_bytes(), content)
            self.assertEqual((self.hw.src/name).read_bytes(), content)
        records = [json.loads(line) for line in (self.output(result)/'checksums.jsonl').read_text().splitlines()]
        self.assertEqual(len(records), 2)

    def test_partial_camera_reconnect_rechecks_source_and_reuses_verified_data(self):
        job = self.partial(camera=True)
        inode = (self.output(job)/'data/first').stat().st_ino
        self.engine = Transfers(self.hw, self.history)
        replacement_id = 'camera:004:009:aaaaaaaaaaaaaaaa'
        self.engine.resume(job['id'], replacement_id)
        result = self.wait()
        self.assertEqual(result['state'], 'complete', result)
        self.assertEqual(result['device'], replacement_id)
        self.assertEqual(result['reused_files'], 1)
        self.assertEqual((self.output(result)/'data/first').stat().st_ino, inode)
        self.assertEqual((self.output(result)/'data/second').read_bytes(), self.camera.files['second'])

    def test_restart_has_durable_selection_and_pending_file(self):
        job = self.partial()
        saved = json.loads(self.history.read_text())[0]
        self.assertEqual(saved['paths'], ['first', 'second'])
        self.assertEqual(saved['pending_path'], 'second')
        saved['state'] = 'copying'
        self.history.write_text(json.dumps([saved]))
        self.engine = Transfers(self.hw, self.history)
        self.assertEqual(self.engine.snapshot()[0]['state'], 'interrupted')
        self.engine.resume(job['id'])
        self.assertEqual(self.wait()['state'], 'complete')

    def test_new_and_legacy_rename_failure_recovery_needs_no_source(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                job = self.rename_failure(legacy)
                self.assertFalse(job['resume_needs_source'])
                self.assertTrue(job['can_resume'])
                with patch.object(self.hw, 'source', side_effect=AssertionError('Source must not be opened')):
                    self.engine.resume(job['id'])
                    result = self.wait()
                self.assertEqual(result['state'], 'complete', result)
                self.assertEqual(result['reused_files'], 2)
                self.assertEqual(len(list(self.output(result).glob('data/*'))), 2)
                # Next subtest gets a new isolated job history.
                self.engine.jobs = []
                self.engine.save()

    def test_finalization_closes_child_handles_before_rename(self):
        from organize import rename_exclusive
        rename = rename_exclusive
        checked = []
        def check(src_fd, src, dst_fd, dst):
            if str(src).startswith('.incomplete-'):
                opened = []
                for fd in Path('/proc/self/fd').iterdir():
                    try:
                        opened.append(os.readlink(fd))
                    except FileNotFoundError:
                        pass
                self.assertFalse(any('/'+src in path for path in opened), opened)
                checked.append(True)
            return rename(src_fd, src, dst_fd, dst)
        with patch('organize.rename_exclusive', side_effect=check):
            self.engine.start('usb', ['first'])
            result = self.wait()
        self.assertEqual(result['state'], 'complete', result)
        self.assertTrue(checked)

    def test_resume_after_rename_succeeded_before_history_save(self):
        job = self.rename_failure()
        prefix, incomplete, final = self.engine.destination_parts(job)
        os.rename(self.hw.dst/prefix/incomplete, self.hw.dst/prefix/final)
        self.engine.jobs[0]['state'] = 'finalizing'
        self.engine.save()
        self.engine = Transfers(self.hw, self.history)
        self.engine.resume(job['id'])
        self.assertEqual(self.wait()['state'], 'complete')

    def test_corrupted_verified_nas_file_is_not_overwritten(self):
        job = self.partial()
        target = self.output(job)/'data/first'
        target.write_bytes(b'changed on NAS')
        self.engine.resume(job['id'])
        result = self.wait()
        self.assertEqual(result['state'], 'failed')
        self.assertIn('not overwritten', result['error'])
        self.assertEqual(target.read_bytes(), b'changed on NAS')

    def test_changed_source_is_not_combined_with_saved_copy(self):
        job = self.partial(camera=True)
        self.camera.files['first'] = b'other camera content'
        self.engine.resume(job['id'], 'camera:004:008:aaaaaaaaaaaaaaaa')
        result = self.wait()
        self.assertEqual(result['state'], 'failed')
        self.assertIn('original camera/card', result['error'])
        self.assertEqual((self.output(job)/'data/first').read_bytes(), b'first original')

    def test_missing_pending_source_is_repeatedly_refused(self):
        job = self.partial()
        (self.hw.src/'second').unlink()
        for _ in range(2):
            self.engine.resume(job['id'])
            result = self.wait()
            self.assertEqual(result['state'], 'failed')
            self.assertEqual(result['pending_path'], 'second')

    def test_partial_symlink_or_hardlink_never_changes_target(self):
        for kind in ('symlink', 'hardlink'):
            with self.subTest(kind=kind):
                job = self.partial()
                partial = self.output(job)/'data/second'
                partial.unlink()
                outside = Path(self.temp.name)/('outside-'+kind)
                outside.write_bytes(b'keep me')
                if kind == 'symlink':
                    partial.symlink_to(outside)
                else:
                    os.link(outside, partial)
                self.engine.resume(job['id'])
                result = self.wait()
                self.assertEqual(result['state'], 'failed')
                self.assertEqual(outside.read_bytes(), b'keep me')
                self.engine.jobs = []
                self.engine.save()

    def test_truncated_last_manifest_record_retries_only_pending_file(self):
        job = self.partial()
        with (self.output(job)/'checksums.jsonl').open('ab') as manifest:
            manifest.write(b'{"path":"second","sha256":')
        self.engine.resume(job['id'])
        result = self.wait()
        self.assertEqual(result['state'], 'complete', result)
        records = [json.loads(line) for line in (self.output(result)/'checksums.jsonl').read_text().splitlines()]
        self.assertEqual(len(records), 2)

    def test_manifest_path_escape_refused(self):
        job = self.partial()
        outside = Path(self.temp.name)/'outside'
        outside.write_bytes(b'keep')
        with (self.output(job)/'checksums.jsonl').open('ab') as manifest:
            manifest.write((json.dumps({'path':'../../outside','status':'verified','size':4,'sha256':hashlib.sha256(b'keep').hexdigest()})+'\n').encode())
        self.engine.resume(job['id'])
        self.assertEqual(self.wait()['state'], 'failed')
        self.assertEqual(outside.read_bytes(), b'keep')

    def test_finalize_rechecks_manifest_totals_and_hashes(self):
        job = self.rename_failure()
        (self.output(job)/'data/first').write_bytes(b'wrong')
        self.engine.resume(job['id'])
        result = self.wait()
        self.assertEqual(result['state'], 'failed')
        self.assertIn('checksum mismatch', result['error'])
        self.assertTrue(self.output(job).exists())

    def test_final_directory_collision_preserved(self):
        job = self.rename_failure()
        prefix, _, final = self.engine.destination_parts(job)
        other = self.hw.dst/prefix/final
        other.mkdir()
        (other/'unrelated').write_bytes(b'keep')
        self.engine.resume(job['id'])
        result = self.wait()
        self.assertEqual(result['state'], 'failed')
        self.assertEqual((other/'unrelated').read_bytes(), b'keep')

    def test_legacy_partial_has_no_resume_and_complete_cannot_resume(self):
        job = self.partial()
        self.engine.jobs[0].pop('paths')
        self.engine.jobs[0].pop('resume_version')
        self.assertFalse(self.engine.snapshot()[0]['can_resume'])
        with self.assertRaises(ValueError):
            self.engine.resume(job['id'])
        self.engine.start('usb', ['first'])
        job = self.wait()
        self.assertFalse(job['can_resume'])
        with self.assertRaises(ValueError):
            self.engine.resume(job['id'])

    def test_resume_cannot_run_during_other_operation(self):
        job = self.partial()
        self.engine.operation.acquire()
        try:
            with self.assertRaises(RuntimeError):
                self.engine.resume(job['id'])
        finally:
            self.engine.operation.release()

    def test_cancel_during_recheck_retains_can_resume(self):
        job = self.rename_failure()
        original_hash = self.engine.hash_file
        def cancel(*args):
            self.engine.cancel_event.set()
            return original_hash(*args)
        with patch.object(self.engine, 'hash_file', side_effect=cancel):
            self.engine.resume(job['id'])
            result = self.wait()
        self.assertEqual(result['state'], 'cancelled')
        self.assertTrue(result['can_resume'])
        self.engine.resume(job['id'])
        self.assertEqual(self.wait()['state'], 'complete')

    def test_missing_nas_does_not_fall_back_to_local_storage(self):
        job = self.partial()
        self.hw.missing = True
        self.engine.resume(job['id'])
        result = self.wait()
        self.assertEqual(result['state'], 'failed')
        self.assertIn('not mounted', result['error'])


if __name__ == '__main__':
    unittest.main()

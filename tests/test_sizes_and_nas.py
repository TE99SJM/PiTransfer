import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from camera import Cameras
from camera_worker import estimate as estimate_camera
from core import Transfers
from tests.test_cameras import DEVICE, FakeCamera
from tests.test_transfer import FakeHardware


class SizeAndNasTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.hw = FakeHardware(self.temp.name)
        self.camera = FakeCamera()
        self.hw.camera_source = lambda device: self.camera
        self.engine = Transfers(self.hw, Path(self.temp.name) / 'history.json')

    def tearDown(self):
        self.temp.cleanup()

    def wait(self):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.engine.operation.acquire(False):
                self.engine.operation.release()
                return
            time.sleep(.005)
        self.fail('Operation did not finish')

    def estimate(self, paths, device='usb'):
        self.engine.estimate(device, paths)
        self.wait()
        return self.engine.estimate_snapshot()

    def test_disk_totals_nested_selection_large_files_without_reading_bytes(self):
        folder = self.hw.src / 'DCIM'
        folder.mkdir()
        with (folder / 'large.mp4').open('wb') as f:
            f.truncate(5 * 1024**3 + 7)
        (folder / 'photo.raw').write_bytes(b'raw')
        (self.hw.src / 'excluded').write_bytes(b'excluded')
        real_open = os.open
        def directories_only(path, flags, *args, **kwargs):
            if not flags & os.O_DIRECTORY:
                raise AssertionError('Opened file content while estimating')
            return real_open(path, flags, *args, **kwargs)
        with patch('core.os.open', side_effect=directories_only), patch.object(self.hw, 'nas', side_effect=AssertionError('NAS accessed during estimate')):
            result = self.estimate(['DCIM', 'DCIM/large.mp4', 'DCIM'])
        self.assertEqual(result['state'], 'complete')
        self.assertEqual(result['files'], 2)
        self.assertEqual(result['bytes'], 5 * 1024**3 + 10)
        self.assertEqual(result['paths'], ['DCIM'])
        self.assertEqual(list(self.hw.dst.iterdir()), [])

    def test_symlinks_and_special_files_are_excluded_from_size(self):
        (self.hw.src / 'file').write_bytes(b'12345')
        (self.hw.src / 'link').symlink_to(self.hw.src / 'file')
        os.mkfifo(self.hw.src / 'pipe')
        result = self.estimate([''])
        self.assertEqual((result['files'], result['bytes'], result['skipped']), (1, 5, 2))

    def test_invalid_and_symlink_paths_refused(self):
        for paths in ([], ['../secret'], ['/etc'], ['a//b'], [[]]):
            with self.assertRaises(ValueError):
                self.engine.estimate('usb', paths)
        (self.hw.src / 'escape').symlink_to(self.hw.dst)
        result = self.estimate(['escape/secret'])
        self.assertEqual(result['state'], 'failed')

    def test_camera_totals_and_unknown_sizes_never_download(self):
        result = self.estimate(['DCIM', 'DCIM/100GOPRO/photo.RAW'], DEVICE)
        self.assertEqual(result['files'], 2)
        self.assertEqual(result['bytes'], sum(len(v) for k, v in self.camera.files.items() if k.startswith('DCIM/')))
        self.assertEqual(self.camera.opened, [])
        self.camera.unknown_size = True
        result = self.estimate([''], DEVICE)
        self.assertEqual(result['files'], 3)
        self.assertEqual(result['unknown_files'], 3)
        self.assertEqual(result['bytes'], 0)

    def test_cancel_and_failure_release_device_lock(self):
        original = self.engine.update
        def cancel(job, **fields):
            original(job, **fields)
            if fields.get('files', 0):
                self.engine.size_cancel.set()
        with patch.object(self.engine, 'update', side_effect=cancel):
            result = self.estimate([''], DEVICE)
        self.assertEqual(result['state'], 'cancelled')
        with patch.object(self.camera, 'estimate', side_effect=RuntimeError('Disconnected')):
            result = self.estimate([''], DEVICE)
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(self.camera.opened, [])
        self.assertEqual(self.estimate([''], DEVICE)['state'], 'complete')

    def test_size_scan_cannot_overlap_other_operations(self):
        self.engine.operation.acquire()
        try:
            with self.assertRaises(RuntimeError):
                self.engine.estimate(DEVICE, [''])
        finally:
            self.engine.operation.release()

    def test_matching_estimate_attached_to_transfer_and_checked_for_space(self):
        (self.hw.src / 'clip').write_bytes(b'12345')
        estimate = self.estimate(['clip'])
        self.engine.start('usb', ['clip'], estimate['id'])
        self.wait()
        job = self.engine.snapshot()[0]
        self.assertEqual(job['state'], 'complete')
        self.assertEqual((job['total_bytes'], job['total_files']), (5, 1))
        with self.assertRaises(ValueError):
            self.engine.start('usb', [''], estimate['id'])
        with self.assertRaises(ValueError):
            self.engine.start('different-device', ['clip'], estimate['id'])
        with patch('core.time.time', return_value=estimate['completed_at']+301), self.assertRaises(ValueError):
            self.engine.start('usb', ['clip'], estimate['id'])

    def test_insufficient_space_fails_before_creating_transfer_directory(self):
        estimate = self.estimate([''], DEVICE)
        with patch('core.os.fstatvfs', return_value=SimpleNamespace(f_bavail=1, f_frsize=1)):
            self.engine.start(DEVICE, [''], estimate['id'])
            self.wait()
        self.assertEqual(self.engine.snapshot()[0]['state'], 'failed')
        self.assertEqual(self.camera.opened, [])
        self.assertEqual(list(self.hw.dst.iterdir()), [])

    def test_nas_lists_folders_only_and_can_browse_nested_mount_like_folder(self):
        (self.hw.dst / 'Video').mkdir()
        (self.hw.dst / 'Video/External Drive').mkdir()
        (self.hw.dst / 'file.txt').write_text('private file bytes')
        (self.hw.dst / 'link').symlink_to(self.hw.src)
        root = self.engine.browse_nas('')
        self.assertEqual(root['items'], [{'name': 'Video'}])
        child = self.engine.browse_nas('Video')
        self.assertEqual(child['items'], [{'name': 'External Drive'}])
        self.assertNotIn('private', json.dumps(root))
        self.assertGreater(root['free'], 0)

    def test_nas_folder_pagination_counts_folders_not_files(self):
        for i in range(205):
            (self.hw.dst / str(i)).mkdir()
            (self.hw.dst / (str(i)+'.txt')).touch()
        self.assertEqual(len(self.engine.browse_nas('')['items']), 200)
        self.assertTrue(self.engine.browse_nas('')['more'])
        self.assertEqual(len(self.engine.browse_nas('', 200)['items']), 5)

    def test_nas_traversal_symlinks_and_missing_mount_are_refused(self):
        for path in ('..', '/etc', 'Video/../Photos'):
            with self.assertRaises(ValueError):
                self.engine.browse_nas(path)
        (self.hw.dst / 'escape').symlink_to(self.hw.src)
        with self.assertRaises(OSError):
            self.engine.browse_nas('escape')
        self.hw.missing = True
        with self.assertRaises(RuntimeError):
            self.engine.browse_nas('')

    def test_transfer_uses_chosen_nas_folder(self):
        (self.hw.dst / 'Video').mkdir()
        (self.hw.dst / 'Video/existing.mp4').write_bytes(b'keep')
        (self.hw.src / 'clip.mp4').write_bytes(b'new')
        self.engine.start('usb', ['clip.mp4'], destination_folder='Video')
        self.wait()
        job = self.engine.snapshot()[0]
        self.assertEqual(job['state'], 'complete')
        self.assertTrue(job['destination'].startswith('Video/Pi-Imports/transfer-'))
        self.assertEqual((self.hw.dst / job['destination'] / 'data/clip.mp4').read_bytes(), b'new')
        self.assertEqual((self.hw.dst / 'Video/existing.mp4').read_bytes(), b'keep')
        self.assertFalse((self.hw.dst / 'Pi-Imports').exists())

    def test_destination_folder_cannot_escape_or_fall_back(self):
        for path in ('../outside', '/etc'):
            with self.assertRaises(ValueError):
                self.engine.start('usb', [''], destination_folder=path)
        (self.hw.dst / 'escape').symlink_to(self.hw.src)
        for folder in ('escape', 'missing-folder'):
            self.engine.start('usb', [''], destination_folder=folder)
            self.wait()
            self.assertEqual(self.engine.snapshot()[0]['state'], 'failed')
            self.assertFalse((self.hw.src / 'Pi-Imports').exists())
            self.assertFalse((self.hw.dst / 'Pi-Imports').exists())

    def test_camera_worker_counts_metadata_in_one_session(self):
        progress = []
        with patch('camera_worker.entries', side_effect=lambda camera, path: self.camera.listing(path)['items']), patch('camera_worker.file_info', side_effect=lambda gp,camera,path: self.camera.stat(path)):
            result = estimate_camera(None, self.camera, ['DCIM', 'DCIM/100GOPRO/clip.MP4'], progress.append)
        self.assertEqual(result['files'], 2)
        self.assertGreater(result['bytes'], 0)
        self.assertEqual(self.camera.opened, [])
        self.assertTrue(progress)

    def test_camera_size_stream_requires_successful_completion(self):
        backend = Cameras()
        totals = json.dumps({'bytes':123, 'files':2, 'unknown_files':1, 'skipped':0, 'complete':True})
        for returncode in (0, 1):
            backend.command = [sys.executable, '-c', f'import sys;sys.stdin.read();print({totals!r});sys.exit({returncode})']
            seen = []
            if returncode:
                with self.assertRaises(RuntimeError):
                    backend.source(DEVICE).estimate([''], lambda:None, seen.append)
            else:
                backend.source(DEVICE).estimate([''], lambda:None, seen.append)
                self.assertEqual(seen[-1]['bytes'], 123)
        backend.command = [sys.executable, '-c', 'import sys;sys.stdin.read()']
        with self.assertRaisesRegex(RuntimeError, 'did not finish'):
            backend.source(DEVICE).estimate([''], lambda:None, lambda _:None)

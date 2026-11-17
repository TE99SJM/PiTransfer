"""One copy worker, independent job progress, safe camera discovery and queueing."""
import contextlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from core import ACTIVE, Transfers
from camera_worker import detect_ports
from tests.test_cameras import FakeCamera
from tests.test_transfer import FakeHardware

A = 'camera:001:003:aaaaaaaaaaaaaaaa'
B = 'camera:002:004:bbbbbbbbbbbbbbbb'


class QueueProgressTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.hw = FakeHardware(self.temp.name)
        self.history = Path(self.temp.name)/'history.json'
        self.engine = Transfers(self.hw, self.history)
        self.cameras = {A:FakeCamera(), B:FakeCamera()}
        for camera in self.cameras.values():
            camera.files = {'video.MP4':b'original' * 150000}
            camera.folders = {''}
        self.connected = [A, B]
        self.discovery_exclusions = []
        def devices(exclude=None):
            self.discovery_exclusions.append(exclude)
            return [{'id':d,'label':'Camera '+('A' if d==A else 'B'), 'node':d,
                     'kind':'camera','filesystem':'PTP/MTP','size':None,'usable':True,'mounted':False}
                    for d in self.connected if ('usb:'+':'.join(d.split(':')[1:3]).replace(':',',')) not in (exclude or [])]
        self.hw.devices = devices
        def source(device):
            if device not in self.connected:
                raise RuntimeError('Camera disconnected or changed')
            return self.cameras[device]
        self.hw.camera_source = source
        self.engine.devices()
        self.gate = threading.Event()
        self.entered = threading.Event()
        self.original_stream = self.cameras[A].stream
        @contextlib.contextmanager
        def gated(path, expected, check):
            self.entered.set()
            if not self.gate.wait(8):
                raise RuntimeError('Test gate timed out')
            with self.original_stream(path, expected, check) as chunks:
                yield chunks
        self.cameras[A].stream = gated

    def tearDown(self):
        self.gate.set()
        self.wait()
        self.temp.cleanup()

    def wait(self):
        deadline = time.monotonic()+10
        while time.monotonic()<deadline:
            if self.engine.operation.acquire(False):
                self.engine.operation.release()
                return self.engine.snapshot()
            time.sleep(.005)
        self.fail('Worker did not finish')

    def start_blocked(self):
        job = self.engine.start(A, [''])
        self.assertTrue(self.entered.wait(3))
        return job

    def test_two_cameras_queue_and_finish_in_separate_folders(self):
        a = self.start_blocked()
        b = self.engine.start(B, [''], enqueue=True)
        jobs = {j['id']:j for j in self.engine.snapshot()}
        self.assertEqual(jobs[b['id']]['state'], 'queued')
        self.assertEqual(jobs[b['id']]['queue_position'], 1)
        self.assertEqual(self.cameras[B].opened, [])
        self.assertEqual(jobs[a['id']]['device_label'], 'Camera A')
        self.gate.set()
        jobs = {j['id']:j for j in self.wait()}
        for job in jobs.values():
            self.assertEqual(job['state'], 'complete', job)
            self.assertEqual(job['bytes'], 1200000)
            self.assertEqual(job['checked_bytes'], 1200000)
            self.assertEqual((self.hw.dst/job['destination']/'data/video.MP4').read_bytes(), b'original'*150000)
        self.assertNotEqual(jobs[a['id']]['destination'], jobs[b['id']]['destination'])
        self.assertEqual(jobs[b['id']]['total_bytes'], 1200000)
        self.assertEqual(jobs[b['id']]['total_files'], 1)

    def test_removing_queued_job_does_not_cancel_active_camera(self):
        a = self.start_blocked()
        b = self.engine.start(B, [''], enqueue=True)
        self.engine.cancel(b['id'])
        self.assertFalse(self.engine.cancel_event.is_set())
        self.gate.set()
        jobs = {j['id']:j for j in self.wait()}
        self.assertEqual(jobs[a['id']]['state'], 'complete')
        self.assertEqual(jobs[b['id']]['state'], 'cancelled')
        self.assertEqual(self.cameras[B].opened, [])
        self.assertTrue(jobs[b['id']]['can_resume'])

    def test_cancel_active_advances_queue_without_cancelling_next(self):
        a = self.start_blocked()
        b = self.engine.start(B, [''], enqueue=True)
        self.engine.cancel(a['id'])
        self.gate.set()
        jobs = {j['id']:j for j in self.wait()}
        self.assertEqual(jobs[a['id']]['state'], 'cancelled')
        self.assertEqual(jobs[b['id']]['state'], 'complete')

    def test_disconnected_queued_device_never_copies_another_camera(self):
        self.start_blocked()
        b = self.engine.start(B, [''], enqueue=True)
        self.connected.remove(B)
        self.gate.set()
        jobs = {j['id']:j for j in self.wait()}
        self.assertEqual(jobs[b['id']]['state'], 'failed')
        self.assertIn('disconnected', jobs[b['id']]['error'])
        self.assertEqual(self.cameras[B].opened, [])
        self.assertEqual(jobs[b['id']]['bytes'], 0)

    def test_new_camera_discovery_excludes_active_usb_port(self):
        self.connected = [A]
        self.engine.device_cache_time = 0
        self.engine.devices()
        self.start_blocked()
        self.connected.append(B)
        self.engine.device_cache_time = 0
        found = self.engine.devices()
        self.assertEqual({d['id'] for d in found}, {A, B})
        self.assertEqual(self.discovery_exclusions[-1], ['usb:001,003'])
        self.assertEqual(self.cameras[B].opened, [])

    def test_queue_checks_known_device_and_prevents_duplicate_jobs(self):
        self.start_blocked()
        for device in (A, 'camera:003:005:cccccccccccccccc'):
            with self.assertRaises((ValueError, RuntimeError)):
                self.engine.start(device, [''], enqueue=True)
        self.engine.start(B, [''], enqueue=True)
        with self.assertRaises(RuntimeError):
            self.engine.start(B, [''], enqueue=True)

    def test_restart_preserves_waiting_requests_without_auto_copy(self):
        a = self.start_blocked()
        b = self.engine.start(B, [''], enqueue=True)
        restarted = Transfers(self.hw, self.history)
        jobs = {j['id']:j for j in restarted.snapshot()}
        self.assertEqual(jobs[a['id']]['state'], 'interrupted')
        self.assertEqual(jobs[b['id']]['state'], 'interrupted')
        self.assertEqual(jobs[b['id']]['paths'], [''])
        self.assertTrue(jobs[b['id']]['can_resume'])
        self.assertEqual(restarted.queue, [])

    def test_recheck_progress_moves_inside_large_file(self):
        payload = b'x' * 3000000
        (self.hw.src/'big').write_bytes(payload)
        rename = os.rename
        def fail(src, dst, **kwargs):
            if str(src).startswith('.incomplete-'):
                raise PermissionError('test rename failure')
            return rename(src,dst,**kwargs)
        with patch('core.os.rename', side_effect=fail):
            job = self.engine.start('usb', ['big'])
            self.wait()
        changes = []
        advance = self.engine.advance_phase
        def observe(job, amount):
            advance(job,amount)
            changes.append((job['phase'],job['phase_bytes'],job['phase_total']))
        with patch.object(self.engine,'advance_phase',side_effect=observe):
            self.engine.resume(job['id'])
            result = self.wait()[0]
        self.assertEqual(result['state'], 'complete')
        self.assertGreater(len(changes), 1)
        self.assertTrue(any(0 < current < total for _,current,total in changes))
        self.assertEqual(changes[-1], ('rechecking',len(payload),len(payload)))

    def test_estimate_and_browse_still_refuse_active_source(self):
        self.start_blocked()
        for action in (lambda:self.engine.browse(A,'',0), lambda:self.engine.estimate(A,['']), lambda:self.engine.eject(A)):
            with self.assertRaises(RuntimeError):
                action()


class FilteredDetectionTest(unittest.TestCase):
    def test_detector_only_receives_concrete_unoccupied_usb_ports(self):
        class Ports(list):
            thisown = True
            def load(self):
                self.extend([SimpleNamespace(get_path=lambda p=p:p) for p in ('usb:','usb:001,003','usb:002,004','disk:/')])
            def count(self):
                return len(self)
        observed = []
        class Abilities:
            def load(self): pass
            def detect(self, ports):
                observed.extend(p.get_path() for p in ports)
                return [('Camera B','usb:002,004')]
        gp = SimpleNamespace(PortInfoList=Ports,CameraAbilitiesList=Abilities)
        self.assertEqual(detect_ports(gp, ['usb:001,003']), [('Camera B','usb:002,004')])
        self.assertEqual(observed, ['usb:002,004'])
        observed.clear()
        detect_ports(gp, only='usb:001,003')
        self.assertEqual(observed, ['usb:001,003'])


if __name__ == '__main__':
    unittest.main()

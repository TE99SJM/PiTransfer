"""Deletion is explicit, receipt-scoped, and freshly verifies both copies."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from camera import Cameras
from core import Transfers, Cancelled, Hardware
import drive_helper as helper
from tests.test_cameras import DEVICE, FakeCamera
from tests.test_transfer import FakeHardware


class DeletableCamera(FakeCamera):
    def delete_verified(self,path,record,verify_backup,check,progress):
        check();data=self.files[path]
        if (len(data),hashlib.sha256(data).hexdigest())!=(record['size'],record['sha256']):
            raise RuntimeError('Original checksum changed; kept')
        progress(len(data));verify_backup();check();del self.files[path]


class CleanupTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.hw=FakeHardware(self.temp.name);self.camera=DeletableCamera()
        self.camera.files={'first.MP4':b'first','second.MP4':b'second'};self.camera.folders={''}
        self.hw.camera_source=lambda device:self.camera
        self.hw.file_metadata=lambda fd:{'CreateDate':'2026:11:17 12:00:00','Model':'GoPro HERO9 Black'}
        self.engine=Transfers(self.hw,self.root/'history.json')

    def tearDown(self):self.temp.cleanup()

    def wait(self):
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            if self.engine.operation.acquire(False):self.engine.operation.release();return self.engine.snapshot()[0]
            time.sleep(.005)
        self.fail('Operation did not finish')

    def copy(self,layout='camera_date',paths=None):
        self.engine.start(DEVICE,paths or [''],layout=layout);job=self.wait()
        self.assertEqual(job['state'],'complete',job);return job

    def delete(self,job):
        token=self.engine.prepare_delete(job['id'])
        self.engine.delete_verified(token['token'],token['confirmation']);return self.wait()

    def test_confirmation_required_one_use_and_no_automatic_deletion(self):
        job=self.copy();before=dict(self.camera.files)
        token=self.engine.prepare_delete(job['id']);self.assertEqual(token['files'],2)
        self.assertEqual(before,self.camera.files)
        with self.assertRaises(ValueError):self.engine.delete_verified(token['token'],'yes')
        with self.assertRaises(ValueError):self.engine.delete_verified(token['token'],token['confirmation'])
        self.assertEqual(before,self.camera.files)
        token=self.engine.prepare_delete(job['id']);self.engine.delete_tokens[token['token']]['expires']=0
        with self.assertRaises(ValueError):self.engine.delete_verified(token['token'],token['confirmation'])
        self.assertEqual(before,self.camera.files)

    def test_verified_camera_delete_keeps_nas_and_unselected_files(self):
        self.camera.files['untouched.RAW']=b'unselected'
        job=self.copy(paths=['first.MP4']);result=self.delete(job)
        self.assertEqual(result['cleanup']['state'],'complete',result)
        self.assertNotIn('first.MP4',self.camera.files);self.assertIn('untouched.RAW',self.camera.files)
        self.assertEqual((self.hw.dst/job['output_folders'][0]/'first.MP4').read_bytes(),b'first')
        audit=(self.hw.dst/job['destination']/'deleted.jsonl').read_text()
        self.assertEqual(json.loads(audit)['path'],'first.MP4');self.assertFalse(result['can_delete'])

    def test_changed_original_and_nas_are_never_deleted(self):
        for target in ('source','nas','missing_nas'):
            with self.subTest(target=target):
                job=self.copy(paths=['first.MP4']);output=self.hw.dst/job['output_folders'][0]
                # Use the manifest mapping because prior jobs can suffix names.
                entry=json.loads((self.hw.dst/job['destination']/'publish.json').read_text())['files'][0]
                backup=self.hw.dst/'Pi-Imports'/entry['output']
                if target=='source':self.camera.files['first.MP4']=b'changed'
                elif target=='nas':backup.write_bytes(b'corrupt')
                else:backup.unlink()
                result=self.delete(job)
                self.assertEqual(result['cleanup']['state'],'failed',result)
                self.assertIn('first.MP4',self.camera.files)
                self.assertEqual(result['cleanup']['files'],0)
                self.assertEqual(result['cleanup']['verified_files'],0)
                self.camera.files['first.MP4']=b'first'

    def test_failed_copy_cannot_delete(self):
        self.hw.missing=True;self.engine.start(DEVICE,['']);job=self.wait()
        with self.assertRaises(ValueError):self.engine.prepare_delete(job['id'])
        self.assertEqual(len(self.camera.files),2)

    def test_receipt_change_after_prepare_requires_new_confirmation(self):
        job=self.copy();token=self.engine.prepare_delete(job['id'])
        planpath=self.hw.dst/job['destination']/'publish.json';plan=json.loads(planpath.read_text())
        plan['files'][0]['output']='GoPro_HERO9_Black_2026-11-17/other.MP4';planpath.write_text(json.dumps(plan))
        self.engine.delete_verified(token['token'],token['confirmation']);result=self.wait()
        self.assertEqual(result['cleanup']['state'],'failed');self.assertEqual(len(self.camera.files),2)

    def test_cancel_between_files_retains_receipt_and_retry_only_remaining(self):
        job=self.copy();original=self.camera.delete_verified
        def cancel(*args):original(*args);self.engine.cancel_event.set()
        with patch.object(self.camera,'delete_verified',side_effect=cancel):result=self.delete(job)
        self.assertEqual(result['cleanup']['state'],'cancelled');self.assertEqual(len(self.camera.files),1)
        result=self.delete(result)
        self.assertEqual(result['cleanup']['state'],'complete');self.assertEqual(self.camera.files,{})
        self.assertEqual(result['cleanup']['previous_deleted_files'],1)
        self.assertEqual(len((self.hw.dst/job['destination']/'deleted.jsonl').read_text().splitlines()),2)

    def test_legacy_layout_deletion_and_restart_do_not_auto_delete(self):
        job=self.copy(layout='original')
        self.engine.jobs[-1].update(state='deleting',cleanup_previous_state='complete',cleanup={'state':'verifying'})
        self.engine.save();self.engine=Transfers(self.hw,self.root/'history.json')
        job=self.engine.snapshot()[0]
        self.assertEqual(job['state'],'complete');self.assertEqual(job['cleanup']['state'],'interrupted')
        self.assertEqual(len(self.camera.files),2)
        result=self.delete(job);self.assertEqual(result['cleanup']['state'],'complete')
        self.assertTrue((self.hw.dst/job['destination']/'data/first.MP4').exists())

    def test_missing_source_and_backup_symlink_fail_closed(self):
        job=self.copy();del self.camera.files['first.MP4']
        result=self.delete(job);self.assertEqual(result['cleanup']['state'],'failed');self.assertIn('second.MP4',self.camera.files)
        self.camera.files['first.MP4']=b'first'
        backup=self.hw.dst/job['output_folders'][0]/'first.MP4'
        outside=self.root/'other';outside.write_bytes(b'first');backup.unlink();backup.symlink_to(outside)
        result=self.delete(job);self.assertEqual(result['cleanup']['state'],'failed');self.assertEqual(len(self.camera.files),2)
        self.assertEqual(outside.read_bytes(),b'first')

    def test_reconnected_camera_deletion_excludes_actual_active_usb_port(self):
        from unittest.mock import Mock
        job=self.copy();replacement='camera:004:009:aaaaaaaaaaaaaaaa'
        active=self.engine.jobs[-1];active.update(state='deleting',cleanup={'device':replacement})
        self.engine.active_job=active
        self.hw.devices=Mock(return_value=[])
        self.engine.device_cache_time=0
        self.engine.devices()
        self.hw.devices.assert_called_once_with(exclude=['usb:004,009'])
        self.assertEqual(self.engine.snapshot()[0]['active_device'],replacement)
        active['state']='complete';self.engine.active_job=None

    def test_disk_deletion_discovery_uses_cache_instead_of_waiting_on_mount_lock(self):
        from unittest.mock import Mock
        job=self.copy();active=self.engine.jobs[-1]
        active.update(state='deleting',cleanup={'device':'8:1:0123456789abcdef'})
        self.engine.active_job=active;self.engine.device_cache_time=0
        self.hw.devices=Mock(side_effect=AssertionError('Would block on helper lock'))
        self.assertEqual(self.engine.devices(),[])
        self.hw.devices.assert_not_called();self.assertFalse(self.engine.discovering)
        active['state']='complete';self.engine.active_job=None

    def test_live_verification_and_deletion_counts_are_separate_and_snapshot_is_stable(self):
        job=self.copy(paths=['first.MP4'])
        reading,verified=threading.Event(),threading.Event()
        continue_read,continue_delete=threading.Event(),threading.Event()
        def gated(path,record,verify_backup,check,progress):
            progress(2);reading.set()
            if not continue_read.wait(5):raise RuntimeError('Read gate timed out')
            progress(record['size']-2);verify_backup();verified.set()
            if not continue_delete.wait(5):raise RuntimeError('Delete gate timed out')
            check();del self.camera.files[path]
        try:
            with patch.object(self.camera,'delete_verified',side_effect=gated):
                token=self.engine.prepare_delete(job['id'])
                self.engine.delete_verified(token['token'],token['confirmation'])
                self.assertTrue(reading.wait(3))
                first=self.engine.snapshot()[0]['cleanup']
                self.assertEqual(first['phase_bytes'],2);self.assertEqual(first['total_bytes'],10)
                self.assertEqual(first['files'],0);self.assertEqual(first['verified_files'],0)
                continue_read.set();self.assertTrue(verified.wait(3))
                middle=self.engine.snapshot()[0]['cleanup']
                self.assertEqual(middle['phase_bytes'],10);self.assertEqual(middle['verified_files'],1)
                self.assertEqual(middle['files'],0);self.assertIn('first.MP4',self.camera.files)
                self.assertEqual(first['phase_bytes'],2,'Earlier snapshot mutated with worker progress')
                continue_delete.set();result=self.wait()
                self.assertEqual(result['cleanup']['files'],1)
                self.assertEqual(result['cleanup']['verified_files'],1)
                self.assertEqual(result['cleanup']['state'],'complete')
                self.engine=Transfers(self.hw,self.root/'history.json')
                self.assertEqual(self.engine.snapshot()[0]['cleanup'],result['cleanup'])
        finally:
            continue_read.set();continue_delete.set();self.wait()

    def test_zero_byte_file_still_requires_verification_before_deletion_count(self):
        self.camera.files['empty.bin']=b''
        job=self.copy(paths=['empty.bin']);result=self.delete(job)
        self.assertEqual(result['cleanup']['total_bytes'],0)
        self.assertEqual(result['cleanup']['verified_files'],1)
        self.assertEqual(result['cleanup']['files'],1)

    def test_disk_ready_callback_reports_verification_before_delete_authorization(self):
        import subprocess
        real_popen=subprocess.Popen;marker=self.root/'delete-authorized'
        script='import json,sys; from pathlib import Path; json.loads(sys.stdin.buffer.readline()); print(json.dumps({"bytes":8}),flush=True); print(json.dumps({"ready":True}),flush=True); assert json.loads(sys.stdin.buffer.readline())=={"delete":True}; Path(sys.argv[1]).touch(); print(json.dumps({"deleted":True}),flush=True)'
        events=[]
        def ready():
            self.assertFalse(marker.exists());events.append('verified')
        hw=Hardware({})
        with patch('core.subprocess.Popen',side_effect=lambda *a,**kw:real_popen([sys.executable,'-u','-c',script,str(marker)],**kw)):
            hw.delete_verified('8:1:0123456789abcdef',dict(path='clip',backup_path='saved',size=4,sha256='a'*64),
                               lambda:None,lambda n:events.append(n),verified=ready)
        self.assertEqual(events,[8,'verified']);self.assertTrue(marker.exists())


class CameraDeleteProtocolTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.file=self.root/'clip.MP4';self.file.write_bytes(b'camera-payload'*100000)
        # Isolated subprocess exercises the binary/JSON handshake and real unlink.
        self.backend=Cameras();self.backend.command=[sys.executable,'-u','-c',
            'import json,sys; from pathlib import Path; q=json.loads(sys.stdin.buffer.readline()); p=Path(sys.argv[1]); sys.stdout.buffer.write(p.read_bytes()); sys.stdout.buffer.flush(); print(json.dumps({"ready":True}),flush=True); a=json.loads(sys.stdin.buffer.readline()); assert a=={"delete":True}; p.unlink(); print(json.dumps({"deleted":True}),flush=True)',str(self.file)]
        self.source=self.backend.source(DEVICE)
        self.source.stat=lambda path:{'kind':'file','size':self.file.stat().st_size,'mtime':1}
        self.record={'size':self.file.stat().st_size,'sha256':hashlib.sha256(self.file.read_bytes()).hexdigest()}

    def tearDown(self):self.temp.cleanup()

    def test_source_hash_then_nas_check_then_delete_acknowledgment(self):
        amounts=[];verified=[]
        def verify():self.assertTrue(self.file.exists());verified.append(True)
        self.source.delete_verified('clip.MP4',self.record,verify,lambda:None,amounts.append)
        self.assertEqual(verified,[True]);self.assertEqual(sum(amounts),self.record['size']);self.assertFalse(self.file.exists())

    def test_bad_source_hash_or_nas_failure_never_authorizes_delete(self):
        for bad_source in (True,False):
            record=dict(self.record,sha256='0'*64) if bad_source else self.record
            def failed():raise RuntimeError('NAS changed')
            with self.assertRaises(RuntimeError):self.source.delete_verified('clip.MP4',record,failed,lambda:None,lambda n:None)
            self.assertTrue(self.file.exists())


class DiskDeleteTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.src=self.root/'usb';self.nas=self.root/'nas';self.src.mkdir();self.nas.mkdir()
        (self.src/'clip').write_bytes(b'verified');(self.nas/'copy').write_bytes(b'verified')
        self.config=self.root/'config.json';self.config.write_text('{}')
        self.record={'path':'clip','backup_path':'copy','size':8,'sha256':hashlib.sha256(b'verified').hexdigest()}
        self.commands=[]

    def tearDown(self):self.temp.cleanup()

    def delete(self,record=None,command=None,confirm=None):
        with patch.object(helper,'validate_mount'),patch.object(helper,'nas_root',side_effect=lambda c:os.open(self.nas,helper.DIRECTORY)),patch.object(helper,'command',side_effect=command or (lambda args:self.commands.append(args))):
            return helper.delete_file({},self.src,record or self.record,config_path=str(self.config),
                                      emit=lambda r:None,confirm=confirm or (lambda:{'delete':True}),check=lambda:None)

    def test_matches_hashes_before_rw_and_restores_ro(self):
        self.assertEqual(self.delete(),{'deleted':True});self.assertFalse((self.src/'clip').exists())
        self.assertEqual((self.nas/'copy').read_bytes(),b'verified')
        self.assertIn('remount,rw,nosuid,nodev,noexec',self.commands[0]);self.assertIn('remount,ro,nosuid,nodev,noexec',self.commands[-1])

    def test_mismatch_missing_symlink_hardlink_and_traversal_never_remount(self):
        for target in (self.src/'clip',self.nas/'copy'):
            target.write_bytes(b'changed!')
            with self.assertRaises(RuntimeError):self.delete()
            self.assertEqual(self.commands,[]);target.write_bytes(b'verified')
        with self.assertRaises(ValueError):self.delete(dict(self.record,path='../clip'))
        (self.src/'clip').rename(self.src/'original');(self.src/'clip').symlink_to(self.src/'original')
        with self.assertRaises(OSError):self.delete()
        (self.src/'clip').unlink();os.link(self.src/'original',self.src/'clip')
        with self.assertRaises(ValueError):self.delete()
        self.assertEqual(self.commands,[]);self.assertEqual((self.src/'original').read_bytes(),b'verified')

    def test_no_confirmation_never_remounts(self):
        with self.assertRaises(ValueError):self.delete(confirm=lambda:{'delete':False})
        self.assertEqual(self.commands,[]);self.assertTrue((self.src/'clip').exists())

    def test_rw_failure_restores_readonly_without_deleting(self):
        def command(args):
            self.commands.append(args)
            if 'remount,rw,nosuid,nodev,noexec' in args:raise RuntimeError('filesystem refuses writable remount')
        with self.assertRaises(RuntimeError):self.delete(command=command)
        self.assertIn('remount,ro,nosuid,nodev,noexec',self.commands[-1]);self.assertTrue((self.src/'clip').exists())

    def test_restore_failure_unmounts_after_handles_closed(self):
        def command(args):
            self.commands.append(args)
            if 'remount,ro,nosuid,nodev,noexec' in args:raise RuntimeError('RO failure')
            if args[0]=='/usr/bin/umount':
                for fd in Path('/proc/self/fd').iterdir():
                    try:self.assertFalse(os.readlink(fd).startswith(str(self.src)))
                    except FileNotFoundError:pass
        with self.assertRaises(RuntimeError):self.delete(command=command)
        self.assertEqual(self.commands[-1][0],'/usr/bin/umount')

    def test_backup_replaced_during_confirmation_preserves_original(self):
        def confirm():
            (self.nas/'copy').unlink();(self.nas/'copy').write_bytes(b'replaced');return {'delete':True}
        with self.assertRaises(RuntimeError):self.delete(confirm=confirm)
        self.assertTrue((self.src/'clip').exists());self.assertEqual(self.commands,[])

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

from camera import Cameras, CameraItemMissing
from core import Transfers, Cancelled, Hardware
import drive_helper as helper
from tests.test_cameras import DEVICE, FakeCamera
from tests.test_transfer import FakeHardware


class DeletableCamera(FakeCamera):
    def delete_verified(self,path,record,verify_backup,check,progress):
        check()
        if path not in self.files: raise CameraItemMissing(path)
        data=self.files[path]
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
        self.camera.files['first.THM']=b'unselected thumbnail'
        job=self.copy(paths=['first.MP4']);result=self.delete(job)
        self.assertEqual(result['cleanup']['state'],'complete',result)
        self.assertNotIn('first.MP4',self.camera.files);self.assertIn('untouched.RAW',self.camera.files)
        self.assertEqual(self.camera.files['first.THM'],b'unselected thumbnail')
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

    def test_missing_camera_source_checks_backup_and_continues_without_claiming_deletion(self):
        job=self.copy();del self.camera.files['first.MP4']
        result=self.delete(job);c=result['cleanup']
        self.assertEqual(c['state'],'complete_with_missing',result)
        self.assertEqual((c['files'],c['verified_files'],c['missing_files']),(1,1,1))
        self.assertEqual(c['bytes'],6);self.assertEqual(c['missing_bytes'],5)
        self.assertEqual(c['phase_bytes'],17);self.assertEqual(c['total_bytes'],17)
        self.assertEqual(c['missing_paths'],['first.MP4']);self.assertEqual(self.camera.files,{})
        audit=(self.hw.dst/job['destination']/'deleted.jsonl').read_text().splitlines()
        self.assertEqual([json.loads(line)['path'] for line in audit],['second.MP4'])
        self.engine=Transfers(self.hw,self.root/'history.json')
        self.assertEqual(self.engine.snapshot()[0]['cleanup'],c)
        self.assertTrue(result['can_delete'])

    def test_absent_camera_file_with_missing_or_changed_backup_stops_before_next_delete(self):
        job=self.copy();del self.camera.files['first.MP4']
        backup=self.hw.dst/job['output_folders'][0]/'first.MP4'
        for changed in (True,False):
            if changed:backup.write_bytes(b'corrupt')
            else:backup.unlink()
            result=self.delete(job);c=result['cleanup']
            self.assertEqual(c['state'],'failed',result)
            self.assertEqual((c['files'],c['verified_files'],c['missing_files']),(0,0,0))
            self.assertNotIn('original was kept',c['error'])
            self.assertIn('second.MP4',self.camera.files)

    def test_camera_errors_and_generic_missing_exceptions_are_not_treated_as_absence(self):
        job=self.copy()
        for error in (RuntimeError('PTP General Error'),FileNotFoundError('Disconnected USB'),CameraItemMissing('wrong-path')):
            with patch.object(self.camera,'delete_verified',side_effect=error):result=self.delete(job)
            self.assertEqual(result['cleanup']['state'],'failed',result)
            self.assertEqual(result['cleanup']['missing_files'],0)
            self.assertEqual(len(self.camera.files),2)

    def test_all_absent_can_be_rechecked_and_reappearing_original_must_match(self):
        job=self.copy();self.camera.files.clear()
        result=self.delete(job);c=result['cleanup']
        self.assertEqual(c['state'],'complete_with_missing')
        self.assertEqual((c['files'],c['verified_files'],c['missing_files']),(0,0,2))
        self.assertEqual((c['phase_bytes'],c['total_bytes']),(11,11))
        self.assertEqual((self.hw.dst/job['destination']/'deleted.jsonl').read_bytes(),b'')
        self.camera.files['first.MP4']=b'wrong'
        result=self.delete(job);self.assertEqual(result['cleanup']['state'],'failed')
        self.assertEqual(self.camera.files['first.MP4'],b'wrong')
        self.camera.files.update({'first.MP4':b'first','second.MP4':b'second'})
        result=self.delete(job);self.assertEqual(result['cleanup']['state'],'complete')
        self.assertEqual(result['cleanup']['files'],2)

    def test_camera_file_disappearing_during_cleanup_is_reported_separately(self):
        self.camera.files={'clip.MP4':b'video','other.MP4':b'other'}
        job=self.copy();original=self.camera.delete_verified
        def remove_other(path,*args):
            original(path,*args)
            if path=='clip.MP4':self.camera.files.pop('other.MP4')
        with patch.object(self.camera,'delete_verified',side_effect=remove_other):result=self.delete(job)
        self.assertEqual(result['cleanup']['state'],'complete_with_missing',result)
        self.assertEqual(result['cleanup']['files'],1)
        self.assertEqual(result['cleanup']['missing_paths'],['other.MP4'])
        token=self.engine.prepare_delete(job['id']);self.assertEqual(token['files'],1)

    def test_camera_sidecars_are_verified_and_deleted_before_videos_can_invalidate_them(self):
        # Model a camera whose file listing breaks after deleting a video with
        # remaining companions. This is a regression simulation, not a claim
        # that every GoPro firmware behaves this way.
        self.camera.folders={'','DCIM','DCIM/100GOPRO','DCIM/101GOPRO'}
        originals={
            'DCIM/100GOPRO/GX010001.MP4':b'video one',
            'DCIM/100GOPRO/GX010001.THM':b'thumbnail one',
            'DCIM/100GOPRO/GL010001.LRV':b'proxy one',
            'DCIM/101GOPRO/GX010002.mp4':b'video two',
            'DCIM/101GOPRO/GX010002.tHm':b'thumbnail two',
            'DCIM/101GOPRO/GL010002.lrv':b'proxy two',
        }
        self.camera.files=dict(originals)
        job=self.copy(layout='original');original=self.camera.delete_verified
        order=[];broken=[False]
        def delete_with_stale_metadata(path,*args):
            if broken[0]:raise RuntimeError('PTP General Error (0x2002): GetObjectInfo')
            original(path,*args);order.append(path)
            if path.lower().endswith('.mp4'):
                broken[0]=any(p.lower().endswith(('.thm','.lrv')) for p in self.camera.files)
        with patch.object(self.camera,'delete_verified',side_effect=delete_with_stale_metadata):result=self.delete(job)
        self.assertEqual(result['cleanup']['state'],'complete',result)
        self.assertFalse(broken[0]);self.assertEqual(self.camera.files,{})
        self.assertTrue(all(p.lower().endswith(('.thm','.lrv')) for p in order[:4]))
        c=result['cleanup'];total=sum(map(len,originals.values()))
        self.assertEqual((c['files'],c['verified_files'],c['missing_files']),(6,6,0))
        self.assertEqual((c['bytes'],c['phase_bytes'],c['total_bytes']),(total,2*total,2*total))
        workspace=self.hw.dst/job['destination']
        audit=[json.loads(line) for line in (workspace/'deleted.jsonl').read_text().splitlines()]
        self.assertEqual([r['path'] for r in audit],order)
        for record in audit:
            data=originals[record['path']]
            self.assertEqual((record['size'],record['sha256']),(len(data),hashlib.sha256(data).hexdigest()))
            self.assertEqual((workspace/'data'/record['path']).read_bytes(),data)

    def test_sidecar_verification_failure_keeps_video_and_thumbnail(self):
        self.camera.files={'clip.MP4':b'video','clip.THM':b'thumbnail'}
        job=self.copy(layout='original');backup=self.hw.dst/job['destination']/'data/clip.THM'
        for target in ('source','nas','missing_nas','ptp'):
            with self.subTest(target=target):
                self.camera.files={'clip.MP4':b'video','clip.THM':b'thumbnail'}
                backup.write_bytes(b'thumbnail')
                if target=='source':self.camera.files['clip.THM']=b'changed!'
                elif target=='nas':backup.write_bytes(b'corrupt!!')
                elif target=='missing_nas':backup.unlink()
                original=self.camera.delete_verified
                def fail_thumbnail(path,*args):
                    if target=='ptp' and path.endswith('.THM'):
                        raise RuntimeError('PTP General Error')
                    return original(path,*args)
                with patch.object(self.camera,'delete_verified',side_effect=fail_thumbnail):result=self.delete(job)
                c=result['cleanup']
                self.assertEqual(c['state'],'failed',result)
                self.assertEqual((c['files'],c['verified_files'],c['missing_files']),(0,0,0))
                self.assertEqual(set(self.camera.files),{'clip.MP4','clip.THM'})
                self.assertEqual((self.hw.dst/job['destination']/'deleted.jsonl').read_bytes(),b'')

    def test_sidecar_retry_handles_prior_receipts_and_absence_without_redeleting(self):
        self.camera.files={'clip.MP4':b'video','clip.THM':b'thumbnail','old.THM':b'old thumbnail'}
        job=self.copy();del self.camera.files['old.THM'];original=self.camera.delete_verified
        def cancel_after_delete(*args):original(*args);self.engine.cancel_event.set()
        with patch.object(self.camera,'delete_verified',side_effect=cancel_after_delete):result=self.delete(job)
        self.assertEqual(result['cleanup']['state'],'cancelled',result)
        self.assertEqual(self.camera.files,{'clip.MP4':b'video'})
        self.engine=Transfers(self.hw,self.root/'history.json')
        result=self.delete(self.engine.snapshot()[0]);c=result['cleanup']
        self.assertEqual(c['state'],'complete_with_missing',result)
        self.assertEqual((c['files'],c['verified_files'],c['missing_files'],c['previous_deleted_files']),(1,1,1,1))
        self.assertEqual(c['missing_paths'],['old.THM'])
        self.assertEqual(self.camera.files,{})
        audit=(self.hw.dst/job['destination']/'deleted.jsonl').read_text().splitlines()
        self.assertEqual([json.loads(line)['path'] for line in audit],['clip.THM','clip.MP4'])

    def test_backup_symlink_fails_closed(self):
        job=self.copy()
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

    def test_confirmed_absence_never_starts_delete_worker(self):
        source=self.backend.source(DEVICE)
        with patch.object(self.backend,'request',return_value={'kind':'missing'}), patch('camera.subprocess.Popen') as spawn:
            with self.assertRaises(CameraItemMissing) as caught:
                source.delete_verified('clip.MP4',self.record,lambda:None,lambda:None,lambda n:None)
            self.assertEqual(caught.exception.path,'clip.MP4');spawn.assert_not_called()


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

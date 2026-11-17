"""Recording dates, flat publication, collisions and interrupted move recovery."""
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from core import Transfers
from organize import date_and_camera, rename_exclusive
from tests.test_transfer import FakeHardware


class OrganizeTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.hw=FakeHardware(self.temp.name)
        self.hw.file_metadata=lambda fd: {'QuickTime:CreateDate':'2026:11:16 02:30:00+00:00','Model':'GoPro HERO9 Black'}
        self.engine=Transfers(self.hw,self.root/'history.json')
        for name,payload in [('store/DCIM/A/clip.MP4',b'first'),('store/DCIM/B/clip.MP4',b'second')]:
            file=self.hw.src/name;file.parent.mkdir(parents=True,exist_ok=True);file.write_bytes(payload)

    def tearDown(self): self.temp.cleanup()

    def wait(self):
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            if self.engine.operation.acquire(False):
                self.engine.operation.release();return self.engine.snapshot()[0]
            time.sleep(.005)
        self.fail('Operation did not finish')

    def start(self,**kwargs):
        self.engine.start('usb',[''],layout='camera_date',import_timezone='America/Chicago',**kwargs)
        result=self.wait();self.assertEqual(result['state'],'complete',result);return result

    def plan(self,job): return json.loads((self.hw.dst/job['destination']/'publish.json').read_text())['files']

    def test_flat_date_folders_collision_and_checksum_mapping(self):
        job=self.start()
        self.assertEqual(job['output_folders'],['Pi-Imports/GoPro_HERO9_Black_2026-11-15'])
        entries=self.plan(job);self.assertEqual(len({e['output'] for e in entries}),2)
        for e in entries:
            self.assertEqual((self.hw.dst/'Pi-Imports'/e['output']).read_bytes(),(self.hw.src/e['source']).read_bytes())
            self.assertEqual(len(e['output'].split('/')),2)
        second=self.start()
        self.assertEqual(len(list((self.hw.dst/job['output_folders'][0]).iterdir())),4)
        self.assertTrue(job['destination'].startswith('Pi-Imports/.pitransfer-'))
        self.assertFalse(list((self.hw.dst/job['destination']/'data').rglob('*.MP4')))
        self.assertFalse(any(e['name'].startswith('.') for e in self.engine.browse_nas('Pi-Imports')['items']))

    def test_different_dates_and_unknown_timestamp_separate(self):
        def metadata(fd):
            payload=os.read(fd,10)
            return {'CreationDate':'2026:11:17 12:30:00','Model':'GoPro HERO9 Black'} if payload==b'first' else {'CreateDate':'2026:11:18 12:30:00','Model':'GoPro HERO9 Black'}
        self.hw.file_metadata=metadata
        job=self.start()
        self.assertEqual(job['output_folders'],['Pi-Imports/GoPro_HERO9_Black_2026-11-17','Pi-Imports/GoPro_HERO9_Black_2026-11-18'])
        self.assertEqual(date_and_camera({},None,'GoPro HERO9 Black')[0],'Unknown-date')
        self.assertEqual(date_and_camera({'CreateDate':'0000:00:00'},1700000000,'Camera')[2],'file timestamp')

    def test_timezone_and_naive_camera_clock(self):
        self.assertEqual(date_and_camera({'CreationDate':'2026:11:17 01:00:00Z'},None,'A','America/Chicago')[0],'2026-11-16')
        self.assertEqual(date_and_camera({'DateTimeOriginal':'2026:11:17 01:00:00'},None,'A','America/Chicago')[0],'2026-11-17')
        with self.assertRaises(ValueError): self.engine.start('usb',[''],layout='camera_date',import_timezone='../bad')

    def test_completed_legacy_import_organizes_without_source(self):
        self.engine.start('usb',['']);job=self.wait();old=self.hw.dst/job['destination']
        self.assertTrue(job['can_organize'])
        with patch.object(self.hw,'source',side_effect=AssertionError('Opened original')):
            self.engine.organize(job['id'],'America/Chicago');result=self.wait()
        self.assertEqual(result['state'],'complete',result);self.assertFalse(old.exists())
        self.assertTrue(result['publication_complete']);self.assertFalse(result['can_organize'])
        self.assertEqual(len(self.plan(result)),2)

    def test_interrupt_after_first_move_resumes_without_source_or_duplicates(self):
        original=rename_exclusive;moved=[]
        def interrupt(srcfd,src,dstfd,dst):
            original(srcfd,src,dstfd,dst)
            if str(src).endswith('.MP4'):
                moved.append(dst);self.engine.cancel_event.set()
        with patch('organize.rename_exclusive',side_effect=interrupt):
            self.engine.start('usb',[''],layout='camera_date');job=self.wait()
        self.assertEqual(job['state'],'cancelled',job);self.assertEqual(len(moved),1)
        self.assertTrue(job['copy_complete']);self.assertTrue(job['can_resume'])
        self.engine=Transfers(self.hw,self.root/'history.json')
        with patch.object(self.hw,'source',side_effect=AssertionError('Opened original')):
            self.engine.resume(job['id']);result=self.wait()
        self.assertEqual(result['state'],'complete',result)
        self.assertEqual(len(list((self.hw.dst/result['output_folders'][0]).iterdir())),2)

    def test_corrupt_published_file_blocks_resume(self):
        original=rename_exclusive
        def interrupt(srcfd,src,dstfd,dst):
            original(srcfd,src,dstfd,dst)
            if str(src).endswith('.MP4'): self.engine.cancel_event.set()
        with patch('organize.rename_exclusive',side_effect=interrupt):
            self.engine.start('usb',[''],layout='camera_date');job=self.wait()
        entry=next(p for p in (self.hw.dst/job['output_folders'][0]).iterdir());entry.write_bytes(b'changed')
        self.engine.resume(job['id']);result=self.wait()
        self.assertEqual(result['state'],'failed');self.assertIn('Checksum changed',result['error'])
        self.assertEqual(entry.read_bytes(),b'changed')

    def test_missing_legacy_media_fails_without_losing_manifest(self):
        self.engine.start('usb',['']);job=self.wait();folder=self.hw.dst/job['destination']
        (folder/'data/store/DCIM/A/clip.MP4').unlink()
        self.engine.organize(job['id']);result=self.wait()
        self.assertEqual(result['state'],'failed');self.assertTrue((folder/'checksums.jsonl').exists())
        self.assertTrue((folder/'data/store/DCIM/B/clip.MP4').exists())

    def test_atomic_move_refuses_existing_file_and_symlink_folder(self):
        fd=os.open(self.hw.dst,os.O_RDONLY|os.O_DIRECTORY)
        try:
            (self.hw.dst/'a').write_bytes(b'a');(self.hw.dst/'b').write_bytes(b'b')
            with self.assertRaises(FileExistsError): rename_exclusive(fd,'a',fd,'b')
            self.assertEqual((self.hw.dst/'b').read_bytes(),b'b')
        finally: os.close(fd)
        target=self.hw.dst/'Pi-Imports';target.mkdir()
        (target/'GoPro_HERO9_Black_2026-11-16').symlink_to(self.hw.src,target_is_directory=True)
        self.engine.start('usb',[''],layout='camera_date');result=self.wait()
        self.assertEqual(result['state'],'failed');self.assertTrue((self.hw.src/'store/DCIM/A/clip.MP4').exists())

    def test_publication_plan_cannot_escape_or_change_receipt(self):
        job=self.start();planfile=self.hw.dst/job['destination']/'publish.json';plan=json.loads(planfile.read_text())
        plan['files'][0]['output']='../../outside';planfile.write_text(json.dumps(plan))
        with self.assertRaises(ValueError): self.engine.prepare_delete(job['id'])


EXIFTOOL=shutil.which('exiftool') or os.environ.get('PITRANSFER_TEST_EXIFTOOL')
@unittest.skipUnless(EXIFTOOL and shutil.which('ffmpeg'),'ExifTool and ffmpeg required for actual header test')
class RealMetadataTest(unittest.TestCase):
    def test_actual_mp4_recording_date_from_fd(self):
        from metadata_worker import extract
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'clip.mp4'
            subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=black:s=16x16:d=0.04',
                            '-metadata','creation_time=2026-11-17T01:30:00Z','-c:v','mpeg4',str(path)],check=True,capture_output=True)
            with path.open('rb') as source: metadata=extract(source.fileno(),EXIFTOOL)
            self.assertTrue(metadata,metadata)
            self.assertEqual(date_and_camera(metadata,None,'GoPro','America/Chicago')[0],'2026-11-16',metadata)

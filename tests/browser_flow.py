"""Browser regression against an isolated temporary NAS and simulated camera.

Run explicitly with Playwright/Chromium installed. Never connects to real hardware.
"""
from pathlib import Path
import contextlib
import errno
import os
import sys
import tempfile
import threading
import time
from unittest.mock import patch
from wsgiref.simple_server import make_server, WSGIRequestHandler

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import App
from tests.test_cameras import DEVICE, FakeCamera
from tests.test_cleanup import DeletableCamera
from tests.test_transfer import FakeHardware
from tests.test_web import config
from playwright.sync_api import sync_playwright, expect


class QuietHandler(WSGIRequestHandler):
    def log_message(self, *args):
        pass


def main():
    with tempfile.TemporaryDirectory() as temp:
        hardware = FakeHardware(temp)
        camera = DeletableCamera()
        cameras = {DEVICE:camera}
        hardware.camera_source = lambda device: cameras[device]
        available = [{"id": DEVICE, "label": "GoPro HERO9 Black", "kind": "camera",
                                     "history_key": 'camera:'+'a'*64,
                                     "size": None, "node": "usb:001,003", "filesystem": "PTP/MTP",
                                     "usable": True, "mounted": False}]
        hardware.devices = lambda exclude=None: [d for d in available if d['node'] not in (exclude or [])]
        for name in ('Video', 'Photos', 'Mounted Drive'):
            (hardware.dst / name).mkdir()
        (hardware.dst / 'do-not-display.txt').write_text('private NAS contents')
        application = App(config(temp), hardware)
        with make_server('127.0.0.1', 0, application, handler_class=QuietHandler) as server:
            host = f'127.0.0.1:{server.server_port}'
            application.config.update(allowed_hosts=[host], allowed_origins=['http://' + host])
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    page = browser.new_page(viewport={"width":1280, "height":960})
                    errors = []
                    page.on('pageerror', lambda exc: errors.append(str(exc)))
                    page.goto('http://' + host)
                    page.get_by_label('App password').fill('test-only-password')
                    page.get_by_role('button', name='Sign in', exact=True).click()
                    expect(page.locator('#nas-title')).to_have_text('NAS ready')

                    page.get_by_role('button', name='Choose NAS folder').click()
                    expect(page.locator('#nas-folders')).to_contain_text('Mounted Drive')
                    expect(page.locator('#nas-folders')).not_to_contain_text('do-not-display.txt')
                    page.locator('#nas-folders .file').filter(has_text='Video').get_by_role('button', name='Browse').click()
                    expect(page.locator('#nas-current')).to_contain_text('/Video')
                    page.get_by_role('button', name='Use this folder').click()
                    expect(page.locator('#nas-detail')).to_contain_text('/Video/Pi-Imports')

                    page.get_by_role('button', name='Select camera', exact=True).click()
                    page.get_by_label('DCIM', exact=True).check()
                    page.get_by_role('button', name='Check size', exact=True).click()
                    expect(page.locator('#transfer-size')).to_contain_text('Selected items: 2 file(s)', timeout=15000)
                    expect(page.locator('#transfer-size')).to_contain_text('MiB')
                    assert camera.opened == [], 'Sizing downloaded file contents'
                    assert not (hardware.dst/'Video/Pi-Imports').exists()

                    dialogs = []
                    scan_entered,finish_scan=threading.Event(),threading.Event()
                    scan_threads=[];scan_errors=[];original_devices=hardware.devices
                    def slow_discovery(exclude=None):
                        # Gate only the deliberate concurrent scan; the copy
                        # flow's own status refresh must reach its confirmation.
                        if threading.current_thread() not in scan_threads:
                            return original_devices(exclude)
                        scan_entered.set()
                        if not finish_scan.wait(30):raise RuntimeError('Browser discovery gate timed out')
                        return original_devices(exclude)
                    def background_discovery():
                        try:application.transfers.devices()
                        except Exception as exc:scan_errors.append(str(exc))
                    def confirm(dialog):
                        dialogs.append(dialog.message)
                        assert 'MiB' in dialog.message and '/Video/Pi-Imports' in dialog.message
                        # Reproduce a status refresh racing the user's OK click.
                        application.transfers.device_cache_time=0
                        worker=threading.Thread(target=background_discovery)
                        scan_threads.append(worker);worker.start()
                        assert scan_entered.wait(5)
                        dialog.accept()
                    with patch.object(hardware,'devices',side_effect=slow_discovery):
                        try:
                            page.once('dialog', confirm)
                            page.locator('#copy-selected').click()
                            expect(page.locator('#notice')).to_contain_text('waiting for the device refresh',timeout=15000)
                            expect(page.locator('#jobs .job-top .pill').first).to_have_text('queued')
                            expect(page.locator('#jobs .progress-heading').first).to_contain_text('Waiting for device refresh')
                            expect(page.locator('#jobs .copy-progress').first).to_have_attribute('value','0')
                            expect(page.get_by_role('button',name='Remove from queue')).to_be_enabled()
                            assert camera.opened==[], 'Copy overlapped device discovery'
                            assert len(application.transfers.snapshot())==1
                        finally:
                            finish_scan.set()
                            for worker in scan_threads:
                                worker.join(5)
                                assert not worker.is_alive()
                    assert scan_errors==[],scan_errors
                    expect(page.locator('#jobs .job-top .pill').first).to_have_text('complete', timeout=15000)
                    assert len(dialogs) == 1
                    job = application.transfers.snapshot()[0]
                    assert job['state'] == 'complete', job
                    assert (hardware.dst/job['output_folders'][0]/'photo.RAW').read_bytes() == b'raw'
                    expect(page.locator('#jobs')).to_contain_text('Estimated total: 2 file(s)')
                    expect(page.locator('#current-jobs .job')).to_have_count(0)
                    expect(page.locator('#completed-jobs .job')).to_have_count(1)
                    expect(page.locator('#completed-jobs .job')).not_to_be_visible()
                    expect(page.locator('#completed-summary')).to_have_text('Completed transfers (1)')
                    page.locator('#completed-summary').click()
                    expect(page.locator('#completed-jobs .job')).to_be_visible()
                    page.evaluate('renderJobs(jobsState)')
                    expect(page.locator('#completed-transfers')).to_have_js_property('open',True)

                    camera.unknown_size = True
                    page.get_by_label('note.txt', exact=True).check()
                    page.locator('#check-size').click()
                    expect(page.locator('#transfer-size')).to_contain_text('1 file(s) with unknown size', timeout=15000)
                    expect(page.locator('#transfer-size')).to_contain_text('At least')

                    def slow_count(paths, check, progress):
                        progress({'files':0,'bytes':0,'unknown_files':0,'skipped':0})
                        for _ in range(1000):
                            check()
                            time.sleep(.01)
                    camera.estimate = slow_count
                    page.locator('#check-size').click()
                    expect(page.locator('#cancel-size')).to_be_enabled()
                    page.locator('#cancel-size').click()
                    expect(page.locator('#size-detail')).to_contain_text('cancelled', timeout=15000)
                    expect(page.locator('#check-size')).to_be_enabled()

                    # Reproduce the older install's final SMB rename failure.
                    from organize import rename_exclusive
                    rename = rename_exclusive
                    def deny_rename(src_fd, src, dst_fd, dst):
                        if str(src).startswith('.incomplete-'):
                            raise PermissionError(errno.EACCES, 'Permission denied', src, None, dst)
                        return rename(src_fd, src, dst_fd, dst)
                    with patch('organize.rename_exclusive', side_effect=deny_rename):
                        application.transfers.start(DEVICE, ['note.txt'])
                        deadline = time.monotonic() + 5
                        while time.monotonic() < deadline:
                            if application.transfers.operation.acquire(False):
                                application.transfers.operation.release()
                                break
                            time.sleep(.01)
                    failed = application.transfers.jobs[-1]
                    assert failed['state'] == 'failed', failed
                    _, incomplete, final = application.transfers.destination_parts(failed)
                    for key in ('copy_complete', 'paths', 'resume_version', 'pending_path'):
                        failed.pop(key, None)
                    failed['error'] = str(PermissionError(errno.EACCES, 'Permission denied', incomplete, None, final))
                    application.transfers.save()
                    downloads = list(camera.opened)
                    page.get_by_role('button', name='Refresh', exact=True).click()
                    expect(page.locator('#jobs .job-top .pill').first).to_have_text('failed')
                    expect(page.get_by_role('button', name='Resume transfer', exact=True)).to_be_enabled()
                    page.once('dialog', lambda dialog: dialog.accept())
                    page.get_by_role('button', name='Resume transfer', exact=True).click()
                    expect(page.locator('#notice')).to_contain_text('Resume started on the Pi')
                    expect(page.locator('#jobs .job-top .pill').first).to_have_text('complete', timeout=15000)
                    expect(page.locator('#current-jobs .job')).to_have_count(0)
                    assert camera.opened == downloads, 'Finalization downloaded the camera files again'
                    expect(page.locator('#jobs .job').first).to_contain_text('1 saved files rechecked')

                    legacy_card=page.locator(f'[data-job-id="{failed["id"]}"]')
                    expect(legacy_card.get_by_role('button',name='Organize by date')).to_be_enabled()
                    page.once('dialog',lambda dialog:dialog.accept())
                    legacy_card.get_by_role('button',name='Organize by date').click()
                    expect(legacy_card).to_contain_text('GoPro_HERO9_Black_',timeout=15000)
                    expect(legacy_card.get_by_role('button',name='Organize by date')).to_have_count(0)
                    assert failed['publication_complete'],failed
                    assert camera.opened==downloads,'Organizing read original media again'
                    expect(page.locator('#jobs .device-group')).to_have_count(1)
                    expect(page.locator('#jobs .device-group-heading h3')).to_have_text('GoPro HERO9 Black')
                    expect(page.locator('#jobs .device-group .job')).to_have_count(2)
                    expect(page.locator('#jobs .device-group-heading p')).to_have_text('2 transfers')
                    expect(legacy_card.locator('.job-top strong')).to_contain_text('Transfer ·')
                    expect(legacy_card.locator('.selection-summary')).to_have_count(0)  # Old saved selection is unknown.
                    # Same physical camera after a reconnect shares a group;
                    # same model with a different identity does not.
                    grouped=page.evaluate('''() => groupTransfers([
                        {id:'new',device:'camera:new',device_history_key:'serial-a',device_label:'GoPro HERO9 Black'},
                        {id:'other',device:'camera:other',device_history_key:'serial-b',device_label:'GoPro HERO9 Black'},
                        {id:'old',device:'camera:old',device_history_key:'serial-a',device_label:'GoPro HERO9 Black'},
                        {id:'legacy',device:'camera:old',device_label:'USB camera'}
                    ], []).map(g=>({label:g.label,ids:g.jobs.map(j=>j.id)}))''')
                    assert grouped==[{'label':'GoPro HERO9 Black','ids':['new','old','legacy']},
                                     {'label':'GoPro HERO9 Black','ids':['other']}]
                    assert page.evaluate("groupTransfers([{id:'a',device:'camera:1',device_label:'Same model'},{id:'b',device:'camera:2',device_label:'Same model'}],[]).length")==2

                    # Only unqualified success moves out of the main list. In
                    # particular, a completed copy cannot hide deletion problems.
                    classified=page.evaluate('''() => [
                        {state:'complete'}, {state:'complete',cleanup:{state:'complete'}},
                        {state:'complete_with_skips'}, {state:'complete',skipped:1},
                        {state:'complete',error:'Organization failed'},
                        ...['queued','starting','counting','copying','verifying','resuming','finalizing','organizing','deleting','failed','cancelled','interrupted'].map(state=>({state})),
                        ...['checking','verifying','deleting','failed','cancelled','interrupted','complete_with_missing'].map(state=>({state:'complete',cleanup:{state}}))
                    ].map(completedSuccessfully)''')
                    assert classified==[True,True]+[False]*22, classified

                    # A progress bar exists for old resume jobs and for every camera.
                    assert abs(page.evaluate("jobProgress({state:'resuming',copy_complete:true,bytes:100,reused_bytes:28}).percent") - 28) < 1e-9
                    assert abs(page.evaluate("jobProgress({state:'verifying',total_bytes:100,bytes:100,checked_bytes:50}).percent") - 75) < 1e-9
                    assert abs(page.evaluate("jobProgress({state:'copying',total_files:4,files:1,unknown_files:1,bytes:10}).percent") - 25) < 1e-9
                    second = 'camera:002:004:aaaaaaaaaaaaaaaa'
                    cameras[second] = FakeCamera()
                    available.append(dict(available[0], id=second, label='Second USB camera', node='usb:002,004',history_key='camera:'+'b'*64))
                    gate, entered = threading.Event(), threading.Event()
                    second_gate, second_entered = threading.Event(), threading.Event()
                    original_stream = camera.stream
                    second_stream = cameras[second].stream
                    @contextlib.contextmanager
                    def gated_stream(path, expected, check):
                        entered.set()
                        if not gate.wait(30):
                            raise RuntimeError('Browser test gate timed out')
                        with original_stream(path, expected, check) as chunks:
                            yield chunks
                    camera.stream = gated_stream
                    @contextlib.contextmanager
                    def gated_second_stream(path, expected, check):
                        second_entered.set()
                        if not second_gate.wait(30):
                            raise RuntimeError('Second camera browser gate timed out')
                        with second_stream(path, expected, check) as chunks:
                            yield chunks
                    cameras[second].stream=gated_second_stream
                    try:
                        active = application.transfers.start(DEVICE, ['note.txt'])
                        assert entered.wait(5)
                        application.transfers.device_cache_time = 0
                        page.get_by_role('button', name='Refresh', exact=True).click()
                        second_card = page.locator('#drives .drive').filter(has_text='Second USB camera')
                        expect(second_card.get_by_role('button', name='Queue all files')).to_be_enabled()
                        page.once('dialog', lambda dialog: dialog.accept())
                        second_card.get_by_role('button', name='Queue all files').click()
                        expect(page.locator('#notice')).to_contain_text('added to the transfer queue')
                        waiting = next(j for j in application.transfers.snapshot() if j['device']==second)
                        queued_card = page.locator(f'[data-job-id="{waiting["id"]}"]')
                        running_card = page.locator(f'[data-job-id="{active["id"]}"]')
                        expect(queued_card).to_contain_text('queue position 1')
                        expect(queued_card.locator('.copy-progress')).to_have_attribute('value','0')
                        expect(running_card.locator('.copy-progress')).to_have_count(1)
                        expect(queued_card.get_by_role('button',name='Remove from queue')).to_be_enabled()
                        expect(page.locator('#current-jobs .job')).to_have_count(2)
                        expect(page.locator('#current-jobs .copy-progress')).to_have_count(2)
                        expect(page.locator('#completed-jobs .delete-originals').first).to_be_disabled()
                        page.locator('#completed-summary').click()
                        expect(page.locator('#completed-transfers')).to_have_js_property('open',False)
                        gate.set()
                        expect(page.locator(f'#current-jobs [data-job-id="{active["id"]}"]')).to_have_count(0,timeout=15000)
                        expect(page.locator('#current-jobs .job')).to_have_count(1)
                        expect(queued_card).to_be_visible()
                        assert second_entered.is_set()
                        expect(running_card).not_to_be_visible()
                        expect(page.locator('#completed-transfers')).to_have_js_property('open',False)
                        expect(page.locator('#jobs .job')).to_have_count(len(application.transfers.snapshot()))
                        second_gate.set()
                        expect(queued_card.locator('.pill')).to_have_text('complete',timeout=15000)
                        expect(running_card.locator('.pill')).to_have_text('complete',timeout=15000)
                        expect(queued_card.locator('.copy-progress')).to_have_attribute('value','100')
                        expect(page.locator('#jobs .copy-progress')).to_have_count(len(application.transfers.snapshot()))
                        expect(page.locator('#jobs .device-group')).to_have_count(2)
                        same_camera=page.locator('#jobs .device-group').filter(has=page.locator('.device-group-heading h3',has_text='GoPro HERO9 Black'))
                        expect(same_camera.locator('.job')).to_have_count(3)
                        expect(same_camera.locator('.copy-progress')).to_have_count(3)
                        expect(page.locator('#current-jobs .job')).to_have_count(0)
                        expect(queued_card).not_to_be_visible()
                        page.locator('#completed-summary').click()
                    finally:
                        gate.set();second_gate.set()
                        camera.stream = original_stream
                        cameras[second].stream=second_stream

                    # Deletion has its own confirmation and preserves the NAS outputs.
                    copied_card=page.locator(f'[data-job-id="{job["id"]}"]')
                    delete=copied_card.get_by_role('button',name='Delete verified originals')
                    expect(delete).to_be_enabled()
                    expect(copied_card.locator('.verification-progress')).to_have_attribute('value','0')
                    expect(copied_card.locator('.deletion-progress')).to_have_attribute('value','0')
                    expect(copied_card.locator('.cleanup-status')).to_contain_text('Not started')
                    deletion_requests=[]
                    page.on('request',lambda request:deletion_requests.append(request) if request.url.endswith('/api/delete/confirm') else None)
                    def cancel_delete(dialog):
                        assert dialog.type=='confirm', 'Deletion should use OK/Cancel, not a text prompt'
                        assert 'delete 2 original files' in dialog.message
                        assert 'GoPro HERO9 Black' in dialog.message and 'MiB' in dialog.message
                        dialog.dismiss()
                    page.once('dialog',cancel_delete)
                    delete.click()
                    expect(delete).to_be_enabled()
                    assert 'DCIM/100GOPRO/photo.RAW' in camera.files
                    expect(copied_card.locator('.cleanup-status')).to_contain_text('Confirmation was cancelled')
                    assert deletion_requests==[], 'Cancel sent a deletion request'
                    def confirm_delete(dialog):
                        assert dialog.type=='confirm'
                        assert 'delete 2 original files' in dialog.message
                        assert 'Click OK to verify and delete' in dialog.message
                        assert 'NAS copies are kept' in dialog.message
                        dialog.accept()
                    # Hold verification and deletion separately to inspect live bars.
                    started,checked=threading.Event(),threading.Event()
                    finish_check,finish_delete=threading.Event(),threading.Event()
                    original_delete=camera.delete_verified
                    def gated_delete(path,record,verify_backup,check,progress):
                        data=camera.files[path]
                        if path.endswith('clip.MP4'):
                            half=len(data)//2;progress(half);started.set()
                            if not finish_check.wait(30):raise RuntimeError('Verification gate timed out')
                            progress(len(data)-half);verify_backup();checked.set()
                            if not finish_delete.wait(30):raise RuntimeError('Deletion gate timed out')
                            check();del camera.files[path]
                        else: original_delete(path,record,verify_backup,check,progress)
                    camera.delete_verified=gated_delete
                    try:
                        page.once('dialog',confirm_delete)
                        delete.click()
                        # Keep Playwright's event loop running to handle the async
                        # confirmation dialog before waiting on the worker gate.
                        expect(copied_card.locator('.cleanup-status')).to_contain_text('Verifying originals',timeout=15000)
                        expect(page.locator(f'#current-jobs [data-job-id="{job["id"]}"]')).to_be_visible()
                        expect(page.locator(f'#completed-jobs [data-job-id="{job["id"]}"]')).to_have_count(0)
                        assert started.is_set()
                        expect(copied_card.locator('.copy-progress')).to_have_attribute('value','100')
                        expect(copied_card.locator('.deletion-progress')).to_have_attribute('value','0')
                        expect(copied_card.locator('.verification-progress')).to_have_js_property('value',len(camera.files['DCIM/100GOPRO/clip.MP4'])//2/(2*(len(camera.files['DCIM/100GOPRO/clip.MP4'])+3))*100)
                        expect(copied_card.get_by_role('button',name='Stop deleting')).to_be_enabled()
                        assert len(camera.files)==3
                        finish_check.set();assert checked.wait(5)
                        expect(copied_card.locator('.cleanup-status')).to_contain_text('Deleting the verified file')
                        expect(copied_card.locator('.cleanup-section')).to_contain_text('1 of 2 files verified')
                        expect(copied_card.locator('.deletion-progress')).to_have_attribute('value','0')
                        finish_delete.set()
                        expect(copied_card).to_contain_text('Original deletion: complete',timeout=15000)
                        expect(page.locator(f'#current-jobs [data-job-id="{job["id"]}"]')).to_have_count(0)
                    finally:
                        finish_check.set();finish_delete.set();camera.delete_verified=original_delete
                    assert 'DCIM/100GOPRO/photo.RAW' not in camera.files
                    assert 'DCIM/100GOPRO/clip.MP4' not in camera.files
                    assert 'note.txt' in camera.files
                    assert len(deletion_requests)==1, 'OK should start exactly one deletion operation'
                    assert (hardware.dst/job['output_folders'][0]/'photo.RAW').read_bytes()==b'raw'
                    expect(delete).to_have_count(0)
                    expect(copied_card.locator('.verification-progress')).to_have_attribute('value','100')
                    expect(copied_card.locator('.deletion-progress')).to_have_attribute('value','100')
                    expect(copied_card.locator('.cleanup-status')).to_contain_text('Originals deleted')
                    expect(page.locator('#files')).not_to_contain_text('DCIM')

                    # Successful jobs stay hidden after reload, but records/actions
                    # are available by expanding history. Failed verification
                    # stays below 100 even when the final checksum consumed every byte.
                    page.reload()
                    expect(page.locator('#completed-summary')).to_have_text('Completed transfers (4)')
                    expect(page.locator('#current-jobs .job')).to_have_count(0)
                    expect(copied_card).not_to_be_visible()
                    expect(page.locator('#completed-transfers')).to_have_js_property('open',False)
                    page.locator('#completed-summary').click()
                    expect(copied_card).to_be_visible()
                    expect(copied_card.locator('.verification-progress')).to_have_attribute('value','100')
                    expect(copied_card.locator('.deletion-progress')).to_have_attribute('value','100')
                    assert page.evaluate("cleanupProgress({state:'failed',total_files:1,files:0,verified_files:0,total_bytes:10,phase_bytes:10}).verification.percent")==99.9
                    assert page.evaluate("cleanupProgress({state:'deleting',total_files:1,files:0,verified_files:1,total_bytes:0,phase_bytes:0}).verification.percent")==100
                    assert page.evaluate("cleanupProgress({state:'interrupted',total_files:2,files:1,total_bytes:20,phase_bytes:10}).deletion.percent")==50
                    assert page.evaluate("cleanupProgress({state:'complete',total_files:1,files:1}).verification.percent")==100

                    # A deletion failure must be visible separately from the completed copy.
                    camera.files['note.txt']=b'changed original'
                    def confirm_one_delete(dialog):
                        assert dialog.type=='confirm'
                        assert 'delete 1 original file (' in dialog.message
                        dialog.accept()
                    page.once('dialog',confirm_one_delete)
                    legacy_card.get_by_role('button',name='Delete verified originals').click()
                    expect(legacy_card.locator('.cleanup-status')).to_contain_text('Deletion stopped with an error')
                    expect(legacy_card.locator('.cleanup-error')).to_contain_text('Original checksum changed')
                    expect(page.locator(f'#current-jobs [data-job-id="{failed["id"]}"]')).to_be_visible()
                    expect(legacy_card.locator('.deletion-progress')).to_have_attribute('value','0')
                    expect(legacy_card.locator('.copy-progress')).to_have_attribute('value','100')
                    assert camera.files['note.txt']==b'changed original'

                    # Missing originals are separate from acknowledged deletions.
                    # Reuse the real transfer record and keep its NAS checksum valid.
                    del camera.files['note.txt']
                    page.once('dialog',confirm_one_delete)
                    legacy_card.get_by_role('button',name='Delete verified originals').click()
                    expect(legacy_card.locator('.cleanup-status')).to_contain_text('0 originals deleted; 1 already absent')
                    expect(legacy_card.locator('.verification-progress')).to_have_attribute('value','100')
                    expect(legacy_card.locator('.deletion-progress')).to_have_attribute('value','0')
                    expect(legacy_card.locator('.cleanup-missing')).to_contain_text('not counted as deleted')
                    legacy_card.locator('.cleanup-missing summary').click()
                    expect(legacy_card.locator('.cleanup-missing .path')).to_have_text('note.txt')
                    page.evaluate('renderJobs(jobsState)')
                    expect(legacy_card.locator('.cleanup-missing')).to_have_attribute('open','')
                    expect(legacy_card.get_by_role('button',name='Recheck remaining originals')).to_be_enabled()
                    page.reload()
                    expect(legacy_card.locator('.cleanup-status')).to_contain_text('1 already absent')
                    expect(legacy_card).to_be_visible()
                    expect(page.locator('#completed-transfers')).to_have_js_property('open',False)
                    page.once('dialog',lambda dialog:dialog.dismiss())
                    legacy_card.get_by_role('button',name='Recheck remaining originals').click()
                    expect(legacy_card.locator('.cleanup-status')).to_contain_text('Confirmation was cancelled')
                    mixed=page.evaluate("cleanupProgress({state:'complete_with_missing',total_files:3,files:2,missing_files:1,verified_files:2,phase_bytes:25,total_bytes:25})")
                    assert mixed['deletion']['percent']==100
                    assert '2 of 2 available originals deleted' in mixed['deletion']['detail']
                    assert '1 already absent' in mixed['deletion']['detail']

                    page.set_viewport_size({"width":390,"height":844})
                    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), 'Mobile layout overflows'
                    assert not errors, errors
                    browser.close()
                    print('Browser checks passed: folders, sizing, cancellation, transfer/resume, per-camera bars, second-camera queue, automatic success hiding, collapsed history/reload, completion percentages, date organization, confirmed original deletion, visible cleanup failures/warnings, and mobile width.')
            finally:
                server.shutdown()


if __name__ == '__main__':
    main()

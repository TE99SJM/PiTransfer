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
                    def confirm(dialog):
                        dialogs.append(dialog.message)
                        assert 'MiB' in dialog.message and '/Video/Pi-Imports' in dialog.message
                        dialog.accept()
                    page.once('dialog', confirm)
                    page.locator('#copy-selected').click()
                    expect(page.locator('#notice')).to_contain_text('You can close this window', timeout=15000)
                    expect(page.locator('#jobs .job-top .pill').first).to_have_text('complete', timeout=15000)
                    assert len(dialogs) == 1
                    job = application.transfers.snapshot()[0]
                    assert job['state'] == 'complete', job
                    assert (hardware.dst/job['output_folders'][0]/'photo.RAW').read_bytes() == b'raw'
                    expect(page.locator('#jobs')).to_contain_text('Estimated total: 2 file(s)')

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

                    # A progress bar exists for old resume jobs and for every camera.
                    assert abs(page.evaluate("jobProgress({state:'resuming',copy_complete:true,bytes:100,reused_bytes:28}).percent") - 28) < 1e-9
                    assert abs(page.evaluate("jobProgress({state:'verifying',total_bytes:100,bytes:100,checked_bytes:50}).percent") - 75) < 1e-9
                    assert abs(page.evaluate("jobProgress({state:'copying',total_files:4,files:1,unknown_files:1,bytes:10}).percent") - 25) < 1e-9
                    second = 'camera:002:004:aaaaaaaaaaaaaaaa'
                    cameras[second] = FakeCamera()
                    available.append(dict(available[0], id=second, label='Second USB camera', node='usb:002,004'))
                    gate, entered = threading.Event(), threading.Event()
                    original_stream = camera.stream
                    @contextlib.contextmanager
                    def gated_stream(path, expected, check):
                        entered.set()
                        if not gate.wait(30):
                            raise RuntimeError('Browser test gate timed out')
                        with original_stream(path, expected, check) as chunks:
                            yield chunks
                    camera.stream = gated_stream
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
                        gate.set()
                        expect(queued_card.locator('.pill')).to_have_text('complete',timeout=15000)
                        expect(running_card.locator('.pill')).to_have_text('complete',timeout=15000)
                        expect(queued_card.locator('.copy-progress')).to_have_attribute('value','100')
                        expect(page.locator('#jobs .copy-progress')).to_have_count(len(application.transfers.snapshot()))
                    finally:
                        gate.set()
                        camera.stream = original_stream

                    # Deletion has its own confirmation and preserves the NAS outputs.
                    copied_card=page.locator(f'[data-job-id="{job["id"]}"]')
                    delete=copied_card.get_by_role('button',name='Delete verified originals')
                    expect(delete).to_be_enabled()
                    expect(copied_card.locator('.verification-progress')).to_have_attribute('value','0')
                    expect(copied_card.locator('.deletion-progress')).to_have_attribute('value','0')
                    expect(copied_card.locator('.cleanup-status')).to_contain_text('Not started')
                    page.once('dialog',lambda dialog:dialog.dismiss())
                    delete.click()
                    expect(delete).to_be_enabled()
                    assert 'DCIM/100GOPRO/photo.RAW' in camera.files
                    expect(copied_card.locator('.cleanup-status')).to_contain_text('Confirmation was cancelled')
                    page.once('dialog',lambda dialog:dialog.accept('wrong'))
                    delete.click()
                    expect(page.locator('#notice')).to_contain_text('Nothing was deleted')
                    expect(delete).to_be_enabled()
                    def confirm_delete(dialog):
                        assert '2 verified originals' in dialog.message
                        assert 'NAS copies are kept' in dialog.message
                        dialog.accept('DELETE 2')
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
                    finally:
                        finish_check.set();finish_delete.set();camera.delete_verified=original_delete
                    assert 'DCIM/100GOPRO/photo.RAW' not in camera.files
                    assert 'DCIM/100GOPRO/clip.MP4' not in camera.files
                    assert 'note.txt' in camera.files
                    assert (hardware.dst/job['output_folders'][0]/'photo.RAW').read_bytes()==b'raw'
                    expect(delete).to_have_count(0)
                    expect(copied_card.locator('.verification-progress')).to_have_attribute('value','100')
                    expect(copied_card.locator('.deletion-progress')).to_have_attribute('value','100')
                    expect(copied_card.locator('.cleanup-status')).to_contain_text('Originals deleted')
                    expect(page.locator('#files')).not_to_contain_text('DCIM')

                    # Completion bars persist after a page reload; failed verification
                    # stays below 100 even when the final checksum consumed every byte.
                    page.reload()
                    expect(copied_card.locator('.verification-progress')).to_have_attribute('value','100')
                    expect(copied_card.locator('.deletion-progress')).to_have_attribute('value','100')
                    assert page.evaluate("cleanupProgress({state:'failed',total_files:1,files:0,verified_files:0,total_bytes:10,phase_bytes:10}).verification.percent")==99.9
                    assert page.evaluate("cleanupProgress({state:'deleting',total_files:1,files:0,verified_files:1,total_bytes:0,phase_bytes:0}).verification.percent")==100
                    assert page.evaluate("cleanupProgress({state:'interrupted',total_files:2,files:1,total_bytes:20,phase_bytes:10}).deletion.percent")==50
                    assert page.evaluate("cleanupProgress({state:'complete',total_files:1,files:1}).verification.percent")==100

                    # A deletion failure must be visible separately from the completed copy.
                    camera.files['note.txt']=b'changed original'
                    page.once('dialog',lambda dialog:dialog.accept('DELETE 1'))
                    legacy_card.get_by_role('button',name='Delete verified originals').click()
                    expect(legacy_card.locator('.cleanup-status')).to_contain_text('Deletion stopped with an error')
                    expect(legacy_card.locator('.cleanup-error')).to_contain_text('Original checksum changed')
                    expect(legacy_card.locator('.deletion-progress')).to_have_attribute('value','0')
                    expect(legacy_card.locator('.copy-progress')).to_have_attribute('value','100')
                    assert camera.files['note.txt']==b'changed original'

                    page.set_viewport_size({"width":390,"height":844})
                    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), 'Mobile layout overflows'
                    assert not errors, errors
                    browser.close()
                    print('Browser checks passed: folders, sizing, cancellation, transfer/resume, per-camera bars, second-camera queue, completion percentages, date organization, confirmed original deletion, and mobile width.')
            finally:
                server.shutdown()


if __name__ == '__main__':
    main()

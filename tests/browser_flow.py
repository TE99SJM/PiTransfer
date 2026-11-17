"""Browser regression against an isolated temporary NAS and simulated camera.

Run explicitly with Playwright/Chromium installed. Never connects to real hardware.
"""
from pathlib import Path
import sys
import tempfile
import threading
import time
from wsgiref.simple_server import make_server, WSGIRequestHandler

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import App
from tests.test_cameras import DEVICE, FakeCamera
from tests.test_transfer import FakeHardware
from tests.test_web import config
from playwright.sync_api import sync_playwright, expect


class QuietHandler(WSGIRequestHandler):
    def log_message(self, *args):
        pass


def main():
    with tempfile.TemporaryDirectory() as temp:
        hardware = FakeHardware(temp)
        camera = FakeCamera()
        hardware.camera_source = lambda device: camera
        hardware.devices = lambda: [{"id": DEVICE, "label": "GoPro HERO9 Black", "kind": "camera",
                                     "size": None, "node": "usb:001,003", "filesystem": "PTP/MTP",
                                     "usable": True, "mounted": False}]
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
                    assert (hardware.dst/job['destination']/'data/DCIM/100GOPRO/photo.RAW').read_bytes() == b'raw'
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

                    page.set_viewport_size({"width":390,"height":844})
                    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), 'Mobile layout overflows'
                    assert not errors, errors
                    browser.close()
                    print('Browser checks passed: NAS folders, size totals, unknown sizes, cancel, copy confirmation, destination, and mobile width.')
            finally:
                server.shutdown()


if __name__ == '__main__':
    main()

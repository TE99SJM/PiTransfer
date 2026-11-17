import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from app import App
from tests.test_transfer import FakeHardware


def config(root):
    salt = bytes.fromhex('11' * 32)
    return {"password_salt": salt.hex(), "password_hash": hashlib.pbkdf2_hmac("sha256", b'test-only-password', salt, 600000).hex(), "allowed_hosts": ["127.0.0.1:8090"], "allowed_origins": ["http://127.0.0.1:8090"], "secure_cookie": False, "history_path": str(Path(root) / "history.json")}


class WebTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = App(config(self.temp.name), FakeHardware(self.temp.name))
        self.cookie, self.csrf = '', ''

    def tearDown(self):
        self.temp.cleanup()

    def request(self, path, method="GET", data=None, **overrides):
        body = json.dumps(data or {}).encode()
        env = {"HTTP_HOST":"127.0.0.1:8090", "HTTP_ORIGIN":"http://127.0.0.1:8090", "HTTP_COOKIE":self.cookie, "HTTP_X_CSRF_TOKEN":self.csrf, "REQUEST_METHOD":method, "PATH_INFO":path, "CONTENT_TYPE":"application/json", "CONTENT_LENGTH":str(len(body)), "wsgi.input":io.BytesIO(body)}
        env.update(overrides)
        out = {}
        def start(status, headers):
            out.update(status=int(status.split()[0]), headers=dict(headers))
        payload = b''.join(self.app(env, start))
        out['body'] = json.loads(payload) if out['headers']['Content-Type'].startswith('application/json') else payload
        return out

    def login(self):
        r = self.request('/api/login', 'POST', {'password':'test-only-password'})
        self.assertEqual(r['status'], 200)
        self.cookie = r['headers']['Set-Cookie'].split(';')[0]
        self.csrf = r['body']['csrf']
        return r

    def test_login_required_for_metadata(self):
        self.assertEqual(self.request('/api/status')['status'], 401)
        self.assertEqual(self.request('/api/browse', 'POST', {'device':'usb'})['status'], 401)

    def test_login_cookie_csrf_and_metadata(self):
        r = self.login()
        self.assertIn('HttpOnly', r['headers']['Set-Cookie'])
        self.assertIn('SameSite=Strict', r['headers']['Set-Cookie'])
        self.assertEqual(self.request('/api/status')['status'], 200)
        self.assertEqual(self.request('/api/browse','POST',{'device':'usb'}, HTTP_X_CSRF_TOKEN='wrong')['status'],403)
        self.assertEqual(self.request('/api/browse','POST',{'device':'usb'})['status'],200)

    def test_no_file_serving_endpoints(self):
        secret = b'FILE_BYTES_MUST_NEVER_BE_SENT'
        (self.app.hardware.src/'secret.txt').write_bytes(secret)
        self.login()
        for path in ('/secret.txt','/files/secret.txt','/download/secret.txt','/preview/secret.txt','/../core.py','/api/file','/api/download'):
            r = self.request(path)
            self.assertEqual(r['status'],404)
            self.assertNotIn(secret.decode(), str(r))
        r = self.request('/api/browse','POST',{'device':'usb'})
        self.assertIn('secret.txt', str(r))
        self.assertNotIn(secret.decode(), str(r))

    def test_host_origin_and_json_required(self):
        self.assertEqual(self.request('/', HTTP_HOST='attacker.example')['status'],403)
        self.assertEqual(self.request('/api/login','POST',{'password':'test-only-password'}, HTTP_ORIGIN='https://attacker.example')['status'],403)
        self.assertEqual(self.request('/api/login','POST', CONTENT_TYPE='text/plain')['status'],400)

    def test_camera_metadata_has_no_media_endpoint(self):
        from tests.test_cameras import DEVICE, FakeCamera
        camera = FakeCamera()
        self.app.hardware.camera_source = lambda device: camera
        self.login()
        r = self.request('/api/browse', 'POST', {'device': DEVICE, 'path': 'DCIM/100GOPRO'})
        self.assertEqual(r['status'], 200)
        self.assertIn('clip.MP4', str(r['body']))
        self.assertEqual(camera.opened, [])
        for endpoint in ('/api/camera/download', '/api/camera/preview', '/api/camera/capture', '/api/camera/delete'):
            self.assertEqual(self.request(endpoint, 'POST', {'device': DEVICE})['status'], 404)
        self.assertEqual(camera.opened, [])

    def test_rate_limit_and_wrong_password(self):
        # Use a stub digest to keep repeated brute-force rejection testing quick.
        from unittest.mock import patch
        with patch('app.hashlib.pbkdf2_hmac', return_value=b'wrong'):
            for i in range(10):
                self.assertEqual(self.request('/api/login','POST',{'password':'wrong'})['status'],401)
            self.assertEqual(self.request('/api/login','POST',{'password':'wrong'})['status'],429)

    def test_logout_invalidates_session(self):
        self.login()
        self.assertEqual(self.request('/api/logout','POST')['status'],200)
        self.assertEqual(self.request('/api/status')['status'],401)

    def test_security_headers_and_static_allowlist(self):
        for path in ('/','/app.js','/style.css'):
            r = self.request(path)
            self.assertEqual(r['status'],200)
            self.assertEqual(r['headers']['Cache-Control'],'no-store')
            self.assertEqual(r['headers']['X-Content-Type-Options'],'nosniff')
            self.assertIn("frame-ancestors 'none'",r['headers']['Content-Security-Policy'])

    def test_secure_cookie_mode(self):
        self.app.config['secure_cookie'] = True
        self.assertIn('; Secure', self.login()['headers']['Set-Cookie'])


if __name__ == '__main__':
    unittest.main()

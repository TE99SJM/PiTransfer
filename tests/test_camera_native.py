"""Exercise the real bindings using libgphoto2's filesystem test driver.

This does not claim USB hardware coverage. Production discovery rejects disk:.
"""
import hashlib
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from camera_worker import download, entries, file_info, estimate, detect_ports, delete_verified

try:
    import gphoto2 as gp
except ImportError:
    gp = None


@unittest.skipIf(gp is None, "Install python3-gphoto2 to run native binding tests")
class NativeCameraTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.camera = gp.Camera()
        abilities = gp.CameraAbilitiesList()
        abilities.load()
        self.camera.set_abilities(abilities[abilities.lookup_model("Directory Browse")])
        ports = gp.PortInfoList()
        ports.load()
        self.camera.set_port_info(ports[ports.lookup_path("disk:" + self.temp.name)])
        self.camera.init()

    def tearDown(self):
        self.camera.exit()
        self.temp.cleanup()

    def test_original_bytes_stream_through_real_fd_backed_camerafile(self):
        payload = bytes(range(256)) * 40000
        (self.root / "clip.MP4").write_bytes(payload)
        before = file_info(gp, self.camera, "clip.MP4")
        self.assertEqual(before["size"], len(payload))
        read_fd, write_fd = os.pipe()
        digest = hashlib.sha256()
        count = [0]
        def receive():
            while True:
                chunk = os.read(read_fd, 65536)
                if not chunk:
                    break
                digest.update(chunk)
                count[0] += len(chunk)
        reader = threading.Thread(target=receive, daemon=True)
        reader.start()
        try:
            with os.fdopen(write_fd, "wb") as pipe:
                with patch("camera_worker.sys.stdout", pipe):
                    download(gp, self.camera, "clip.MP4", before)
            reader.join(timeout=5)
            self.assertFalse(reader.is_alive())
        finally:
            os.close(read_fd)
        self.assertEqual(count[0], len(payload))
        self.assertEqual(digest.digest(), hashlib.sha256(payload).digest())
        self.assertEqual((self.root / "clip.MP4").read_bytes(), payload)

    def test_metadata_lists_names_and_empty_folders(self):
        (self.root / "DCIM").mkdir()
        (self.root / "photo.JPG").write_bytes(b"do-not-preview")
        result = entries(self.camera, "")
        self.assertEqual([item["name"] for item in result], ["DCIM", "photo.JPG"])
        self.assertNotIn("do-not-preview", str(result))

    def test_changed_size_fails_before_requesting_bytes(self):
        (self.root / "photo.RAW").write_bytes(b"original")
        expected = file_info(gp, self.camera, "photo.RAW")
        expected["size"] += 1
        with self.assertRaisesRegex(RuntimeError, "changed before"):
            download(gp, self.camera, "photo.RAW", expected)

    def test_recursive_file_sizes_use_metadata_only(self):
        (self.root / 'DCIM').mkdir()
        (self.root / 'DCIM/photo.JPG').write_bytes(b'12345')
        with (self.root / 'DCIM/big.MP4').open('wb') as f:
            f.truncate(5 * 1024**3 + 3)
        with patch('camera_worker.download', side_effect=AssertionError('Downloaded media')):
            result = estimate(gp, self.camera, ['DCIM', 'DCIM/photo.JPG'], lambda _: None)
        self.assertEqual(result['files'], 2)
        self.assertEqual(result['bytes'], 5 * 1024**3 + 8)

    def test_filtered_native_port_lists_have_one_owner(self):
        import gc
        ports = gp.PortInfoList()
        ports.load()
        borrowed = gp.PortInfoList()
        borrowed.thisown = False
        expected = []
        for i in range(ports.count()):
            expected.append(ports[i].get_path())
            borrowed.append(ports[i])
        self.assertEqual([borrowed[i].get_path() for i in range(borrowed.count())], expected)
        del borrowed
        gc.collect()
        self.assertEqual([ports[i].get_path() for i in range(ports.count())], expected)
        self.assertEqual(detect_ports(gp, only='usb:999,999'), [])

    def test_native_delete_requires_handshake_and_stable_object(self):
        payload=b'original video bytes'
        target=self.root/'clip.MP4';target.write_bytes(payload)
        expected=file_info(gp,self.camera,'clip.MP4')
        with tempfile.TemporaryFile(mode='w+b') as output:
            import io
            text=io.TextIOWrapper(output,encoding='utf-8',write_through=True)
            try:
                with patch('camera_worker.sys.stdout',text):
                    with self.assertRaises(ValueError):
                        delete_verified(gp,self.camera,'clip.MP4',expected,lambda:{'delete':False})
                self.assertEqual(target.read_bytes(),payload)
                output.seek(0);output.truncate()
                def approved():
                    text.flush();output.seek(0);received=output.read()
                    self.assertTrue(received.startswith(payload),received)
                    self.assertIn(b'"ready": true',received)
                    output.seek(0,2)
                    return {'delete':True}
                with patch('camera_worker.sys.stdout',text):
                    delete_verified(gp,self.camera,'clip.MP4',expected,approved)
                self.assertFalse(target.exists())
            finally:
                text.detach()

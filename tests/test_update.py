"""Exercise deployment/rollback in temporary directories with service/apt stubs."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class UpdateTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.opt = self.base / "opt"
        self.opt.mkdir()
        self.current = self.opt / "pi-nas-drop"
        self.current.mkdir()
        (self.current / "app.py").write_text("old application")
        self.configdir = self.base / "etc/pi-nas-drop"
        self.configdir.mkdir(parents=True)
        self.rules = self.base / "etc/udev/rules.d"
        self.rules.mkdir(parents=True)
        self.history = self.base / "history.json"
        self.history.write_text("[]")
        self.config = self.configdir / "config.json"
        self.config.write_text(json.dumps({"history_path": str(self.history), "allowed_hosts": ["private.example"], "password_hash": "test-hash"}))
        self.credentials = self.configdir / "nas-credentials"
        self.credentials.write_text("test-only-private-credentials")
        self.original_config = self.config.read_bytes()
        self.checkout = self.base / "checkout"
        self.checkout.mkdir()
        for name in ("app.py", "core.py", "drive_helper.py", "camera.py", "camera_worker.py"):
            shutil.copyfile(ROOT / name, self.checkout / name)
        shutil.copytree(ROOT / "web", self.checkout / "web")
        shutil.copytree(ROOT / "udev", self.checkout / "udev")
        script = (ROOT / "update.sh").read_text().replace("/opt/pi-nas-drop", str(self.opt / "pi-nas-drop"))
        script = script.replace("/etc/pi-nas-drop", str(self.configdir)).replace("/etc/udev/rules.d", str(self.rules))
        (self.checkout / "update.sh").write_text(script)
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.log = self.base / "commands.log"
        self.stub("id", "#!/bin/sh\necho 0\n")
        self.stub("apt-get", '#!/bin/sh\necho "apt $*" >> "$UPDATE_LOG"\n')
        self.stub("udevadm", "#!/bin/sh\nexit 0\n")
        self.stub("systemctl", '#!/bin/sh\necho "systemctl $*" >> "$UPDATE_LOG"\nif [ "$1" = start ] && [ "${FAIL_START:-0}" = 1 ]; then exit 1; fi\n')
        self.stub("python3", f'#!{sys.executable}\nimport os,sys\nif sys.argv[1:]==["-c","import gphoto2"]: sys.exit(0)\nos.execv({sys.executable!r},[{sys.executable!r}]+sys.argv[1:])\n')
        # Run the real installer tool without privileged ownership changes.
        self.stub("install", f'#!{sys.executable}\nimport os,sys\nargs=[]\ni=1\nwhile i<len(sys.argv):\n if sys.argv[i] in ("-o","-g"): i+=2\n else: args.append(sys.argv[i]); i+=1\nos.execv({shutil.which("install")!r},["install"]+args)\n')
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ["PATH"], UPDATE_LOG=str(self.log))

    def stub(self, name, source):
        path = self.bin / name
        path.write_text(source)
        path.chmod(0o755)

    def tearDown(self):
        self.temp.cleanup()

    def run_update(self):
        return subprocess.run(["/bin/bash", "update.sh"], cwd=self.checkout, env=self.env,
                              capture_output=True, text=True, timeout=20)

    def assert_settings_retained(self):
        self.assertEqual(self.config.read_bytes(), self.original_config)
        self.assertEqual(self.credentials.read_text(), "test-only-private-credentials")

    def test_update_retains_settings_history_and_previous_code(self):
        result = self.run_update()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.current / "app.py").read_bytes(), (ROOT / "app.py").read_bytes())
        self.assertTrue((self.current / "camera_worker.py").exists())
        backups = list(self.opt.glob("*.previous"))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / "app.py").read_text(), "old application")
        self.assertEqual(self.history.read_text(), "[]")
        self.assert_settings_retained()

    def test_active_transfer_blocks_service_stop(self):
        for state in ('copying', 'resuming', 'finalizing'):
            with self.subTest(state=state):
                self.history.write_text(json.dumps([{'state':state}]))
                result = self.run_update()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("transfer is active", result.stderr)
                self.assertFalse(self.log.exists())
                self.assertEqual((self.current / "app.py").read_text(), "old application")
                self.assert_settings_retained()

    def test_failed_service_start_restores_previous_code(self):
        self.env["FAIL_START"] = "1"
        result = self.run_update()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("previous application restored", result.stderr)
        self.assertEqual((self.current / "app.py").read_text(), "old application")
        self.assert_settings_retained()

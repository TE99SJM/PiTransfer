#!/usr/bin/env python3
"""Interactive local configuration. Credentials never pass through the web UI."""
import getpass
import grp
import hashlib
import json
import os
import pwd
import re
import secrets
from pathlib import Path


def main():
    if os.geteuid() != 0:
        raise SystemExit("Run the installer with sudo")
    root = Path("/etc/pi-nas-drop")
    if (root / "config.json").exists():
        raise SystemExit("Configuration already exists; refusing to replace it")
    share = input("NAS share (for example //nas.local/incoming): ").strip()
    if not re.fullmatch(r"//[a-zA-Z0-9.-]+/[a-zA-Z0-9_.-]+", share):
        raise SystemExit("Use //SERVER/SHARE with letters, digits, dot, dash or underscore")
    username = input("NAS username: ").strip()
    nas_password = getpass.getpass("NAS password (hidden): ")
    domain = input("NAS domain [leave blank if unused]: ").strip()
    if not username or not nas_password or any('\n' in x or '\r' in x or '\x00' in x for x in (username, nas_password, domain)):
        raise SystemExit("Invalid credentials")
    password = getpass.getpass("Choose a NEW PiTransfer app password (12+ characters): ")
    if len(password) < 12 or len(password) > 1024 or password != getpass.getpass("Repeat app password: "):
        raise SystemExit("App passwords must match and be 12–1024 characters")
    uid, gid = pwd.getpwnam("pidrop").pw_uid, grp.getgrnam("pidrop").gr_gid
    salt = secrets.token_bytes(32)
    config = {"nas_mount": "/mnt/pidropnas", "nas_share": share, "history_path": "/var/lib/pi-nas-drop/history.json", "allowed_hosts": ["127.0.0.1:8090", "localhost:8090"], "allowed_origins": ["http://127.0.0.1:8090", "http://localhost:8090"], "secure_cookie": False, "password_salt": salt.hex(), "password_hash": hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600000).hex()}
    credential = "username=" + username + "\npassword=" + nas_password + "\n"
    if domain:
        credential += "domain=" + domain + "\n"
    os.umask(0o077)
    (root / "nas.credentials").write_text(credential)
    (root / "config.json").write_text(json.dumps(config, indent=2) + "\n")
    os.chown(root / "config.json", 0, gid)
    os.chmod(root / "config.json", 0o640)
    unit = f"""[Unit]
Description=PiTransfer encrypted NAS destination
Wants=network-online.target
After=network-online.target

[Mount]
What={share}
Where=/mnt/pidropnas
Type=cifs
Options=credentials=/etc/pi-nas-drop/nas.credentials,vers=3,seal,cache=none,uid={uid},gid={gid},forceuid,forcegid,file_mode=0600,dir_mode=0700,nosuid,nodev,noexec,_netdev
TimeoutSec=30

[Install]
WantedBy=multi-user.target
"""
    mount = Path("/etc/systemd/system/mnt-pidropnas.mount")
    mount.write_text(unit)
    os.chmod(mount, 0o644)


if __name__ == "__main__":
    main()

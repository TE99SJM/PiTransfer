# PiTransfer

A small, headless Raspberry Pi web app for copying files from USB storage to an encrypted SMB3 NAS share. No desktop or Docker required. Designed for a Pi with a 32 GB system card: file data streams in 1 MiB chunks directly to the NAS, with no local staging directory.

[Download source ZIP](https://github.com/TE99SJM/PiTransfer/archive/refs/heads/main.zip) · [Repository](https://github.com/TE99SJM/PiTransfer)

## What the interface does

- Detects USB disks, flash drives and USB SD-card readers, refreshing every five seconds.
- Displays filenames, folder names and sizes. Select files/folders or copy an entire partition.
- Shows copied bytes, verified file counts, current filename and recent transfers.
- Allows cancellation and unmounting after transfers.
- Does **not** preview, play, download, upload, edit, execute or delete files.

The backend must read file bytes to copy and verify them; it never sends those bytes to the browser. Directory browsing returns metadata only. This is a transfer station, not an antivirus scanner or an air gap.

## Transfer behavior

Sources mount read-only with `nosuid,nodev,noexec`. ext3/ext4 also use `noload` to prevent journal replay. The app refuses disks already mounted outside its managed area, including a USB boot disk. Drive selection includes a filesystem/serial fingerprint to reject a swapped device when its identity changes.

Each copy gets its own folder:

```
NAS share / Pi-Imports / transfer-UTCtimestamp-randomID /
  data/               copied files and folders
  checksums.jsonl     SHA-256 and verification result for each file
```

The job uses an `.incomplete-...` directory until all regular files pass SHA-256 read-back verification. Symlinks and special files are skipped and recorded; the UI distinguishes completion with skips. Regular files are created exclusively, so existing files cannot be silently overwritten. Original files are never deleted. File modification times and empty directories are retained; ownership, permissions, ACLs, extended attributes, hard-link relationships and sparse allocation are not preserved.

NAS mounts require SMB3, `seal` encryption and `cache=none` for uncached file-data read-back. The configured share must match the mounted share. Transfers fail closed if the NAS is unmounted; the underlying local mount directory is root-owned and not writable by the app. The dedicated service account reads source files using ordinary permissions, so inaccessible files fail the transfer instead of bypassing permissions.

One transfer runs at a time. Keep one Gunicorn worker; its threads share the transfer lock. Cancellation waits for outstanding kernel/network I/O. Failed or interrupted jobs retain their partial NAS directory and never report success. There is no automatic resume in v1: retry creates a new directory. Remove incomplete folders from the NAS separately if you no longer need them. Unmount all partitions of a disk before physically unplugging it.

## Supported systems

Target: Raspberry Pi OS Lite / Debian with systemd and Python 3.9 or newer, 32-bit or 64-bit. Source filesystems: FAT32, exFAT, NTFS (ntfs-3g), ext2, ext3 and ext4. Encrypted disks, APFS, phones using MTP, and RAID/LVM are not supported. The Pi's system SD card is excluded. A dirty filesystem may require repair on its original computer; the app does not repair it.

Use a stable power supply; some external disks need their own power. The Pi, USB link, NAS and network determine speed. Hash verification adds a full read of the copied data from the NAS. Only the app, dependencies and small bounded job history use the Pi's system storage.

## Installation reference

We can walk through installation one step at a time. First identify the Pi with `cat /etc/os-release; uname -m; hostname`.

Once the OS is confirmed, download the source with `git clone https://github.com/TE99SJM/PiTransfer.git`, enter it with `cd PiTransfer`, and run `sudo bash install.sh`. You can also use GitHub’s **Code → Download ZIP**, extract it, and run the same installer from the extracted folder. The installer uses Debian packages, creates a non-login `pidrop` account, installs the app under `/opt/pi-nas-drop`, and asks locally for your NAS login and a separate app password. Enter your NAS share in `//SERVER/SHARE` form when prompted. No private NAS address is embedded in the app. No credentials are included in this package.

The NAS account needs read, create, write and rename permissions in the destination. Prefer a dedicated share/account limited to incoming transfers. The installer stores NAS credentials in a root-only file; the web process cannot read them. If the NAS does not support SMB3 encryption, mounting fails. Keep encryption required; diagnose NAS support rather than removing `seal`.

The app binds **only to 127.0.0.1:8090**. To reach it from your computer, forward that port with SSH:

```
ssh -N -L 8090:127.0.0.1:8090 USER@PI_ADDRESS
```

Then open `http://127.0.0.1:8090` on that computer. SSH encrypts the network hop; HTTP is confined to local loopback. The session cookie is HttpOnly and SameSite=Strict. Requests also require an allowed Host, allowed Origin and a session CSRF token. The password uses salted PBKDF2-SHA256; repeated login failures are rate-limited.

For convenient phone access, an administrator can configure private Tailscale Serve HTTPS. First add the exact `PI_NAME.TAILNET.ts.net` hostname to `allowed_hosts`, its `https://...` origin to `allowed_origins`, and set `secure_cookie` to `true` in `/etc/pi-nas-drop/config.json`; restart `pi-nas-drop`. Then configure `tailscale serve --bg http://127.0.0.1:8090` and restrict access with your tailnet access policy. Use Serve, not the public Funnel service. The default installer does not install Tailscale or expose a LAN/public listener. Secure cookies require HTTPS; after enabling that mode, use the HTTPS address rather than the HTTP SSH-tunnel login.

The service intentionally allows its small root-owned helper through a constrained sudo rule for USB mounting. It does not run the web app as root. Do not add `NoNewPrivileges=yes` or private mount namespaces without redesigning the helper integration: those can block sudo or disconnect source mounts from the app.

## Maintenance reference

- App status: `sudo systemctl status pi-nas-drop`
- NAS status: `sudo systemctl status mnt-pidropnas.mount`
- App log: `sudo journalctl -u pi-nas-drop -n 80 --no-pager`
- Retry NAS mount after correcting credentials/network: `sudo systemctl restart mnt-pidropnas.mount` (only when no transfer is running).
- Reboot starts both units. If the NAS was unavailable at boot, retry its mount after it becomes available.
- An existing installation is not overwritten by `install.sh`. Stop the app and install updated source files deliberately; retain `/etc/pi-nas-drop` and `/var/lib/pi-nas-drop`.
- To uninstall, stop/disable both units, unmount all app-managed USB partitions, and remove the app, its units and `/etc/sudoers.d/pi-nas-drop`. No source or NAS files need to be deleted.

## Validation and limits

Run `python3 -m unittest discover -s tests -v` in this folder. Tests exercise real temporary-file copies, checksum mismatch detection, cancellation, failure handling, path/symlink containment, source preservation, exclusive destinations, device filtering and HTTP authorization. They need no root access or NAS.

This package passed 29 automated tests in a Linux development environment, plus Python, shell and JavaScript syntax checks. Browser visual verification could not be completed because the available browser could not reach the local preview. Actual browser interaction, USB mounting, ARM installation, NAS encryption negotiation, unplug/disconnect behavior and transfer speed still require testing on your Pi and NAS. Unit tests do not substitute for those hardware checks. A readable malicious filesystem still reaches the Linux filesystem driver; keep the Pi OS updated.

## Technical references

- [Linux SMB3 mount options and encryption](https://www.kernel.org/doc/html/next/admin-guide/cifs/usage.html)
- [Linux ext4 read-only and noload behavior](https://docs.kernel.org/7.0/admin-guide/ext4.html)
- [Tailscale Serve](https://tailscale.com/docs/reference/tailscale-cli/serve)

Version: 0.1.0

# PiTransfer

A small, headless Raspberry Pi web app for copying files from USB drives and supported USB cameras to an encrypted SMB3 NAS share. No desktop or Docker required. Designed for a Pi with a 32 GB system card: the application copies file data in chunks of up to 1 MiB directly to the NAS, with no local staging directory.

[Download source ZIP](https://github.com/TE99SJM/PiTransfer/archive/refs/heads/main.zip) · [Repository](https://github.com/TE99SJM/PiTransfer)

## What the interface does

- Detects USB disks, flash drives, USB SD-card readers, and libgphoto2-supported MTP/PTP cameras, refreshing every five seconds while idle.
- Displays filenames and folders, with sizes for disk files. Camera browsing lists names without downloading media or thumbnails. Select files/folders or copy all accessible files.
- Shows copied bytes, verified file counts, current filename and recent transfers.
- Allows cancellation and unmounting after transfers.
- Does **not** preview, play, download, upload, edit, execute or delete files.

The backend must read file bytes to copy and verify them; it never sends those bytes to the browser. Directory browsing returns metadata only. This is a transfer station, not an antivirus scanner or an air gap.

## Transfer behavior

Disk sources mount read-only with `nosuid,nodev,noexec`. ext3/ext4 also use `noload` to prevent journal replay. The app refuses disks already mounted outside its managed area, including a USB boot disk. Drive selection includes a filesystem/serial fingerprint to reject a swapped device when its identity changes.

Camera sources use libgphoto2 read operations under the unprivileged service account. They are not mounted as filesystems. Each operation revalidates the USB address and device fingerprint, including its sysfs connection identity. The worker only lists metadata or downloads original file objects; it never requests previews, captures images, uploads, or deletes objects. Camera bytes pass through a pipe into the same exclusive NAS destination and SHA-256 read-back checks used for USB disks. Native driver allocations are capped at 384 MiB per worker; drivers that require buffering a larger complete object fail instead of staging it on the Pi. A camera must remain powered, idle, and connected for the copy. Device-reported metadata cannot detect every concurrent camera-side change.

Each copy gets its own folder:

```
NAS share / Pi-Imports / transfer-UTCtimestamp-randomID /
  data/               copied files and folders
  checksums.jsonl     SHA-256 and verification result for each file
```

The job uses an `.incomplete-...` directory until all regular files pass SHA-256 read-back verification. Symlinks and special files are skipped and recorded; the UI distinguishes completion with skips. Regular files are created exclusively, so existing files cannot be silently overwritten. Original files are never deleted. File modification times and empty directories are retained; ownership, permissions, ACLs, extended attributes, hard-link relationships and sparse allocation are not preserved.

NAS mounts require SMB3, `seal` encryption and `cache=none` for uncached file-data read-back. The configured share must match the mounted share. Transfers fail closed if the NAS is unmounted; the underlying local mount directory is root-owned and not writable by the app. The dedicated service account reads source files using ordinary permissions, so inaccessible files fail the transfer instead of bypassing permissions.

One transfer runs at a time. Keep one Gunicorn worker; its threads share the transfer lock. Discovery uses cached devices during browsing and copying so polling does not compete for the USB camera. Camera sessions close after each operation. When no operation is active, a camera can be unplugged without a filesystem unmount. Cancellation kills an active camera-download worker; outstanding metadata calls can take up to 45 seconds, and kernel/NAS I/O can take longer. A camera download with no data for 120 seconds fails. Failed or interrupted jobs retain their partial NAS directory and never report success. There is no automatic resume: retry creates a new directory. Remove incomplete folders from the NAS separately if you no longer need them. Unmount all partitions of a disk before physically unplugging it.

## Supported systems

Target: Raspberry Pi OS Lite / Debian with systemd and Python 3.9 or newer, 32-bit or 64-bit. Source filesystems: FAT32, exFAT, NTFS (ntfs-3g), ext2, ext3 and ext4. Encrypted disks, APFS, and RAID/LVM are not supported. The Pi's system SD card is excluded. A dirty filesystem may require repair on its original computer; the app does not repair it.

## Direct USB camera compatibility

The app supports the standard USB transfer paths across brands; it cannot promise every camera model or USB mode.

Camera streaming requires `python3-gphoto2` 2.5 or newer; Raspberry Pi OS / Debian Trixie provides it. Older distributions with earlier bindings can still use USB disks and readers but need an OS/package upgrade for this camera path.

| Camera connection | Support |
| --- | --- |
| USB mass storage | Existing disk path; supported filesystem required |
| PTP/MTP | Camera path through the installed libgphoto2 drivers |
| USB webcam/UVC, charging-only, network-only, proprietary modes | Does not expose files through these paths; select a file-transfer mode if the camera provides one |

Use a data-capable USB cable connected to the **Pi**, power on the camera, choose **MTP**, **PTP**, **File Transfer**, or **Mass Storage**, then select it under **Connected devices**. For GoPro HERO8/HERO9, change **Connections → USB Connection → MTP** (HERO8 may place Connections inside Preferences). Other models have different menus. GoPro Connect mode alone does not make the card an ordinary USB disk. Close other applications that claim the camera.

Only objects exposed by the camera and its driver are available; some cameras hide sidecars, non-media files, or portions of their storage. Copy all files means all accessible objects, not a complete card image. PiTransfer applies no extension filter and never renders any of those objects. Phone support is not guaranteed; locked devices, iPhones, and devices requiring vendor pairing are outside this feature. Newer or vendor-specific cameras may require newer distro drivers or a USB card reader. See the [libgphoto2 supported-camera list](https://gphoto.github.io/proj/libgphoto2/support/).

If a camera does not appear after updating, reconnect it to apply the new USB permissions. An administrator can run `sudo -u pidrop gphoto2 --auto-detect` to check discovery without opening file contents. A camera shown but unavailable for browsing may be in the wrong USB mode, claimed by another process, locked, or unsupported by the installed driver.

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
- To update an existing installation, finish/cancel transfers and avoid starting another during the update. From the Git checkout run `git pull --ff-only` followed by `sudo bash update.sh`. The updater installs camera dependencies, checks for active transfer history, stages the new code, restarts the service, and retains the previous application in `/opt/pi-nas-drop.update.*.previous`. It preserves `/etc/pi-nas-drop`, NAS credentials, app password, Tailscale settings, and `/var/lib/pi-nas-drop`. Reconnect cameras after the update so udev applies their permissions. `install.sh` still refuses to overwrite an existing configuration.
- To uninstall, stop/disable both units, unmount all app-managed USB partitions, and remove the app, its units and `/etc/sudoers.d/pi-nas-drop`. No source or NAS files need to be deleted.

## Validation and limits

Run `python3 -m unittest discover -s tests -v` in this folder. Tests exercise real temporary-file copies, checksum mismatch detection, cancellation, failure handling, path/symlink containment, source preservation, exclusive destinations, device filtering and HTTP authorization. Camera tests cover metadata-only browsing, recursive/paginated transfers, RAW/video/sidecar objects, unknown sizes, truncation, disconnects, source swaps, subprocess timeouts and cancellation, and the absence of media-serving endpoints. They need no root access or NAS. Install `python3-gphoto2` and run `/usr/bin/python3 -m unittest tests.test_camera_native -v` for the additional native tests; CI runs these explicitly.

The camera update has 60 automated tests, including three native-binding tests using libgphoto2's Directory Browse test driver and three updater tests for settings preservation, active-transfer refusal, and rollback. Production discovery rejects the non-USB test driver. Native tests have passed with python-gphoto2 2.5.1 and 2.6.4; they prove the adapter can stream original bytes through a real fd-backed CameraFile, but do **not** prove hardware compatibility for an individual camera. Physical camera/USB transfers, ARM installation, udev permissions, NAS encryption negotiation, unplug behavior and transfer speed still require testing on your Pi and NAS. Unit tests do not substitute for those hardware checks. A readable malicious filesystem or USB protocol still reaches its respective driver; keep the Pi OS and camera libraries updated.

## Technical references

- [Linux SMB3 mount options and encryption](https://www.kernel.org/doc/html/next/admin-guide/cifs/usage.html)
- [Linux ext4 read-only and noload behavior](https://docs.kernel.org/7.0/admin-guide/ext4.html)
- [Tailscale Serve](https://tailscale.com/docs/reference/tailscale-cli/serve)
- [libgphoto2 camera support and protocols](https://github.com/gphoto/libgphoto2)
- [python-gphoto2 bindings and file APIs](https://github.com/jim-easterbrook/python-gphoto2)
- [GoPro USB connection troubleshooting](https://community.gopro.com/s/article/Camera-is-not-Recognized-when-Plugged-into-a-Computer-USB-Port?language=en_US)

Version: 0.2.0

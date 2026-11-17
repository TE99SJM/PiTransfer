"""Unprivileged, short-lived libgphoto2 worker. No capture, preview or write API.

Metadata is JSON; a download writes only the original object's bytes to stdout.
The fd-backed CameraFile streams through a pipe instead of staging on the Pi.
"""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import sys

USB_PORT = re.compile(r"usb:([0-9]{3}),([0-9]{3})\Z")
CAMERA_ID = re.compile(r"camera:([0-9]{3}):([0-9]{3}):[0-9a-f]{16}\Z")


def components(path):
    if not isinstance(path, str) or len(path) > 4096:
        raise ValueError("Invalid camera path")
    result = path.split("/") if path else []
    if len(result) > 64:
        raise ValueError("Camera directory nesting exceeds 64 levels")
    for name in result:
        safe_name(name)
    return result


def safe_name(name):
    if (not isinstance(name, str) or name in ("", ".", "..")
            or any(c in name for c in ("/", "\\", "\x00"))
            or len(name.encode("utf-8")) > 255
            or any(ord(c) < 32 for c in name)):
        raise ValueError("Camera returned an unsafe file or folder name")
    return name


def identity(model, port, sysroot=Path("/sys/bus/usb/devices")):
    match = USB_PORT.fullmatch(port)
    if not match:
        return None  # Never enable disk:, serial, network, or virtual cameras.
    bus, address = map(int, match.groups())
    for dev in sysroot.iterdir():
        try:
            if int((dev / "busnum").read_text()) != bus or int((dev / "devnum").read_text()) != address:
                continue
            vendor = (dev / "idVendor").read_text().strip()
            product = (dev / "idProduct").read_text().strip()
            serial = (dev / "serial").read_text().strip() if (dev / "serial").exists() else ""
            # sysfs inode changes on reconnection, even for cameras with no serial.
            signature = json.dumps([model, dev.name, vendor, product, serial, dev.stat().st_ino])
            fingerprint = hashlib.sha256(signature.encode()).hexdigest()[:16]
            return f"camera:{bus:03d}:{address:03d}:{fingerprint}"
        except (OSError, ValueError):
            continue
    return None


def discover(gp):
    devices = []
    seen = set()
    for model, port in gp.Camera.autodetect():
        device = identity(model, port)
        if device and port not in seen:
            seen.add(port)
            devices.append({"id": device, "label": model, "node": port,
                            "kind": "camera", "filesystem": "PTP/MTP", "size": None,
                            "usable": True, "mounted": False, "reason": ""})
    return devices


@contextlib.contextmanager
def open_camera(gp, device):
    if not isinstance(device, str) or not CAMERA_ID.fullmatch(device):
        raise ValueError("Invalid camera identifier")
    found = next((d for d in discover(gp) if d["id"] == device), None)
    if found is None:
        raise RuntimeError("Camera disconnected or changed. Refresh and select it again.")
    ports = gp.PortInfoList()
    ports.load()
    abilities = gp.CameraAbilitiesList()
    abilities.load()
    camera = gp.Camera()
    camera.set_abilities(abilities[abilities.lookup_model(found["label"])])
    camera.set_port_info(ports[ports.lookup_path(found["node"])])
    camera.init()
    try:
        if identity(found["label"], found["node"]) != device:
            raise RuntimeError("Camera changed while connecting")
        yield camera
    finally:
        camera.exit()


def entries(camera, path):
    components(path)
    folder = "/" + path
    items = []
    seen = set()
    for kind, values in (("folder", camera.folder_list_folders(folder)),
                         ("file", camera.folder_list_files(folder))):
        for name, _ in values:
            safe_name(name)
            if name in seen:
                raise ValueError("Camera returned duplicate names in one folder")
            seen.add(name)
            items.append({"name": name, "kind": kind, "size": None})
    return sorted(items, key=lambda item: (item["kind"] != "folder", item["name"]))


def file_info(gp, camera, path):
    names = components(path)
    if not names:
        raise ValueError("Choose a camera file")
    info = camera.file_get_info("/" + "/".join(names[:-1]), names[-1]).file
    return {"kind": "file", "size": int(info.size) if info.fields & gp.GP_FILE_INFO_SIZE else None,
            "mtime": int(info.mtime) if info.fields & gp.GP_FILE_INFO_MTIME else None}


def stat_path(gp, camera, path):
    names = components(path)
    if not names:
        return {"kind": "folder"}
    parent = "/".join(names[:-1])
    entry = next((e for e in entries(camera, parent) if e["name"] == names[-1]), None)
    if entry is None:
        raise FileNotFoundError("Camera item is no longer present: " + path)
    return {"kind": "folder"} if entry["kind"] == "folder" else file_info(gp, camera, path)


def download(gp, camera, path, expected):
    before = file_info(gp, camera, path)
    if before != expected:
        raise RuntimeError("Camera file changed before copying: " + path)
    names = components(path)
    fd = os.dup(sys.stdout.fileno())
    try:
        camera_file = gp.check_result(gp.gp_file_new_from_fd(fd))
    except BaseException:
        os.close(fd)
        raise
    try:
        # Only the original file type. Never request thumbnails or previews.
        gp.check_result(gp.gp_camera_file_get(camera, "/" + "/".join(names[:-1]),
                                            names[-1], gp.GP_FILE_TYPE_NORMAL, camera_file))
        if file_info(gp, camera, path) != before:
            raise RuntimeError("Camera file changed during copying: " + path)
    finally:
        # CameraFile owns and closes the duplicate fd.
        del camera_file


def main():
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    # Bound native driver allocations too. A driver requiring a whole large file
    # in RAM must fail, not consume the Pi's memory or stage media on its SD card.
    resource.setrlimit(resource.RLIMIT_AS, (384 * 1024 * 1024, 384 * 1024 * 1024))
    try:
        import gphoto2 as gp
    except ImportError as exc:
        raise RuntimeError("Camera support is not installed. Run the PiTransfer updater.") from exc
    version = tuple(int(x) for x in gp.__version__.split(".")[:2])
    if version < (2, 5):
        raise RuntimeError("Camera streaming requires python3-gphoto2 2.5 or newer (Raspberry Pi OS/Debian Trixie). USB drives still work.")
    request = json.loads(sys.stdin.buffer.read(65537))
    if not isinstance(request, dict):
        raise ValueError("Invalid camera request")
    action = request.get("action")
    if action == "devices":
        result = discover(gp)
    elif action in ("list", "stat", "download"):
        path = request.get("path", "")
        components(path)
        with open_camera(gp, request.get("device")) as camera:
            if action == "list":
                offset = request.get("offset", 0)
                if type(offset) is not int or not 0 <= offset <= 10000000:
                    raise ValueError("Invalid offset")
                items = entries(camera, path)
                result = {"items": items[offset:offset + 200], "offset": offset,
                          "more": len(items) > offset + 200}
            elif action == "stat":
                result = stat_path(gp, camera, path)
            else:
                download(gp, camera, path, request["expected"])
                return
    else:
        raise ValueError("Unsupported camera operation")
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(str(exc)[:800] + "\nCheck the USB data cable, file-transfer mode, and camera permissions; close other camera apps.", file=sys.stderr)
        sys.exit(1)

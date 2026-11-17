"""Unprivileged, bounded metadata extraction. Never decodes or serves media."""
import json
import os
import resource
import subprocess
import sys

TAGS = ('DateTimeOriginal', 'SubSecDateTimeOriginal', 'CreationDate', 'CreateDate',
        'MediaCreateDate', 'TrackCreateDate', 'Model', 'CameraModelName', 'Make', 'FileType')


def extract(fd, executable='/usr/bin/exiftool'):
    result = subprocess.run([executable, '-j', '-G1', '-s', '-fast', '-api', 'QuickTimeUTC=1',
                             *['-' + tag for tag in TAGS], '/proc/self/fd/' + str(fd)],
                            pass_fds=(fd,), capture_output=True, timeout=15,
                            env={'PATH':'/usr/bin:/bin', 'LANG':'C.UTF-8', 'TZ':'UTC'})
    if result.returncode not in (0, 1) or len(result.stdout) > 256 * 1024:
        return {}
    rows = json.loads(result.stdout)
    if not isinstance(rows,list) or not rows or not isinstance(rows[0], dict):
        return {}
    return {k:v for k,v in rows[0].items() if k.split(':')[-1] in TAGS
            and isinstance(v, str) and len(v) <= 512}


if __name__ == '__main__':
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_AS, (256*1024**2, 256*1024**2))
    resource.setrlimit(resource.RLIMIT_CPU, (20, 20))
    try:
        result = extract(int(sys.argv[1]))
    except (OSError, ValueError, subprocess.SubprocessError, MemoryError):
        result = {}
    print(json.dumps(result))

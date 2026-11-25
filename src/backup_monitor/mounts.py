"""Whether a job's source or destination sits on a file system that is read-only.

A job's state is written into its destination, so a destination that turned
read-only cannot report its own failure: the status file keeps the last run's
"ok" for as long as the disk stays that way. The readers therefore look at the
mount table themselves (2026-11-22: SECONDARY, NTFS, set read-only by the
kernel after file system errors, still showed its jobs as fine).

Only the mount table and fstab are read — never the file system itself, which
is the very thing that may be failing (a stuck FUSE request, an NFS server that
went away), and the panels poll this every few seconds.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

MOUNTINFO = '/proc/self/mountinfo'
FSTAB = '/etc/fstab'


@dataclass
class Mount:
    target: str
    source: str
    fstype: str
    read_only: bool


def _unescape(field: str) -> str:
    """Mount tables write space, tab, newline and backslash as octal escapes."""
    out, i = [], 0
    while i < len(field):
        code = field[i + 1:i + 4]
        if field[i] == '\\' and len(code) == 3 and all(c in '01234567' for c in code):
            out.append(chr(int(code, 8)))
            i += 4
        else:
            out.append(field[i])
            i += 1
    return ''.join(out)


def _within(path: str, target: str) -> bool:
    return target == '/' or path == target or path.startswith(target.rstrip('/') + '/')


def mount_of(path: str, mountinfo: str | None = None) -> Mount | None:
    """The mount ``path`` lives on: the deepest mount point above it, and of
    several mounts on one point the last, which is the one in use.

    ``path`` is taken as written — resolving symlinks would touch the file
    system. A mount is read-only when either its own options or its super
    block say so: a kernel that sets a file system read-only after errors
    (errors=remount-ro) changes the super block, while the mount itself still
    says rw."""
    path = os.path.abspath(path)
    best: Mount | None = None
    try:
        with open(mountinfo or os.environ.get('MIRROR_BACKUP_MOUNTINFO', MOUNTINFO),
                  encoding='utf-8', errors='replace') as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    for line in lines:
        head, sep, tail = line.partition(' - ')
        fields, rest = head.split(), tail.split()
        if not sep or len(fields) < 6 or len(rest) < 3:
            continue
        target = _unescape(fields[4])
        if not _within(path, target):
            continue
        if best is not None and len(target) < len(best.target):
            continue
        options = fields[5].split(',') + rest[2].split(',')
        best = Mount(target=target, source=_unescape(rest[1]), fstype=rest[0],
                     read_only='ro' in options)
    return best


def declared_options(target: str, fstab: str | None = None) -> list[str] | None:
    """The options fstab gives the mount point ``target``; None when fstab does
    not declare it (a mount unit, an automounter, mounted by hand)."""
    try:
        with open(fstab or os.environ.get('BACKUP_SYNC_FSTAB') or FSTAB,
                  encoding='utf-8', errors='replace') as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    found = None
    for line in lines:
        fields = line.split('#', 1)[0].split()
        if len(fields) >= 4 and _unescape(fields[1]).rstrip('/') == target.rstrip('/'):
            found = fields[3].split(',')   # the last entry wins, as with mount -a
    return found


def read_only_problem(path: str, role: str, *, mountinfo: str | None = None,
                      fstab: str | None = None) -> str:
    """What is wrong with ``path`` (role 'source' or 'destination') being on a
    read-only file system; '' when nothing is.

    A destination must take writes, so any read-only mount under it is a
    problem. A source may well be read-only on purpose — a snapshot, a disk
    mounted ro — so it is one only when fstab mounts it read-write: then the
    kernel switched it, which it does after file system errors, and reading
    it fails too wherever the damage is."""
    mount = mount_of(path, mountinfo)
    if mount is None or not mount.read_only:
        return ''
    declared = declared_options(mount.target, fstab)
    forced = declared is not None and 'ro' not in declared
    if role == 'source' and not forced:
        return ''
    what = 'Destination' if role == 'destination' else 'Source'
    message = f'{what} is on a read-only file system: {mount.target} ({mount.source})'
    if forced:
        message += (' — fstab mounts it read-write, so the kernel switched it to'
                    ' read-only, which it does after file system errors: check the disk')
    return message

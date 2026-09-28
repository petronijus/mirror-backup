"""File helpers: atomic replacement, and removal that goes through the trash."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path


def atomic_write_text(path: Path, text: str, mode: int | None = None):
    """Replace ``path`` with ``text`` so readers never see a half-written file.

    The temporary file sits next to the target (same filesystem, so the rename
    is atomic) and the target's permissions are kept. A symlinked parent — the
    config dir linked into a git checkout — is followed, not replaced.
    """
    path = Path(path)
    parent = path.parent.resolve()
    parent.mkdir(parents=True, exist_ok=True)
    if mode is None:
        try:
            mode = path.stat().st_mode & 0o7777
        except FileNotFoundError:
            mode = 0o644
    fd, tmp = tempfile.mkstemp(prefix=f'.{path.name}.', suffix='.tmp', dir=parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, parent / path.name)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def trash(path: Path) -> str:
    """Move ``path`` out of the way without destroying it.

    ``gio trash`` first. Where there is no trash (no gio, a filesystem without
    one), the file is renamed to ``<name>.bak-<timestamp>`` next to itself —
    never overwritten, never deleted. Returns a description of where it went.
    """
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        return ''
    gio = shutil.which('gio')
    if gio:
        result = subprocess.run([gio, 'trash', '--', str(path)],
                                capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            return 'trash'
    stamp = time.strftime('%Y%m%d-%H%M%S')
    target = path.with_name(f'{path.name}.bak-{stamp}')
    n = 1
    while target.exists() or target.is_symlink():
        n += 1
        target = path.with_name(f'{path.name}.bak-{stamp}-{n}')
    os.rename(path, target)
    return str(target)

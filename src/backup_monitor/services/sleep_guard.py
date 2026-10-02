"""Pause a poller for the length of a suspend — the Python twin of the
suspend guard in scripts/backup-sync.

Anything that reads a job's state polls the destination, and a destination
may be FUSE (NTFS via ntfs-3g, rclone, sshfs). A process that is inside a
FUSE request when the kernel freezes userspace cannot be frozen: the freezer
stops the FUSE daemon too, the request never completes, and after 20 s the
whole suspend is aborted. Polling every few seconds makes hitting that window
a matter of time (2026-09-29: `status --watch` in `lowntfs-3g` lookup).

So the poller holds a logind *delay* inhibitor. logind announces a suspend
with PrepareForSleep(true) and waits (up to InhibitDelayMaxSec) for every
delay inhibitor. The announcement is handled on the poller's own main loop,
i.e. between two polls, never during one: the poller stops, the inhibitor is
released, and the suspend proceeds with nothing of ours in flight. After
PrepareForSleep(false) the inhibitor is taken again and polling resumes.

A one-shot read (`status --json`, which the GNOME panel runs every few
seconds) has no main loop to pause; :func:`guarded_read` holds the inhibitor
for the length of the read instead, so a suspend announced meanwhile waits
for it. logind refuses new delay inhibitors once a suspend is under way
(OperationInProgress) — that is the cue not to start reading at all.

Uses Gio's D-Bus client only — no GTK — so it stays usable from the CLI.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Callable, Iterator

import gi

gi.require_version('Gio', '2.0')
from gi.repository import Gio, GLib  # noqa: E402

LOGIND_NAME = 'org.freedesktop.login1'
LOGIND_PATH = '/org/freedesktop/login1'
LOGIND_IFACE = 'org.freedesktop.login1.Manager'
DBUS_NAME = 'org.freedesktop.DBus'
DBUS_PATH = '/org/freedesktop/DBus'
DBUS_IFACE = 'org.freedesktop.DBus'

# Returned by Inhibit while a suspend (or shutdown) is already being carried out.
OPERATION_IN_PROGRESS = 'org.freedesktop.login1.OperationInProgress'

# A logind that does not answer must not stall the poller's start.
CALL_TIMEOUT_MS = 5000


class SuspendInProgress(Exception):
    """A suspend has been announced: a destination read started now could
    still be in flight when userspace is frozen."""


def _inhibit(conn: Gio.DBusConnection, name: str, who: str, why: str) -> int:
    """Take a sleep delay inhibitor; returns its file descriptor (closing it
    releases the inhibitor). Raises SuspendInProgress when logind is already
    suspending, GLib.Error for anything else (no logind, no answer)."""
    try:
        reply, fd_list = conn.call_with_unix_fd_list_sync(
            name, LOGIND_PATH, LOGIND_IFACE, 'Inhibit',
            GLib.Variant('(ssss)', ('sleep', who, why, 'delay')),
            GLib.VariantType('(h)'), Gio.DBusCallFlags.NONE, CALL_TIMEOUT_MS, None, None)
    except GLib.Error as e:
        if Gio.DBusError.get_remote_error(e) == OPERATION_IN_PROGRESS:
            raise SuspendInProgress(e.message) from e
        raise
    (index,) = reply.unpack()
    fd = None
    for i, received in enumerate(fd_list.steal_fds() if fd_list is not None else []):
        if i == index:
            fd = received
        else:
            os.close(received)
    if fd is None:
        raise GLib.Error('logind returned no inhibitor file descriptor')
    return fd


@contextmanager
def guarded_read(who: str, why: str, *, connection: Gio.DBusConnection | None = None,
                 name: str = LOGIND_NAME) -> Iterator[bool]:
    """Hold a sleep delay inhibitor around a one-shot read of the destinations.

    Yields whether the inhibitor is held — False when there is no system bus
    or no logind, in which case the read goes ahead unguarded, as it always
    did. Raises SuspendInProgress, before anything is read, when a suspend is
    already under way."""
    fd = None
    try:
        conn = connection or Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        fd = _inhibit(conn, name, who, why)
    except GLib.Error:
        fd = None
    try:
        yield fd is not None
    finally:
        if fd is not None:
            os.close(fd)


class SleepGuard:
    """Holds a delay inhibitor; calls ``on_sleep`` when a suspend is announced
    (the inhibitor is released right after it returns) and ``on_wake`` after
    resume (the inhibitor is taken again right before it runs).

    Callbacks run on the thread-default main context of the thread that
    called :meth:`start`, so the owner must iterate that context — a
    ``GLib.MainLoop`` — and must not block in it while a suspend waits.
    """

    def __init__(self, on_sleep: Callable[[], None], on_wake: Callable[[], None], *,
                 who: str, why: str,
                 connection: Gio.DBusConnection | None = None,
                 name: str = LOGIND_NAME):
        self._on_sleep = on_sleep
        self._on_wake = on_wake
        self._who = who
        self._why = why
        self._conn = connection
        self._name = name
        self._subscription: int | None = None
        self._fd: int | None = None

    @property
    def active(self) -> bool:
        return self._subscription is not None

    @property
    def holding(self) -> bool:
        return self._fd is not None

    def start(self) -> bool:
        """Subscribe to PrepareForSleep and take the inhibitor. False (and no
        guard) when there is no system bus or no logind on it.

        If a suspend is already under way, ``on_sleep`` is called right here —
        the owner must not start polling — and the inhibitor is taken on wake,
        just as after any other suspend."""
        try:
            if self._conn is None:
                self._conn = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            self._subscription = self._conn.signal_subscribe(
                self._name, LOGIND_IFACE, 'PrepareForSleep', LOGIND_PATH, None,
                Gio.DBusSignalFlags.NONE, self._on_signal)
            # The match rule is sent before this call, and the bus handles one
            # connection's messages in order: once logind's owner comes back,
            # no PrepareForSleep can slip past between subscribing and
            # inhibiting. It also tells us that logind is there at all.
            self._conn.call_sync(
                DBUS_NAME, DBUS_PATH, DBUS_IFACE, 'GetNameOwner',
                GLib.Variant('(s)', (self._name,)), GLib.VariantType('(s)'),
                Gio.DBusCallFlags.NONE, CALL_TIMEOUT_MS, None)
            self._acquire()
        except SuspendInProgress:
            self._on_sleep()
        except GLib.Error:
            self.stop()
            return False
        return True

    def stop(self) -> None:
        self._release()
        if self._subscription is not None and self._conn is not None:
            self._conn.signal_unsubscribe(self._subscription)
        self._subscription = None

    def _on_signal(self, _conn, _sender, _path, _iface, _signal, params) -> None:
        (starting,) = params.unpack()
        if starting:
            try:
                self._on_sleep()
            finally:
                self._release()
        else:
            try:
                self._acquire()
            except (GLib.Error, SuspendInProgress):
                pass    # guard is off until the next wake; polling goes on regardless
            self._on_wake()

    def _acquire(self) -> None:
        if self._fd is None:
            self._fd = _inhibit(self._conn, self._name, self._who, self._why)

    def _release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

"""The suspend guard and the pollers that use it, against a fake logind on a
private bus (Gio.TestDBus) — the real logind is never asked for anything.

Needs `dbus-daemon` for the private bus; skipped without it.
"""

from __future__ import annotations

import functools
import io
import os
import select
import shutil
import signal
import sys
import threading
import time
import unittest
import warnings
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

import gi  # noqa: E402

gi.require_version('Gio', '2.0')
from gi.repository import Gio, GLib  # noqa: E402

from backup_monitor import cli  # noqa: E402
from backup_monitor.services import sleep_guard as sg  # noqa: E402
from backup_monitor.services import snapshot  # noqa: E402
from backup_monitor.services import status_monitor  # noqa: E402

FAKE_NAME = sg.LOGIND_NAME      # on the private bus, so no clash with the real one
ABSENT_NAME = 'org.freedesktop.login1.Absent'

MANAGER_XML = '''
<node>
  <interface name="org.freedesktop.login1.Manager">
    <method name="Inhibit">
      <arg type="s" direction="in"/><arg type="s" direction="in"/>
      <arg type="s" direction="in"/><arg type="s" direction="in"/>
      <arg type="h" direction="out"/>
    </method>
    <signal name="PrepareForSleep"><arg type="b"/></signal>
  </interface>
</node>
'''


def spin(predicate, timeout: float = 5.0) -> bool:
    """Iterate the default main context until ``predicate()`` holds."""
    ctx = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        if not ctx.iteration(False):
            time.sleep(0.005)
    return predicate()


def spin_for(seconds: float) -> None:
    spin(lambda: False, seconds)


class FakeLogind:
    """Answers Inhibit and emits PrepareForSleep. It runs on a thread with its
    own main loop, as the real one runs in its own process: the guard calls
    Inhibit synchronously and would otherwise wait on the very loop that has
    to answer."""

    def __init__(self, address: str):
        self.address = address
        self.inhibits: list[tuple[str, str, str, str]] = []
        self.refuse = False                 # answer Inhibit with OperationInProgress
        self._write_ends: list[int] = []    # one pipe per inhibitor handed out
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._ready.wait(5):
            raise RuntimeError('fake logind did not come up')

    def _run(self):
        ctx = GLib.MainContext.new()
        ctx.push_thread_default()
        self._loop = GLib.MainLoop.new(ctx, False)
        self._conn = Gio.DBusConnection.new_for_address_sync(
            self.address,
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
            | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None)
        iface = Gio.DBusNodeInfo.new_for_xml(MANAGER_XML).interfaces[0]
        self._registration = self._conn.register_object(
            sg.LOGIND_PATH, iface, self._on_call, None, None)
        self._conn.call_sync(
            sg.DBUS_NAME, sg.DBUS_PATH, sg.DBUS_IFACE, 'RequestName',
            GLib.Variant('(su)', (FAKE_NAME, 4)), GLib.VariantType('(u)'),
            Gio.DBusCallFlags.NONE, -1, None)
        self._ready.set()
        self._loop.run()
        self._conn.unregister_object(self._registration)
        self._conn.close_sync(None)
        ctx.pop_thread_default()

    def _on_call(self, _conn, _sender, _path, _iface, method, params, invocation):
        if method != 'Inhibit':
            invocation.return_dbus_error('org.freedesktop.DBus.Error.UnknownMethod', method)
            return
        if self.refuse:
            invocation.return_dbus_error(
                sg.OPERATION_IN_PROGRESS,
                'The operation inhibition has been requested for is already running')
            return
        self.inhibits.append(params.unpack())
        read_end, write_end = os.pipe()
        self._write_ends.append(write_end)
        fd_list = Gio.UnixFDList.new()
        fd_list.append(read_end)
        os.close(read_end)
        invocation.return_value_with_unix_fd_list(GLib.Variant('(h)', (0,)), fd_list)

    def held(self, index: int) -> bool:
        """Whether inhibitor ``index`` is still open somewhere (the client has
        not closed its descriptor)."""
        poller = select.poll()
        poller.register(self._write_ends[index], select.POLLERR)
        return not poller.poll(0)

    def held_now(self) -> int:
        return sum(self.held(i) for i in range(len(self._write_ends)))

    def prepare_for_sleep(self, starting: bool) -> None:
        self._conn.emit_signal(None, sg.LOGIND_PATH, sg.LOGIND_IFACE,
                               'PrepareForSleep', GLib.Variant('(b)', (starting,)))

    def close(self):
        self._loop.quit()
        self._thread.join(5)
        for fd in self._write_ends:
            os.close(fd)


@unittest.skipUnless(shutil.which('dbus-daemon'), 'dbus-daemon is needed for a private test bus')
class FakeBus(unittest.TestCase):
    def setUp(self):
        self.bus = Gio.TestDBus.new(Gio.TestDBusFlags.NONE)
        self.bus.up()
        self.addCleanup(self.bus.down)
        self.logind = FakeLogind(self.bus.get_bus_address())
        self.addCleanup(self.logind.close)
        self.conn = Gio.DBusConnection.new_for_address_sync(
            self.bus.get_bus_address(),
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
            | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None)
        self.addCleanup(self.conn.close_sync, None)
        self.sleeps = 0
        self.wakes = 0

    def guard(self, name=FAKE_NAME, on_sleep=None) -> sg.SleepGuard:
        def sleep():
            self.sleeps += 1
            if on_sleep:
                on_sleep()

        def wake():
            self.wakes += 1

        guard = sg.SleepGuard(sleep, wake, who='test', why='testing',
                              connection=self.conn, name=name)
        self.addCleanup(guard.stop)
        return guard


class SleepGuardTest(FakeBus):
    def test_start_takes_a_sleep_delay_inhibitor(self):
        guard = self.guard()
        self.assertTrue(guard.start())
        self.assertTrue(guard.active)
        self.assertTrue(guard.holding)
        self.assertEqual(self.logind.inhibits, [('sleep', 'test', 'testing', 'delay')])
        self.assertTrue(self.logind.held(0))

    def test_suspend_releases_after_the_callback_and_wake_retakes(self):
        held_during_callback = []
        guard = self.guard(on_sleep=lambda: held_during_callback.append(self.logind.held(0)))
        guard.start()

        self.logind.prepare_for_sleep(True)
        self.assertTrue(spin(lambda: self.sleeps == 1 and not self.logind.held(0)))
        self.assertEqual(held_during_callback, [True])   # logind waited for the poller
        self.assertFalse(guard.holding)

        self.logind.prepare_for_sleep(False)
        self.assertTrue(spin(lambda: self.wakes == 1))
        self.assertTrue(guard.holding)
        self.assertEqual(len(self.logind.inhibits), 2)
        self.assertTrue(self.logind.held(1))

    def test_stop_releases_and_unsubscribes(self):
        guard = self.guard()
        guard.start()
        guard.stop()
        self.assertTrue(spin(lambda: not self.logind.held(0)))
        self.assertFalse(guard.active)
        self.logind.prepare_for_sleep(True)
        spin_for(0.2)
        self.assertEqual(self.sleeps, 0)

    def test_no_logind_means_no_guard(self):
        guard = self.guard(name=ABSENT_NAME)
        self.assertFalse(guard.start())
        self.assertFalse(guard.active)
        self.assertFalse(guard.holding)

    def test_start_during_a_suspend_sleeps_until_wake(self):
        self.logind.refuse = True
        guard = self.guard()
        self.assertTrue(guard.start())
        self.assertEqual(self.sleeps, 1)       # the owner must not start polling
        self.assertFalse(guard.holding)

        self.logind.refuse = False
        self.logind.prepare_for_sleep(False)
        self.assertTrue(spin(lambda: self.wakes == 1))
        self.assertTrue(guard.holding)


class GuardedReadTest(FakeBus):
    def test_inhibitor_held_for_the_read_only(self):
        with sg.guarded_read('test', 'read', connection=self.conn, name=FAKE_NAME) as held:
            self.assertTrue(held)
            self.assertTrue(self.logind.held(0))
        self.assertFalse(self.logind.held(0))

    def test_refused_during_a_suspend(self):
        self.logind.refuse = True
        body_ran = False
        with self.assertRaises(sg.SuspendInProgress):
            with sg.guarded_read('test', 'read', connection=self.conn, name=FAKE_NAME):
                body_ran = True
        self.assertFalse(body_ran)

    def test_without_logind_reads_unguarded(self):
        with sg.guarded_read('test', 'read', connection=self.conn, name=ABSENT_NAME) as held:
            self.assertFalse(held)

    def test_released_when_the_read_fails(self):
        with self.assertRaises(OSError):
            with sg.guarded_read('test', 'read', connection=self.conn, name=FAKE_NAME):
                raise OSError('destination went away')
        self.assertFalse(self.logind.held(0))


class StatusCommandTest(FakeBus):
    def setUp(self):
        super().setUp()
        self.reads = 0

        def fake_snapshot(_jobs, **_kw):
            self.reads += 1
            return {'config': '', 'jobs': []}

        for target, value in (
            ('backup_monitor.cli.guarded_read',
             functools.partial(sg.guarded_read, connection=self.conn, name=FAKE_NAME)),
            ('backup_monitor.cli.load_all_jobs', lambda: []),
            ('backup_monitor.services.snapshot.snapshot', fake_snapshot),
        ):
            patcher = mock.patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_reads_under_the_inhibitor(self):
        with mock.patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(cli.print_status(as_json=True), 0)
        self.assertEqual(self.reads, 1)
        self.assertEqual(len(self.logind.inhibits), 1)
        self.assertFalse(self.logind.held(0))

    def test_reads_nothing_during_a_suspend(self):
        self.logind.refuse = True
        with mock.patch('sys.stderr', new_callable=io.StringIO) as err:
            self.assertEqual(cli.print_status(as_json=True), cli.EXIT_SUSPENDING)
        self.assertEqual(self.reads, 0)
        self.assertIn('suspend', err.getvalue())


class StatusWatchTest(FakeBus):
    def setUp(self):
        super().setUp()
        self.reads = 0

        def fake_snapshot(_jobs, **_kw):
            self.reads += 1
            return {'config': '', 'jobs': [{'id': 'j', 'status': {'state': 'idle'}}]}

        for target, value in (
            ('backup_monitor.cli.load_all_jobs', lambda: [{'id': 'j'}]),
            ('backup_monitor.cli._mtime', lambda _p: 1.0),
            ('backup_monitor.services.snapshot.snapshot', fake_snapshot),
            ('backup_monitor.services.snapshot.jobs_timer_info', lambda _jobs: {}),
        ):
            patcher = mock.patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.out = io.StringIO()
        self.watch = cli.StatusWatch(self.out, guard_connection=self.conn, logind_name=FAKE_NAME)
        self.watch.IDLE_S = 0.05
        self.addCleanup(self.watch.stop)

    def test_no_reads_between_suspend_and_wake(self):
        self.watch.start()
        self.assertTrue(spin(lambda: self.reads >= 2))
        self.assertEqual(len(self.out.getvalue().splitlines()), 1)   # unchanged: one line

        self.logind.prepare_for_sleep(True)
        self.assertTrue(spin(lambda: not self.watch.polling and not self.logind.held(0)))
        reads_at_sleep = self.reads
        spin_for(0.3)
        self.assertEqual(self.reads, reads_at_sleep)

        self.logind.prepare_for_sleep(False)
        self.assertTrue(spin(lambda: self.reads > reads_at_sleep))
        self.assertTrue(self.watch.polling)
        self.assertTrue(self.logind.held(1))

    def test_started_during_a_suspend_waits_for_wake(self):
        self.logind.refuse = True
        self.watch.start()
        spin_for(0.2)
        self.assertEqual(self.reads, 0)
        self.assertFalse(self.watch.polling)
        self.logind.refuse = False
        self.logind.prepare_for_sleep(False)
        self.assertTrue(spin(lambda: self.reads >= 1))

    def test_terminates_cleanly_on_sigterm(self):
        GLib.timeout_add(100, lambda: os.kill(os.getpid(), signal.SIGTERM) and False)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            self.assertEqual(self.watch.run(), 0)
        glib_warnings = [str(w.message) for w in caught
                         if not issubclass(w.category, DeprecationWarning)]
        self.assertEqual(glib_warnings, [])
        self.assertFalse(self.logind.held(0))

    def test_polls_on_without_logind(self):
        watch = cli.StatusWatch(self.out, guard_connection=self.conn, logind_name=ABSENT_NAME)
        watch.IDLE_S = 0.05
        self.addCleanup(watch.stop)
        watch.start()
        self.assertTrue(spin(lambda: self.reads >= 2))


class StatusMonitorTest(FakeBus):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(status_monitor.systemd_service, 'get_all_timer_info',
                                    lambda _names, callback, _scopes=None: callback({}))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.monitor = status_monitor.StatusMonitor(
            [], guard_connection=self.conn, logind_name=FAKE_NAME)
        self.updates = 0
        self.monitor.connect('updated', lambda _m: setattr(self, 'updates', self.updates + 1))
        self.addCleanup(self.monitor.stop)

    def test_pauses_across_a_suspend(self):
        self.monitor.start()
        self.assertTrue(self.monitor.polling)

        self.logind.prepare_for_sleep(True)
        self.assertTrue(spin(lambda: not self.monitor.polling and not self.logind.held(0)))
        updates_at_sleep = self.updates
        spin_for(0.2)
        self.assertEqual(self.updates, updates_at_sleep)

        self.logind.prepare_for_sleep(False)
        self.assertTrue(spin(lambda: self.monitor.polling))
        self.assertGreater(self.updates, updates_at_sleep)   # refreshed right away

    def test_repeated_wake_does_not_double_the_polls(self):
        self.monitor.start()
        first = self.monitor._poll_id
        self.logind.prepare_for_sleep(False)
        self.assertTrue(spin(lambda: self.monitor._poll_id not in (None, first)))
        second = self.monitor._poll_id
        self.logind.prepare_for_sleep(False)
        self.assertTrue(spin(lambda: self.monitor._poll_id not in (None, second)))
        ctx = GLib.MainContext.default()
        self.assertIsNone(ctx.find_source_by_id(first))
        self.assertIsNone(ctx.find_source_by_id(second))

if __name__ == '__main__':
    unittest.main()

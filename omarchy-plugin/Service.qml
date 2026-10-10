import QtQuick
import Quickshell
import Quickshell.Io
import "Model.js" as Model

// The widget's data: one long-running `mirror-backup status --watch`, which
// prints the whole snapshot as a JSON line at start and after every change
// (it polls the jobs' state files itself — they may sit on NFS, where inotify
// misses writes from other machines). Controls go through `mirror-backup
// control`, like the desktop app's and the GNOME extension's: it knows whether
// a job is the user's or a system job (run by root, controlled through polkit).
Item {
  id: root

  readonly property string command: Quickshell.env("HOME") + "/.local/bin/mirror-backup"

  property var snapshot: ({ jobs: [] })
  readonly property var jobs: snapshot && snapshot.jobs ? snapshot.jobs : []
  readonly property string summary: Model.summaryState(jobs)
  property bool connected: false
  property string lastError: ""
  property bool _restarting: false

  function accept(line) {
    var text = String(line || "").trim()
    if (text === "") return
    try {
      snapshot = JSON.parse(text)
      connected = true
      lastError = ""
    } catch (e) {
      lastError = "Unreadable status from mirror-backup: " + e
    }
  }

  // Restarting the watcher prints a fresh snapshot at once. The old process
  // exits asynchronously; onExited starts the new one.
  function refresh() {
    restartTimer.stop()
    if (watcher.running) {
      _restarting = true
      watcher.running = false
    } else {
      watcher.running = true
    }
  }

  function control(job, action) {
    if (!job || !job.id) return
    if (["start", "stop", "pause", "resume"].indexOf(action) < 0) return
    Quickshell.execDetached([root.command, "control", job.id, action])
    settleTimer.restart()
  }

  function openApp() {
    Quickshell.execDetached([root.command])
  }

  Process {
    id: watcher
    command: [root.command, "status", "--watch"]
    running: true
    stdout: SplitParser {
      onRead: function(line) { root.accept(line) }
    }
    stderr: StdioCollector {
      id: watcherErrors
    }
    onExited: function(exitCode) {
      if (root._restarting) {
        root._restarting = false
        watcher.running = true
        return
      }
      root.connected = false
      var err = String(watcherErrors.text || "").trim().split("\n").pop()
      root.lastError = err !== "" ? err
        : (exitCode === 127 ? "mirror-backup is not installed (run install.sh)"
          : "mirror-backup status stopped (exit " + exitCode + ")")
      restartTimer.restart()
    }
  }

  // A crashed or missing watcher is retried, not given up on: the command may
  // simply be mid-reinstall.
  Timer {
    id: restartTimer
    interval: 10000
    onTriggered: watcher.running = true
  }

  // systemctl returns before the job has written its first status; ask again
  // once it has had a moment.
  Timer {
    id: settleTimer
    interval: 800
    onTriggered: root.refresh()
  }
}

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model

// Mirror Backup in the Omarchy bar: the counterpart of the GNOME Shell
// extension. The icon shows the state of all jobs at once (urgent on an
// error, pulsing while a backup runs); the panel lists every job with its
// progress, schedule and last run — wherever that run happened — and the
// start / pause / stop controls.
//
// Bar icon: left = panel, right = open the app, middle = refresh.
// Panel keys: j/k move, Enter = the row's main action, x = stop, o = open
// the app, r = refresh, Esc closes.
Panel {
  id: root
  moduleName: "petronijus.mirror-backup"
  ipcTarget: "petronijus.mirror-backup"
  manageIpc: false

  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property color accent: Color.accent
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  readonly property string glyphDisk: String.fromCodePoint(0xF02CA)
  readonly property string glyphPlay: String.fromCodePoint(0xF040A)
  readonly property string glyphPause: String.fromCodePoint(0xF03E4)
  readonly property string glyphStop: String.fromCodePoint(0xF04DB)
  readonly property string glyphOpen: String.fromCodePoint(0xF03CC)

  readonly property var jobs: backup.jobs
  readonly property string summary: backup.summary
  readonly property bool anyActive: Model.isBusy(summary) || jobs.some(function(j) { return Model.isBusy(Model.jobState(j)) })

  // Cursor: 0..jobs.length-1 are job rows, jobs.length is the "open app" row.
  property int cursorIndex: 0
  property bool cursorActive: false
  property double nowMs: Date.now()

  function barIconColor() {
    if (summary === "error") return root.urgent
    if (!backup.connected || jobs.length === 0 || summary === "unavailable") return Qt.darker(root.barForeground, 1.55)
    return root.barForeground
  }

  function stateColor(job) {
    var state = Model.jobState(job)
    if (state === "error") return root.urgent
    if (state === "running" || state === "scanning") return root.accent
    if (state === "idle" && Model.lastRunOk(job)) return root.foreground
    return root.dim
  }

  function moveCursor(dy) {
    var count = jobs.length + 1
    if (!cursorActive) { cursorActive = true; return }
    cursorIndex = Math.max(0, Math.min(count - 1, cursorIndex + dy))
  }

  function activateCursor() {
    if (!cursorActive) return
    if (cursorIndex >= jobs.length) { openApp(); return }
    var acts = Model.actions(jobs[cursorIndex])
    if (acts.length > 0) backup.control(jobs[cursorIndex], acts[0])
  }

  function stopCursor() {
    if (cursorActive && cursorIndex < jobs.length && Model.isBusy(Model.jobState(jobs[cursorIndex])))
      backup.control(jobs[cursorIndex], "stop")
  }

  function openApp() {
    backup.openApp()
    root.close()
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  onOpenedChanged: if (opened) {
    cursorActive = false
    cursorIndex = 0
    nowMs = Date.now()
    backup.refresh()
    Qt.callLater(function() { keyCatcher.forceActiveFocus() })
  }
  onJobsChanged: if (cursorIndex > jobs.length) cursorIndex = jobs.length

  Service {
    id: backup
  }

  IpcHandler {
    target: root.ipcTarget
    function open(): void { root.open() }
    function close(): void { root.close() }
    function toggle(): void { root.toggle() }
    function refresh(): string { backup.refresh(); return "ok" }
    function status(): string { return Model.heroMeta(root.jobs) }
  }

  // Countdowns and "last run 5m ago" stay true while the panel is open.
  Timer {
    interval: 1000
    running: root.opened
    repeat: true
    triggeredOnStart: true
    onTriggered: root.nowMs = Date.now()
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.glyphDisk
    foreground: root.barIconColor()
    tooltipText: backup.connected ? Model.tooltip(root.jobs, Date.now()) : ("Mirror Backup — " + (backup.lastError || "starting…"))
    onPressed: function(buttonCode) {
      if (buttonCode === Qt.RightButton) backup.openApp()
      else if (buttonCode === Qt.MiddleButton) backup.refresh()
      else root.toggle()
    }

    SequentialAnimation on opacity {
      running: root.anyActive
      loops: Animation.Infinite
      alwaysRunToEnd: true
      NumberAnimation { to: 0.35; duration: 1000; easing.type: Easing.InOutSine }
      NumberAnimation { to: 1.0; duration: 1000; easing.type: Easing.InOutSine }
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(400))
    contentHeight: panel.fittedContentHeight(column.implicitHeight, Style.space(640))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onMoveRequested: function(dx, dy) { if (dy !== 0) root.moveCursor(dy) }
      onActivateRequested: root.activateCursor()
      onDeleteRequested: root.stopCursor()
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(t) {
        if (t === "r" || t === "R") backup.refresh()
        else if (t === "o" || t === "O") root.openApp()
      }

      Flickable {
        id: panelFlick
        anchors.fill: parent
        contentWidth: width
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        interactive: contentHeight > height
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

        Column {
          id: column
          width: panelFlick.width
          spacing: Style.space(12)

          PanelHero {
            width: parent.width
            title: "Mirror Backup"
            meta: backup.connected ? Model.heroMeta(root.jobs) : "Not connected"
            foreground: root.foreground
            fontFamily: root.fontFamily
            iconComponent: Component {
              Text {
                text: root.glyphDisk
                color: root.summary === "error" ? root.urgent : root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.display
              }
            }
          }

          Text {
            textFormat: Text.PlainText
            visible: backup.lastError !== ""
            width: parent.width
            text: backup.lastError
            color: root.urgent
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.WordWrap
          }

          Text {
            textFormat: Text.PlainText
            visible: backup.connected && root.jobs.length === 0
            width: parent.width
            text: "No backup jobs yet — create them in the app."
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
            wrapMode: Text.WordWrap
          }

          Column {
            width: parent.width
            spacing: Style.space(6)
            visible: root.jobs.length > 0

            Repeater {
              model: root.jobs
              JobRow {
                required property var modelData
                required property int index
                width: parent.width
                job: modelData
                rowIndex: index
              }
            }
          }

          PanelSeparator {
            foreground: root.foreground
          }

          FooterRow {
            width: parent.width
          }
        }
      }
    }
  }

  component JobRow: CursorSurface {
    id: row
    property var job: null
    property int rowIndex: 0
    readonly property string state: Model.jobState(job)
    readonly property var status: job && job.status ? job.status : ({})
    readonly property bool active: Model.isActive(state)

    hasCursor: root.cursorActive && root.cursorIndex === rowIndex
    foreground: root.foreground
    implicitHeight: body.implicitHeight + Style.space(16)

    MouseArea {
      anchors.fill: parent
      hoverEnabled: true
      acceptedButtons: Qt.NoButton
      onEntered: { root.cursorActive = true; root.cursorIndex = row.rowIndex }
    }

    ColumnLayout {
      id: body
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.space(10)
      anchors.rightMargin: Style.space(10)
      spacing: Style.space(4)

      RowLayout {
        Layout.fillWidth: true
        spacing: Style.space(8)

        Rectangle {
          Layout.alignment: Qt.AlignVCenter
          implicitWidth: Style.space(8)
          implicitHeight: Style.space(8)
          radius: width / 2
          color: root.stateColor(row.job)
          opacity: row.state === "deferred" ? 0.55 : 1
        }

        ColumnLayout {
          Layout.fillWidth: true
          spacing: Style.space(1)

          Text {
            textFormat: Text.PlainText
            Layout.fillWidth: true
            text: row.job ? row.job.name : ""
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
            font.bold: true
            elide: Text.ElideRight
          }

          Text {
            textFormat: Text.PlainText
            Layout.fillWidth: true
            text: row.job ? Model.route(row.job) : ""
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            elide: Text.ElideMiddle
          }
        }

        Text {
          textFormat: Text.PlainText
          Layout.alignment: Qt.AlignVCenter
          text: Model.stateLabel(row.job)
          color: row.state === "error" ? root.urgent : (row.active ? root.accent : root.dim)
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          font.bold: true
        }

        Repeater {
          model: Model.actions(row.job)
          PanelActionButton {
            required property string modelData
            Layout.alignment: Qt.AlignVCenter
            iconText: modelData === "start" ? root.glyphPlay
              : modelData === "pause" ? root.glyphPause
              : modelData === "resume" ? root.glyphPlay : root.glyphStop
            tooltipText: modelData === "start" ? "Start backup"
              : modelData === "pause" ? "Pause" : modelData === "resume" ? "Resume" : "Stop"
            foreground: root.foreground
            hoverColor: modelData === "stop" ? root.urgent : root.foreground
            fontFamily: root.fontFamily
            onClicked: backup.control(row.job, modelData)
          }
        }
      }

      // progress
      Rectangle {
        visible: row.active
        Layout.fillWidth: true
        implicitHeight: Style.space(4)
        radius: height / 2
        color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.14)

        Rectangle {
          anchors.left: parent.left
          anchors.top: parent.top
          anchors.bottom: parent.bottom
          width: parent.width * Math.max(0, Math.min(1, Number(row.status.progress || 0) / 100))
          radius: parent.radius
          color: row.state === "paused" ? root.dim : root.accent
          Behavior on width { NumberAnimation { duration: 400; easing.type: Easing.OutCubic } }
        }
      }

      Text {
        textFormat: Text.PlainText
        visible: row.active && row.state !== "scanning" && !!row.status.current_file
        Layout.fillWidth: true
        text: Model.shortenPath(row.status.current_file)
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        elide: Text.ElideMiddle
      }

      Text {
        textFormat: Text.PlainText
        visible: row.active && text !== ""
        Layout.fillWidth: true
        text: row.job ? Model.activityLine(row.job, root.nowMs) : ""
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        elide: Text.ElideRight
      }

      Text {
        textFormat: Text.PlainText
        visible: (row.state === "error" || row.state === "unavailable") && !!row.status.error
        Layout.fillWidth: true
        text: String(row.status.error || "")
        color: row.state === "error" ? root.urgent : root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        wrapMode: Text.WordWrap
      }

      Text {
        textFormat: Text.PlainText
        visible: text !== ""
        Layout.fillWidth: true
        text: row.job ? Model.scheduleLine(row.job, root.nowMs) : ""
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        elide: Text.ElideRight
      }
    }
  }

  component FooterRow: CursorSurface {
    id: footer
    hasCursor: root.cursorActive && root.cursorIndex === root.jobs.length
    foreground: root.foreground
    implicitHeight: footerRow.implicitHeight + Style.spacing.rowPaddingX

    MouseArea {
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onEntered: { root.cursorActive = true; root.cursorIndex = root.jobs.length }
      onClicked: root.openApp()
    }

    RowLayout {
      id: footerRow
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.space(10)
      anchors.rightMargin: Style.space(10)
      spacing: Style.space(8)

      Text {
        textFormat: Text.PlainText
        Layout.fillWidth: true
        text: "Open Mirror Backup"
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
      }

      Text {
        textFormat: Text.PlainText
        text: root.glyphOpen
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.icon
      }
    }
  }
}

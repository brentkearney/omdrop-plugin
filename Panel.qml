import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// omdrop -- receive files from nearby Apple devices.
//
// This panel owns no state. `bin/omdrop` is the single source of truth: it
// asks systemd whether the receiver is up and asks the radio side whether
// anyone can actually see us, and answers in one JSON line. We poll it while
// the panel is open and while the bar icon is lit, so the widget stays right
// when a window expires or something else turns receiving off.
//
// The three answers that matter are deliberately distinguishable:
//   receiving + visible      someone's sheet can show us
//   receiving + not visible  the server is up but nothing is advertising
//   visible == null          the radio half is absent; we genuinely cannot
//                            tell, and must not draw that as either answer
Panel {
  id: root
  moduleName: "netmojo.omdrop"
  ipcTarget: "netmojo.omdrop"
  manageIpc: false

  // The bar sizes each slot from its item's implicitWidth. Ui/Panel does not
  // provide one, so without this the slot is zero-wide: the widget loads, runs
  // and reports healthy, and paints nothing. Every built-in bar widget sets it.
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  // Shipped beside this file so the widget works without anything on PATH.
  readonly property string cli: Qt.resolvedUrl("bin/omdrop").toString().replace("file://", "")

  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  property bool installed: false      // the CLI answered at all
  property bool receiving: false
  property int visibility: -1         // -1 unknown, 0 no, 1 yes
  property int remaining: -1
  property string deviceName: ""
  property string downloadDir: ""
  property string blockedReason: ""
  property string band: ""
  property bool bandHeld: false

  property string lastError: ""

  readonly property string homePath: Quickshell.env("HOME") || ""

  // ~/Downloads reads better than /home/someone/Downloads.
  function tildify(p) {
    if (homePath !== "" && p.indexOf(homePath) === 0) return "~" + p.substring(homePath.length)
    return p
  }

  // What the radio side says is wrong, in words rather than a token.
  readonly property string reasonText: {
    switch (blockedReason) {
    case "no_awdl0":   return "The AWDL interface is missing. The Wi-Fi driver needs loading."
    // The window is open and the radio is working; the Wi-Fi has simply been
    // moved to a band where Apple devices cannot see this computer. Worth
    // saying plainly, because everything else looks fine from here.
    case "offband":    return "Your Wi-Fi moved to a band Apple devices cannot find this computer on. Omdrop is moving it back."
    // The retry advice this used to carry was disproved by five identical
    // failures: parked-with-a-template-loaded cannot be cleared from userspace,
    // so a retry cannot succeed. The CLI's own message names the way out, and
    // lastError shows it verbatim rather than paraphrasing it into advice.
    case "parked":     return ""
    case "ok":
    case "expired":
    case "stopped":
    case "":           return ""
    // Anything unrecognised is worth showing rather than swallowing, but only
    // when something is actually wrong: while we are visible there is no
    // problem to report, whatever token the radio side is using for "fine".
    default:           return visibility === 1 ? "" : "The radio side reports: " + blockedReason
    }
  }
  property bool busy: false
  // Which way the press was going, captured when it was pressed. Deriving it
  // from `receiving` reads the state we are moving away from, and `omdrop on`
  // starts the receiver before the radio -- so a poll lands mid-flight, sees
  // receiving=true, and the headline of a turn-ON says "Turning off".
  property bool turningOn: false
  property int busyElapsed: 0
  // The radio gate takes 15-60s and roughly half of enables have to retry, so
  // silence here is indistinguishable from a hang. Count up once it is long
  // enough to be worth saying, and never claim a total we cannot predict.
  property string busyDots: "."

  Timer {
    interval: 450
    repeat: true
    running: root.busy || root.settling
    onTriggered: root.busyDots = root.busyDots.length >= 3 ? "." : root.busyDots + "."
    onRunningChanged: if (!running) root.busyDots = "."
  }

  Timer {
    interval: 1000
    repeat: true
    running: root.busy
    onTriggered: root.busyElapsed++
    onRunningChanged: if (!running) root.busyElapsed = 0
  }

  // The CLI answering is not the same as this machine being able to receive.
  // `visible: null` means the radio half is missing, which is the state the
  // struck-through icon and the "Not available" copy are for.
  readonly property bool usable: installed && visibility !== -1

  // The receiver comes up in a moment; the radio takes 15-60s of gating after
  // that, and `omdrop on` returns before it finishes. So there is a stretch
  // where the window is genuinely open and nobody can see us yet -- which is
  // normal, and must not be reported in the same words as a radio that failed.
  // Counted, not computed from a clock: a binding on Date.now() never
  // re-evaluates, so it would latch at whatever it read first.
  property int settleElapsed: 0
  readonly property bool settling: receiving && visibility !== 1 && settleElapsed < 200

  Timer {
    interval: 1000
    repeat: true
    running: root.receiving && root.visibility !== 1
    onTriggered: root.settleElapsed++
    onRunningChanged: if (!running) root.settleElapsed = 0
  }

  // Discrete stops rather than a free 0-120 scale: the useful values are few,
  // and the two ends are modes, not durations. `settings.windowMinutes` (the
  // widget's own config) seeds the starting position; moving the slider
  // overrides it for this session.
  readonly property var windowStops: [
    { arg: "once",    label: "One file, then off" },
    { arg: "1m",      label: "1 minute" },
    { arg: "5m",      label: "5 minutes" },
    { arg: "10m",     label: "10 minutes" },
    { arg: "15m",     label: "15 minutes" },
    { arg: "30m",     label: "30 minutes" },
    { arg: "60m",     label: "1 hour" },
    { arg: "forever", label: "Until I turn it off" }
  ]
  property int windowIndex: defaultWindowIndex()
  readonly property string windowArg: windowStops[windowIndex].arg

  function defaultWindowIndex() {
    var want = settings.windowMinutes || 10
    for (var i = 0; i < windowStops.length; i++)
      if (windowStops[i].arg === want + "m") return i
    return 3
  }

  // The slider is a control AND a report: while a window is running it has to
  // show the window that is running, not the setting the widget was configured
  // with. Turning on with "Until I turn it off" and reopening the panel showed
  // 10 minutes -- the toggle told the truth and the row beneath it did not.
  //
  // Only on a change of mode, so a running window never fights a user who is
  // dragging the slider to pick the next one. Going off leaves the last
  // position alone: it is what they chose, and most likely what they want next.
  property string windowMode: ""
  function syncWindowToRunning(mode) {
    if (mode === windowMode) return
    windowMode = mode
    var i
    if (mode === "once")    { windowIndex = 0; return }
    if (mode === "forever") { windowIndex = windowStops.length - 1; return }
    if (mode === "timed" && remaining > 0) {
      // Only what is LEFT is knowable, so land on the shortest stop that could
      // still be running: a 10-minute window with 4 minutes left reads as 5.
      var mins = remaining / 60
      for (i = 1; i < windowStops.length - 1; i++)
        if (parseInt(windowStops[i].arg) >= mins) { windowIndex = i; return }
    }
  }
  readonly property color barIconColor: receiving ? barForeground : Qt.darker(barForeground, 1.55)

  // A band move is its own phase, and it is the slow one: NetworkManager tears
  // the connection down and back up. Measured on this hardware, 2.5-4s
  // typically and occasionally 30-50s when the 2.4 scan is cold. Saying
  // "Making you visible" through that is not wrong, but it tells the user
  // nothing about why their Wi-Fi just went away.
  readonly property bool switchingBand: busy && bandHeld && band !== "" && band !== "2.4"

  // The headline names the state of the thing the switch controls: Omdrop is
  // on or it is off. "Not receiving" described a symptom, and read as a
  // complaint about a machine that was working exactly as asked.
  //
  // The shades of "on" -- advertising but unanswered, receiver up with nothing
  // on the radio -- live in detailText, which is where an explanation belongs.
  // The one exception stays a headline: a window that is open while nothing
  // can see us is a fault, not a nuance, and must not read as "on".
  readonly property string stateText: {
    if (!usable) return "Not available"
    if (switchingBand) return "Switching to 2.4 GHz" + busyDots
    if (busy) return (turningOn ? "Turning on" : "Turning off") + busyDots
    if (settling) return "Waking the radio" + busyDots
    if (!receiving) return "Omdrop off"
    if (visibility === 0) return "Nobody can see you yet"
    return "Omdrop on"
  }


  // One sentence, naming what they will see and roughly when.
  readonly property string visibleText: {
    var who = deviceName !== "" ? "\"" + deviceName + "\"" : "this computer"
    return "Nearby Apple devices should see " + who + " in AirDrop, after 10 seconds."
  }

  readonly property string detailText: {
    if (!usable) return doctorText !== "" ? doctorText
                                          : "Needs the AWDL radio support this plugin's README describes."
    if (switchingBand) return "AirDrop needs 2.4 GHz. Your Wi-Fi drops for a few seconds, and comes back when you turn Omdrop off."
    if (settling) return "The radio takes up to a minute to come up."
    if (lastError !== "") return lastError
    if (reasonText !== "") return reasonText
    if (!receiving) return "Nearby Apple devices cannot see this computer."
    if (visibility === 0) return "The receiver is up, but nothing is advertising on the radio."
    if (visibility === -1) return "Cannot tell whether anyone can see us."
    return visibleText
  }

  // The row that set the window becomes the row that reports it: while a timed
  // window runs, the chosen duration is replaced by what is left of it. Only
  // for timed windows -- "one file" and "until I turn it off" have nothing to
  // count down, so they keep their label.
  readonly property bool countingDown: receiving && remaining > 0
  readonly property string windowValueText: {
    if (!countingDown) return windowStops[windowIndex].label
    var m = Math.floor(remaining / 60), sec = remaining % 60
    return (m < 10 ? "0" : "") + m + ":" + (sec < 10 ? "0" : "") + sec
  }

  readonly property string countdownText: {
    if (busy) {
      if (busyElapsed < 4) return ""
      var m = Math.floor(busyElapsed / 60), s2 = busyElapsed % 60
      var phase = turningOn ? "WAKING THE RADIO" : "STOPPING"
      return phase + " · " + m + ":" + (s2 < 10 ? "0" : "") + s2
    }
    return ""
  }

  function toggleOmdrop() {
    if (busy || !usable) return
    turningOn = !receiving
    busy = true
    lastError = ""
    toggleProc.running = true
  }

  Process {
    id: toggleProc
    command: [root.cli, "toggle", root.windowArg]
    // A refusal is the interesting case: the CLI declines rather than
    // pretending, and without this the panel would just sit there.
    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.lastError = text.trim()
    }
    onExited: function(code) {
      root.busy = false
      if (code === 0) root.lastError = ""
      statusProc.running = true
    }
  }

  // Both of these are validated by the receiver, not here: it refuses to start
  // on a bad value, and the CLI puts the old one back if that happens. A
  // failure therefore shows up as the field snapping back on the next poll.
  Process { id: setNameProc; onExited: statusProc.running = true }
  Process { id: setDirProc;  onExited: statusProc.running = true }

  function setDeviceName(v) {
    if (v === "" || v === root.deviceName) return
    setNameProc.command = [root.cli, "name", v]
    setNameProc.running = true
  }

  function setDownloadDir(v) {
    if (v === "" || v === root.downloadDir) return
    setDirProc.command = [root.cli, "dir", v]
    setDirProc.running = true
  }

  Process {
    id: statusProc
    command: [root.cli, "status", "--json"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.applyStatus(text)
    }
    onExited: function(code) { if (code !== 0) root.installed = false }
  }

  // "Not available" is the entire first-run experience for anyone who installs
  // the plugin before the driver package, and it explains nothing. The CLI
  // already knows exactly which piece is absent, in a sentence written for
  // someone holding a laptop; ask it, and say that instead.
  //
  // Only when we cannot work: a healthy panel never spawns this.
  property string doctorText: ""
  property bool driverInstallable: false
  Process {
    id: doctorProc
    command: [root.cli, "doctor", "--json"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        try {
          var d = JSON.parse(text)
          var missing = d.missing || []
          root.doctorText = missing.length ? missing[0].say : ""
          // Both are installed by the same press: the driver is a package to
          // build, the support library an AUR package the receiver imports.
          root.driverInstallable = missing.some(function(m) {
            return m.id === "driver" || m.id === "library"
          })
        } catch (e) {
          root.doctorText = ""
          root.driverInstallable = false
        }
      }
    }
  }

  // Fire and forget: the terminal it opens is the user interface from here on,
  // and re-asking doctor when it exits catches the case where they installed.
  Process {
    id: installDriverProc
    command: [root.cli, "install-driver"]
    onExited: { root.doctorText = ""; statusProc.running = true }
  }

  function applyStatus(text) {
    try {
      var s = JSON.parse(text)
      installed = true
      receiving = !!s.receiving
      visibility = (s.visible === null || s.visible === undefined) ? -1 : (s.visible ? 1 : 0)
      remaining = (s.remaining === null || s.remaining === undefined) ? -1 : s.remaining
      deviceName = s.name || ""
      downloadDir = s.dir || ""
      blockedReason = s.reason || ""
      band = s.band || ""
      bandHeld = !!s.band_held
      syncWindowToRunning(s.mode || "off")
      if (!nameField.activeFocus) nameField.text = deviceName
      if (!dirField.activeFocus) dirField.text = tildify(downloadDir)
      // Asked once per transition into unusable, not on every poll.
      if (!usable && doctorText === "" && !doctorProc.running) doctorProc.running = true
      if (usable) { doctorText = ""; driverInstallable = false }
    } catch (e) {
      installed = false
    }
  }

  // Once a second while the panel is open so the countdown ticks; every ten
  // seconds otherwise, which is enough for the bar icon to notice a window
  // that closed on its own.
  // The countdown ticks locally. Asking the CLI once a second would spawn a
  // dozen processes a second for a number we can count ourselves.
  Timer {
    interval: 1000
    repeat: true
    running: root.countingDown
    onTriggered: {
      root.remaining--
      // Reaching zero is the moment the window ends, so stop showing it as
      // running: the timer unit is firing `omdrop off` right now, and waiting
      // for the next poll to notice leaves the switch sitting in the wrong
      // position -- which is exactly what it did.
      if (root.remaining <= 0) {
        root.remaining = -1
        root.receiving = false
        root.visibility = 0
        statusProc.running = true
      }
    }
  }

  // The resync. Slower than the tick, and what notices a window that ended
  // somewhere else -- an expiry we did not predict, or someone running
  // `omdrop off` in a terminal.
  Timer {
    interval: root.opened ? 3000 : 10000
    repeat: true
    running: true
    triggeredOnStart: true
    onTriggered: statusProc.running = true
  }

  // Opening the panel should show the truth immediately, not up to ten
  // seconds of whatever was true when it was last closed.
  onOpenedChanged: if (opened && !busy) statusProc.running = true

  IpcHandler {
    target: root.ipcTarget
    function toggle(): string { root.toggleOmdrop(); return "ok" }
    function status(): string { return root.stateText }
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    iconComponent: Component {
      Item {
        OmdropIcon {
          anchors.centerIn: parent
          iconSize: Style.space(12)
          color: root.barIconColor
          crossed: !root.usable
          drifting: root.busy || root.settling
        }
      }
    }
    onPressed: function(buttonCode) {
      if (buttonCode === Qt.RightButton) root.toggleOmdrop()
      else root.toggle()
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(300))
    contentHeight: panel.fittedContentHeight(column.implicitHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.controller.hide()
      onActivateRequested: root.toggleOmdrop()
      onTextKey: function(t) { if (t === "d" || t === "D") root.toggleOmdrop() }

      Column {
        id: column
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        spacing: Style.space(12)

        Item {
          width: parent.width
          implicitHeight: Math.max(heroIcon.implicitHeight, heroLabels.implicitHeight, heroSwitch.implicitHeight)

          OmdropIcon {
            id: heroIcon
            iconSize: Style.font.display
            color: root.busy || root.receiving ? root.foreground : root.dim
            crossed: !root.usable
            drifting: root.busy || root.settling
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
          }

          ToggleSwitch {
            id: heroSwitch
            visible: root.usable
            checked: root.receiving
            busy: root.busy
            foreground: root.foreground
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            onToggled: root.toggleOmdrop()
          }

          Column {
            id: heroLabels
            anchors.left: heroIcon.right
            anchors.leftMargin: Style.space(14)
            anchors.right: parent.right
            anchors.rightMargin: heroSwitch.visible ? heroSwitch.width + Style.space(12) : 0
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(2)

            Text {
              width: parent.width
              text: root.stateText
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.title
              font.bold: true
              elide: Text.ElideRight
            }

            Text {
              width: parent.width
              visible: root.countdownText !== ""
              text: root.countdownText
              color: Qt.darker(root.foreground, 1.4)
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              font.bold: true
              font.letterSpacing: 1.2
            }
          }
        }

        PanelSeparator { foreground: root.foreground }

        Text {
          width: parent.width
          visible: text !== ""
          text: root.detailText
          color: Qt.darker(root.foreground, 1.5)
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
          wrapMode: Text.WordWrap
        }

        // The one thing a first run can act on from here. Everything else
        // doctor reports is either a command (`omdrop setup`) or a restart, so
        // only the missing driver gets a button -- and it opens a terminal
        // rather than installing anything quietly. Installing a kernel module
        // is the user's decision, taken at their own sudo prompt.
        Button {
          visible: root.driverInstallable
          text: "Install the Wi-Fi driver"
          bordered: true
          foreground: root.foreground
          onClicked: installDriverProc.running = true
        }

        PanelSeparator { visible: root.usable; foreground: root.foreground }

        // How long a press of the switch keeps us visible. Both ends are
        // modes: the floor stops after one file arrives, the ceiling never
        // stops on its own.
        Column {
          visible: root.usable
          width: parent.width
          spacing: Style.space(8)

          Item {
            width: parent.width
            implicitHeight: Math.max(windowHeader.implicitHeight, windowValue.implicitHeight)

            PanelSectionHeader {
              id: windowHeader
              text: "STAY VISIBLE FOR"
              foreground: root.foreground
              fontFamily: root.fontFamily
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
            }

            Text {
              id: windowValue
              text: root.windowValueText
              // Under a minute it goes urgent: the last stretch is when you
              // care whether to press again.
              color: root.countingDown && root.remaining <= 60 ? root.urgent : root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
              font.bold: root.countingDown
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter

              Behavior on color { ColorAnimation { duration: 200 } }
            }
          }

          PanelSlider {
            width: parent.width
            bar: root.bar
            minimum: 0
            maximum: root.windowStops.length - 1
            step: 1
            integer: true
            tickCount: root.windowStops.length
            value: root.windowIndex
            onMoved: function(v) { root.windowIndex = Math.round(v) }
            onReleased: function(v) { root.windowIndex = Math.round(v) }
          }
        }

        PanelSeparator { visible: root.usable; foreground: root.foreground }

        Column {
          visible: root.usable
          width: parent.width
          spacing: Style.space(8)

          PanelSectionHeader {
            text: "THEY SEE YOU AS"
            foreground: root.foreground
            fontFamily: root.fontFamily
          }

          TextField {
            id: nameField
            width: parent.width
            foreground: root.foreground
            text: root.deviceName
            // Don't fight the user's typing: only take the polled value back
            // when this field is not the one being edited.
            onActiveFocusChanged: if (!activeFocus) text = root.deviceName
            onAccepted: root.setDeviceName(text)
          }
        }

        Column {
          visible: root.usable
          width: parent.width
          spacing: Style.space(8)

          PanelSectionHeader {
            text: "SAVE FILES TO"
            foreground: root.foreground
            fontFamily: root.fontFamily
          }

          TextField {
            id: dirField
            width: parent.width
            foreground: root.foreground
            text: root.tildify(root.downloadDir)
            onActiveFocusChanged: if (!activeFocus) text = root.tildify(root.downloadDir)
            onAccepted: root.setDownloadDir(text)
          }
        }
      }
    }
  }
}

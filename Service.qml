import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons

// Singleton owner of the Python backend. One daemon serves every bar (one
// per monitor): widgets read the mirrored state below and send commands
// through configure()/refresh()/markRead().
Item {
  id: root

  property var shell: null
  property var manifest: null

  // ---- state mirrored from the daemon -----------------------------------
  property string status: "starting"
  property string message: ""
  property var accounts: []
  property var counts: ({ "1h": 0, "24h": 0, "7d": 0, "all": 0 })
  property bool capped: false
  property var messages: []
  property string inboxUrl: "https://mail.proton.me/inbox"
  property bool multiAccount: false
  property bool syncing: false
  readonly property bool signedIn: status !== "signed-out" && accounts.length > 0

  // ---- backend process ---------------------------------------------------
  readonly property string pluginDir: {
    var url = Qt.resolvedUrl(".").toString()
    return decodeURIComponent(url.replace(/^file:\/\//, "")).replace(/\/$/, "")
  }
  readonly property string backend: pluginDir + "/backend/promarchyctl"
  readonly property string iconPath: pluginDir + "/assets/promarchy.svg"

  property var _settings: null
  property string _settingsJson: ""
  property int _restarts: 0
  property bool _stopping: false

  function configure(settings) {
    var json = JSON.stringify(settings || {})
    if (json === _settingsJson) return
    _settingsJson = json
    _settings = settings
    send({ cmd: "configure", settings: settings })
  }

  function refresh() {
    syncing = true
    syncingTimer.restart()
    send({ cmd: "refresh" })
  }

  // A cheap event-stream check (one request), used when the dropdown opens.
  function poll() {
    send({ cmd: "poll" })
  }

  function markRead(account, ids) {
    if (!account || !ids || ids.length === 0) return
    send({ cmd: "markRead", account: account, ids: ids })
  }

  function send(command) {
    if (daemon.running) daemon.write(JSON.stringify(command) + "\n")
  }

  // ---- opening things ------------------------------------------------------
  function openUrl(url, openWith) {
    var target = String(url || inboxUrl)
    if (!/^https:\/\/mail\.proton\.me\//.test(target)) return
    if (openWith === "webapp") Quickshell.execDetached(["omarchy-launch-webapp", target])
    else Quickshell.execDetached(["omarchy-launch-browser", target])
  }

  // Focus a window already showing Proton Mail rather than opening another.
  function openInbox(openWith) {
    var launcher = openWith === "webapp" ? "omarchy-launch-webapp" : "omarchy-launch-browser"
    Quickshell.execDetached(["omarchy-launch-or-focus", "Proton Mail", launcher + " https://mail.proton.me/inbox"])
  }

  function compose(openWith) {
    openUrl("https://mail.proton.me/inbox#mailto=mailto%3A", openWith)
  }

  function signIn() {
    Quickshell.execDetached([
      "omarchy-launch-floating-terminal-with-presentation",
      "python3 -I " + Util.shellQuote(backend) + " login"
    ])
  }

  function signOut() {
    Quickshell.execDetached([
      "omarchy-launch-floating-terminal-with-presentation",
      "python3 -I " + Util.shellQuote(backend) + " logout --all"
    ])
  }

  function ingest(line) {
    var state
    try {
      state = JSON.parse(line)
    } catch (e) {
      return
    }
    if (!state || state.type !== "state") return
    _restarts = 0
    status = String(state.status || "error")
    message = String(state.message || "")
    accounts = Array.isArray(state.accounts) ? state.accounts : []
    counts = state.counts || counts
    capped = state.capped === true
    messages = Array.isArray(state.messages) ? state.messages : []
    inboxUrl = String(state.inboxUrl || inboxUrl)
    multiAccount = state.multiAccount === true
    syncing = false
  }

  Process {
    id: daemon
    command: ["python3", "-I", root.backend, "daemon", "--icon", root.iconPath]
    stdinEnabled: true
    stdout: SplitParser {
      onRead: function(line) { root.ingest(line) }
    }
    stderr: SplitParser {
      onRead: function(line) { console.warn("[promarchy] " + line) }
    }
    onStarted: if (root._settings) root.send({ cmd: "configure", settings: root._settings })
    onExited: function(exitCode) {
      if (root._stopping) return
      root.status = "error"
      root.message = "Mail backend stopped (exit " + exitCode + "); restarting…"
      restartTimer.interval = Math.min(60000, 2000 * Math.pow(2, Math.min(root._restarts, 5)))
      restartTimer.restart()
    }
  }

  Timer {
    id: restartTimer
    repeat: false
    onTriggered: {
      root._restarts++
      daemon.running = true
    }
  }

  // The daemon answers a refresh within a second or two; never spin forever.
  Timer {
    id: syncingTimer
    interval: 15000
    onTriggered: root.syncing = false
  }

  Component.onCompleted: daemon.running = true
  Component.onDestruction: {
    _stopping = true
    daemon.running = false
  }
}

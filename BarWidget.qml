import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "Model.js" as Model

// Bar icon with an unread count, plus a keyboard-driven dropdown of recent
// mail. One instance exists per monitor; all of them read the same Service.
Panel {
  id: root
  moduleName: "atlastimed.promarchy"
  manageIpc: false

  readonly property string pluginId: "atlastimed.promarchy"
  property var mail: null
  // Panel (unlike BarWidget) does not define these.
  readonly property bool vertical: bar ? bar.vertical : false

  // ---- settings ------------------------------------------------------------
  function flag(name, fallback) {
    var value = setting(name, fallback)
    if (typeof value === "string") return !/^(false|0|no|off)$/i.test(value.trim())
    return value === undefined || value === null ? fallback : !!value
  }

  readonly property string countWindow: Model.normalizeWindow(setting("countWindow", "24h"))
  readonly property string headerWindow: countWindow === "off" ? "24h" : countWindow
  readonly property bool notifyEnabled: flag("notify", true)
  readonly property string openWith: setting("openWith", "browser") === "webapp" ? "webapp" : "browser"
  readonly property var daemonConfig: ({
    pollSeconds: Number(setting("pollSeconds", 30)),
    recentLimit: Number(setting("recentCount", 8)),
    categories: String(setting("categories", "all")),
    groupThreads: flag("groupThreads", true),
    notify: notifyEnabled,
    senderImages: flag("senderImages", true),
    // true, or a sample state such as "offline" (see backend/promarchy/demo.py)
    demo: String(setting("demo", "false"))
  })

  // ---- state -----------------------------------------------------------------
  readonly property string status: mail ? mail.status : "starting"
  readonly property string warning: mail ? mail.warning : ""
  readonly property bool demo: mail ? mail.demo : false
  readonly property var counts: mail ? mail.counts : ({})
  readonly property bool capped: mail ? mail.capped : false
  readonly property var messages: mail ? mail.messages : []
  readonly property var accounts: mail ? mail.accounts : []
  readonly property bool signedIn: mail ? mail.signedIn : false
  readonly property int badgeCount: Model.countFor(counts, countWindow)
  readonly property int headerCount: Model.countFor(counts, headerWindow)
  // "partial": some accounts work and some do not; counts are still shown.
  readonly property bool healthy: status === "ok" || status === "partial"
  // Counts from an earlier sync (or paused by Proton): shown, but not as if
  // they were current.
  readonly property bool stale: status === "offline" || status === "error" || status === "auth-expired"
    || status === "throttled"
  readonly property bool listEnabled: daemonConfig.recentLimit > 0
  readonly property bool needsSignIn: {
    if (status === "signed-out" || status === "auth-expired") return true
    for (var i = 0; i < accounts.length; i++) if (accounts[i].status === "auth-expired") return true
    return false
  }
  readonly property bool anyUnreadShown: {
    for (var i = 0; i < messages.length; i++) if (messages[i].unread) return true
    return false
  }
  readonly property string accountLabel: accounts.length === 1 ? accounts[0].email
    : accounts.length > 1 ? accounts.length + " accounts" : ""

  // ---- look --------------------------------------------------------------------
  // Panel text follows the bar's text colour, as first-party panels do; the
  // secondary shade is derived against the popup's own background with a
  // contrast floor (Qt.darker turns it unreadable on some themes and brighter
  // than the main text on light ones).
  readonly property color foreground: bar ? bar.foreground : Commons.Color.foreground
  readonly property color surface: Commons.Color.popups.background
  readonly property color dim: {
    var c = Model.secondary(foreground, surface, 4.5, 0.4)
    return Qt.rgba(c.r, c.g, c.b, 1)
  }
  readonly property color highlight: bar ? bar.urgent : Commons.Color.urgent
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property color barDim: {
    var c = Model.secondary(barForeground, Commons.Color.bar.background, 3.0, 0.45)
    return Qt.rgba(c.r, c.g, c.b, 1)
  }
  readonly property color barIconColor: healthy || status === "starting" ? barForeground : barDim
  readonly property string statusMark: Model.statusMark(status, warning)

  // ---- cursor ------------------------------------------------------------------
  // One highlight for pointer and keyboard, in one of two places: a mail row
  // ("rows") or an action in the accounts section ("accounts").
  property int cursorIndex: 0
  property bool cursorActive: false
  property string cursorZone: "rows"
  property int accountCursor: 0
  // Which message the row cursor is on, so it follows that message (or
  // lets go) when new mail moves the rows.
  property string cursorKey: ""
  property real now: Date.now()
  property bool accountsOpen: false
  property bool confirmSignOut: false

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  function resolveService() {
    if (mail) return
    var api = bar ? bar.shell : null
    if (api && typeof api.serviceFor === "function") mail = api.serviceFor(pluginId) || null
  }

  function pushConfig() {
    if (mail) mail.configure(daemonConfig)
  }

  function messageKey(message) {
    return message ? String(message.account || "") + "\n" + String(message.id || "") : ""
  }

  onBarChanged: resolveService()
  onMailChanged: pushConfig()
  onDaemonConfigChanged: pushConfig()
  onMessagesChanged: {
    if (cursorActive && cursorZone === "rows" && cursorKey !== "") {
      for (var i = 0; i < messages.length; i++) {
        if (messageKey(messages[i]) === cursorKey) {
          cursorIndex = i
          return
        }
      }
      cursorActive = false  // the selected message left the list: select nothing
    }
    if (cursorIndex >= messages.length) cursorIndex = Math.max(0, messages.length - 1)
  }
  onOpenedChanged: if (opened) {
    now = Date.now()
    cursorActive = false
    cursorZone = "rows"
    cursorIndex = 0
    cursorKey = ""
    accountsOpen = false
    confirmSignOut = false
    if (flick) flick.contentY = 0
    if (mail) mail.poll()
    Qt.callLater(function() { keyCatcher.forceActiveFocus() })
  }
  onAccountsOpenChanged: {
    if (!accountsOpen) {
      confirmSignOut = false
      if (cursorZone === "accounts") {
        cursorZone = "rows"
        cursorActive = false
      }
    } else {
      revealTimer.restart()
    }
  }
  onConfirmSignOutChanged: {
    // A destructive confirmation starts on its safe choice.
    accountCursor = confirmSignOut ? 1 : 0
    if (accountsOpen) revealTimer.restart()
  }

  // The service can appear after the widget (plugin rescans, hot reloads).
  Timer {
    interval: 400
    repeat: true
    running: !root.mail
    triggeredOnStart: true
    onTriggered: root.resolveService()
  }

  Timer {
    interval: 30000
    repeat: true
    running: root.opened
    onTriggered: root.now = Date.now()
  }

  // Scroll the accounts section into view once its new height is laid out.
  Timer {
    id: revealTimer
    interval: 40
    onTriggered: root.revealItem(accountSection)
  }

  // Likewise the notice when it says something new: it scrolls with the list,
  // and feedback scrolled out of sight would be lost.
  Timer {
    id: noticeRevealTimer
    interval: 40
    onTriggered: root.revealItem(notice)
  }

  // ---- actions -----------------------------------------------------------------
  function persist(values) {
    var entry = { id: root.moduleName }
    for (var existing in root.settings) if (existing !== "id") entry[existing] = root.settings[existing]
    for (var key in values) entry[key] = values[key]
    root.settings = entry
    if (root.bar && root.bar.shell && typeof root.bar.shell.updateEntryInline === "function")
      root.bar.shell.updateEntryInline(root.moduleName, entry)
  }

  function setWindow(value) {
    if (Model.WINDOWS.indexOf(value) !== -1 && value !== countWindow) persist({ countWindow: value })
  }

  function stepWindow(delta) {
    var index = Model.WINDOWS.indexOf(headerWindow)
    var next = Math.max(0, Math.min(Model.WINDOWS.length - 1, index + delta))
    setWindow(Model.WINDOWS[next])
  }

  function openMessage(message) {
    if (!mail || !message) return
    mail.openUrl(message.url, openWith)
    close()
  }

  function openInbox() {
    if (mail) mail.openInbox(openWith)
    close()
  }

  function compose() {
    if (mail) mail.compose(openWith)
    close()
  }

  // Sign-in opens a real terminal: never from the sample data of demo mode.
  function signIn() {
    if (!mail || demo) return
    mail.signIn()
    close()
  }

  function markRead(message) {
    if (mail && message && message.unreadIds && message.unreadIds.length)
      mail.markRead(message.account, message.unreadIds, message.kind)
  }

  // A row action needs a row the user can see: if none is selected, or the
  // selected one has scrolled out of view, the key first shows it.
  function showsSelectedRow() {
    if (!cursorActive || cursorZone !== "rows" || messages.length === 0) return false
    var item = rows.itemAt(cursorIndex)
    if (!item || !flick || !flick.visible) return false
    var top = item.mapToItem(flick, 0, 0).y
    return top >= -0.5 && top + item.height <= flick.height + 0.5
  }

  function revealSelectedRow() {
    if (messages.length === 0) return
    if (cursorZone !== "rows" || !cursorActive) setCursor(Math.min(cursorIndex, messages.length - 1))
    scrollCursorIntoView()
  }

  function markSelectedRead() {
    if (messages.length === 0) return
    if (!showsSelectedRow()) {
      revealSelectedRow()
      return
    }
    markRead(selectedMessage())
  }

  function markAllRead() {
    if (!mail) return
    var groups = {}
    for (var i = 0; i < messages.length; i++) {
      var m = messages[i]
      if (!m.unread) continue
      var key = m.account + "\n" + (m.kind || "message")
      if (!groups[key]) groups[key] = { account: m.account, kind: m.kind || "message", ids: [] }
      groups[key].ids = groups[key].ids.concat(m.unreadIds || [])
    }
    for (var k in groups) mail.markRead(groups[k].account, groups[k].ids, groups[k].kind)
  }

  function selectedMessage() {
    return messages.length > 0 ? messages[Math.max(0, Math.min(cursorIndex, messages.length - 1))] : null
  }

  function setCursor(index) {
    cursorZone = "rows"
    cursorActive = true
    cursorIndex = index
    cursorKey = messageKey(messages[index])
  }

  // The accounts section's actions, in order, for the keyboard cursor.
  function accountActions() {
    if (!accountsOpen || demo) return []
    return confirmSignOut ? ["confirm", "cancel"] : ["add", "signout"]
  }

  function setAccountCursor(index) {
    cursorZone = "accounts"
    cursorActive = true
    accountCursor = index
  }

  function runAccountAction(action) {
    if (action === "add") signIn()
    else if (action === "signout") confirmSignOut = true
    else if (action === "cancel") confirmSignOut = false
    else if (action === "confirm") {
      confirmSignOut = false
      accountsOpen = false
      if (mail && !demo) mail.signOut()
    }
  }

  // From the keyboard the cursor moves into the section; a click leaves the
  // highlight with the pointer (one highlight at a time).
  function toggleAccounts(fromKeyboard) {
    accountsOpen = !accountsOpen
    if (fromKeyboard && accountsOpen && accountActions().length > 0) setAccountCursor(0)
  }

  function moveCursor(dx, dy) {
    var actions = accountActions()
    if (cursorZone === "accounts" && cursorActive && actions.length > 0) {
      var step = dx !== 0 ? dx : dy
      if (step < 0 && accountCursor === 0 && dx === 0) {
        if (messages.length > 0) setCursor(messages.length - 1)  // back up into the list
        scrollCursorIntoView()
        return
      }
      setAccountCursor(Math.max(0, Math.min(actions.length - 1, accountCursor + step)))
      revealTimer.restart()
      return
    }
    if (dx !== 0) {
      stepWindow(dx)
      return
    }
    if (messages.length === 0) {
      if (dy > 0 && actions.length > 0) setAccountCursor(0)
      return
    }
    if (!cursorActive) {
      revealSelectedRow()
      return
    }
    if (dy > 0 && cursorIndex === messages.length - 1 && actions.length > 0) {
      setAccountCursor(0)  // down from the last row into the accounts section
      revealTimer.restart()
      return
    }
    setCursor(Math.max(0, Math.min(messages.length - 1, cursorIndex + dy)))
    scrollCursorIntoView()
  }

  function activateCursor() {
    if (!signedIn) {
      signIn()
      return
    }
    var actions = accountActions()
    if (cursorZone === "accounts" && cursorActive && accountCursor < actions.length) {
      runAccountAction(actions[accountCursor])
      return
    }
    if (!cursorActive) {
      openInbox()
      return
    }
    if (!showsSelectedRow()) {
      revealSelectedRow()
      return
    }
    openMessage(selectedMessage())
  }

  function handleKey(text) {
    switch (text) {
    case "r": if (mail) mail.refresh(); break
    case "m": markSelectedRead(); break
    case "a": markAllRead(); break
    case "o": openInbox(); break
    case "c": compose(); break
    case "n": persist({ notify: !notifyEnabled }); break
    case "s": if (signedIn) toggleAccounts(true); break
    case "1": setWindow("1h"); break
    case "2": setWindow("24h"); break
    case "3": setWindow("7d"); break
    case "4": setWindow("all"); break
    }
  }

  function revealItem(item) {
    if (!item || !flick || !item.visible) return
    var top = item.mapToItem(flick.contentItem, 0, 0).y
    var bottom = top + item.height
    var maxY = Math.max(0, flick.contentHeight - flick.height)
    if (bottom > flick.contentY + flick.height) flick.contentY = Math.min(maxY, bottom - flick.height)
    if (top < flick.contentY) flick.contentY = Math.max(0, top)
  }

  function scrollCursorIntoView() {
    Qt.callLater(function() {
      var item = rows.itemAt(cursorIndex)
      if (!item || !flick) return
      var point = item.mapToItem(flick.contentItem, 0, 0)
      var margin = Style.space(6)
      var maxY = Math.max(0, flick.contentHeight - flick.height)
      if (point.y < flick.contentY + margin) flick.contentY = Math.max(0, point.y - margin)
      else if (point.y + item.height > flick.contentY + flick.height - margin)
        flick.contentY = Math.min(maxY, point.y + item.height + margin - flick.height)
    })
  }

  // ---- bar button ----------------------------------------------------------------
  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    labelVisible: false
    hasVisualContent: true
    fixedWidth: root.vertical ? -1 : badgeLayout.implicitWidth + Style.space(14)
    fixedHeight: root.vertical ? badgeLayout.implicitHeight + Style.space(12) : -1
    tooltipText: root.opened ? "" : Model.tooltip(root.status, root.badgeCount, root.countWindow, root.capped, root.warning)

    onPressed: function(buttonCode) {
      if (buttonCode === Qt.RightButton) root.openInbox()
      else if (buttonCode === Qt.MiddleButton) { if (root.mail) root.mail.refresh() }
      else root.toggle()
    }

    Grid {
      id: badgeLayout
      anchors.centerIn: parent
      columns: root.vertical ? 1 : 3
      columnSpacing: Style.space(4)
      rowSpacing: Style.space(1)
      horizontalItemAlignment: Grid.AlignHCenter
      verticalItemAlignment: Grid.AlignVCenter

      ProtonMark {
        size: Style.bar.iconCanvas - 1
        // Unread mail takes the bar's attention colour only while the count
        // is current; stale counts keep their shape but step back.
        color: root.badgeCount > 0 && !root.stale ? root.highlight : root.barIconColor
        filled: root.badgeCount > 0

        Behavior on color {
          enabled: !root.bar || root.bar.foregroundAnimationEnabled
          ColorAnimation { duration: 160 }
        }
      }

      Text {
        textFormat: Text.PlainText
        visible: text !== ""
        text: Model.badgeText(root.badgeCount)
        color: root.stale ? root.barDim : root.highlight
        font.family: root.fontFamily
        font.pixelSize: root.vertical ? Style.font.caption : Style.font.bodySmall
        font.bold: true
      }

      // Offline, paused by Proton, needs attention, or a sign-in not yet
      // saved: visible without opening the panel, and not by colour alone.
      Text {
        textFormat: Text.PlainText
        visible: text !== ""
        text: root.statusMark
        color: root.status === "offline" || root.status === "throttled" ? root.barDim : root.highlight
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
      }
    }
  }

  // ---- dropdown --------------------------------------------------------------------
  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(400))
    contentHeight: panel.fittedContentHeight(layout.implicitHeight, Style.space(680))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onMoveRequested: function(dx, dy) { root.moveCursor(dx, dy) }
      onActivateRequested: root.activateCursor()
      onCloseRequested: root.close()
      onDeleteRequested: root.markSelectedRead()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(t) { root.handleKey(t) }

      // The title and count row and the footer stay put; everything of
      // variable height (notices, the list, the accounts section) scrolls in
      // between, so no control can end up below the screen.
      ColumnLayout {
        id: layout
        anchors.fill: parent
        spacing: Style.space(12)

        PanelHero {
          id: hero
          Layout.fillWidth: true
          title: "Proton Mail"
          meta: root.signedIn && (root.healthy || root.status === "throttled")
            ? Model.headline(root.headerCount, root.headerWindow, root.capped)
            : Model.statusLine(root.status, root.mail ? root.mail.message : "")
          foreground: root.foreground
          fontFamily: root.fontFamily
          iconComponent: Component {
            ProtonMark {
              size: Style.font.display
              color: root.foreground
              filled: root.headerCount > 0
            }
          }
          trailingControl: Component {
            Button {
              visible: root.signedIn
              iconText: "󰑐"
              tooltipText: "Refresh (r)"
              iconSpinning: root.mail ? root.mail.syncing : false
              foreground: hero.foreground
              fontFamily: hero.fontFamily
              horizontalPadding: Style.spacing.sm
              verticalPadding: Style.spacing.sm
              onClicked: if (root.mail) root.mail.refresh()
            }
          }
        }

        // The count window: what the badge and headline count. The list
        // below always shows the most recent inbox mail.
        RowLayout {
          visible: root.signedIn
          Layout.fillWidth: true
          spacing: Style.space(8)

          Text {
            textFormat: Text.PlainText
            text: root.countWindow === "off" ? "Badge off" : "Unread from"
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }

          ButtonGroup {
            options: [
              { value: "1h", label: "1h", tooltip: "Count unread mail from the last hour (1)" },
              { value: "24h", label: "24h", tooltip: "Count unread mail from the last 24 hours (2)" },
              { value: "7d", label: "7d", tooltip: "Count unread mail from the last 7 days (3)" },
              { value: "all", label: "All", tooltip: "Count all unread mail in the inbox (4)" }
            ]
            value: root.countWindow
            focusable: false
            foreground: root.foreground
            fontFamily: root.fontFamily
            fontSize: Style.font.bodySmall
            onChanged: function(v) { root.setWindow(v) }
          }

          Text {
            Layout.fillWidth: true
            textFormat: Text.PlainText
            horizontalAlignment: Text.AlignRight
            text: root.accountLabel
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            elide: Text.ElideMiddle
          }
        }

        Flickable {
          id: flick
          // From the state, never from the content's own height: hidden
          // children measure zero, so that would never show again.
          visible: notice.text !== "" || (root.signedIn && (root.listEnabled || root.accountsOpen))
          Layout.fillWidth: true
          Layout.fillHeight: true
          Layout.preferredHeight: scrollColumn.implicitHeight
          Layout.minimumHeight: Math.min(scrollColumn.implicitHeight, Style.space(80))
          contentWidth: width
          contentHeight: scrollColumn.implicitHeight
          clip: true
          boundsBehavior: Flickable.StopAtBounds
          flickableDirection: Flickable.VerticalFlick
          interactive: contentHeight > height
          ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

          Column {
            id: scrollColumn
            width: flick.width
            spacing: Style.space(12)

            // Sign-in, expired session, offline and error states, plus
            // anything the backend reported that the user can act on.
            CursorSurface {
              id: notice
              readonly property bool needsSignIn: root.needsSignIn
              readonly property string text: Model.noticeText(root.status, root.mail ? root.mail.message : "", root.warning)
              visible: text !== ""
              onTextChanged: if (text !== "" && root.opened) noticeRevealTimer.restart()
              width: parent.width
              implicitHeight: noticeColumn.implicitHeight + Style.spacing.rowPaddingX * 2
              foreground: root.foreground
              bordered: true

              Column {
                id: noticeColumn
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                anchors.margins: Style.spacing.rowPaddingX
                spacing: Style.space(10)

                Text {
                  textFormat: Text.PlainText
                  width: parent.width
                  wrapMode: Text.WordWrap
                  lineHeight: 1.1
                  color: root.status === "error" || root.status === "partial" || root.warning !== "" ? root.highlight : root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.body
                  text: notice.text
                }

                Text {
                  visible: root.demo && notice.needsSignIn
                  width: parent.width
                  textFormat: Text.PlainText
                  wrapMode: Text.WordWrap
                  text: "Demo mode shows sample data, so sign-in is off here."
                  color: root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.bodySmall
                }

                Button {
                  visible: !root.demo && (notice.needsSignIn || root.status === "error")
                  text: notice.needsSignIn ? "Sign in" : "Retry"
                  iconText: notice.needsSignIn ? "󰍂" : "󰑐"
                  bordered: true
                  foreground: root.foreground
                  fontFamily: root.fontFamily
                  onClicked: {
                    if (notice.needsSignIn) root.signIn()
                    else if (root.mail) root.mail.refresh()
                  }
                }
              }
            }

            PanelSeparator {
              visible: root.signedIn && root.listEnabled
              width: parent.width
              foreground: root.foreground
            }

            PanelSectionHeader {
              visible: root.signedIn && root.listEnabled
              width: parent.width
              text: "RECENT INBOX"
              foreground: root.foreground
              fontFamily: root.fontFamily
            }

            Text {
              visible: root.signedIn && root.listEnabled && root.messages.length === 0
              width: parent.width
              textFormat: Text.PlainText
              wrapMode: Text.WordWrap
              text: Model.emptyText(root.status, root.daemonConfig.recentLimit)
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
            }

            Column {
              id: rowColumn
              visible: root.signedIn && root.listEnabled && root.messages.length > 0
              width: parent.width
              spacing: Style.space(2)

              Repeater {
                id: rows
                model: root.messages

                MessageRow {
                  required property var modelData
                  required property int index
                  width: rowColumn.width
                  message: modelData
                  rowIndex: index
                }
              }
            }

            // Account maintenance, kept apart from everyday mail actions.
            Column {
              id: accountSection
              visible: root.signedIn && root.accountsOpen
              width: parent.width
              spacing: Style.space(8)

              PanelSeparator {
                width: parent.width
                foreground: root.foreground
              }

              PanelSectionHeader {
                width: parent.width
                text: root.accounts.length > 1 ? "ACCOUNTS" : "ACCOUNT"
                foreground: root.foreground
                fontFamily: root.fontFamily
              }

              Repeater {
                model: root.accounts

                RowLayout {
                  required property var modelData
                  width: accountSection.width
                  spacing: Style.space(8)

                  Text {
                    Layout.fillWidth: true
                    textFormat: Text.PlainText
                    text: modelData.email
                    color: root.foreground
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.bodySmall
                    elide: Text.ElideMiddle
                  }

                  Text {
                    textFormat: Text.PlainText
                    text: Model.accountState(modelData.status)
                    color: modelData.status === "ok" || modelData.status === "starting" ? root.dim : root.highlight
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                  }
                }
              }

              Text {
                visible: root.demo
                width: parent.width
                textFormat: Text.PlainText
                wrapMode: Text.WordWrap
                text: "Demo mode shows sample mail, so account actions are off here."
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
              }

              RowLayout {
                visible: !root.demo && !root.confirmSignOut
                width: parent.width
                spacing: Style.space(6)

                Button {
                  text: "Add account"
                  iconText: "󰀔"
                  tooltipText: "Sign in to another Proton account"
                  bordered: true
                  hasCursor: root.cursorActive && root.cursorZone === "accounts" && root.accountCursor === 0
                  foreground: root.foreground
                  fontFamily: root.fontFamily
                  fontSize: Style.font.bodySmall
                  onHovered: function(on) { if (on) root.setAccountCursor(0) }
                  onClicked: root.runAccountAction("add")
                }

                Item { Layout.fillWidth: true }

                Button {
                  text: root.accounts.length > 1 ? "Sign out of all" : "Sign out"
                  iconText: "󰍃"
                  bordered: true
                  hasCursor: root.cursorActive && root.cursorZone === "accounts" && root.accountCursor === 1
                  foreground: root.foreground
                  fontFamily: root.fontFamily
                  fontSize: Style.font.bodySmall
                  onHovered: function(on) { if (on) root.setAccountCursor(1) }
                  onClicked: root.runAccountAction("signout")
                }
              }

              // Signing back in may need a CAPTCHA, so the scope is spelled
              // out and there is a plain way back (where the cursor starts).
              Column {
                visible: !root.demo && root.confirmSignOut
                width: parent.width
                spacing: Style.space(8)

                Text {
                  width: parent.width
                  textFormat: Text.PlainText
                  wrapMode: Text.WordWrap
                  text: Model.signOutQuestion(root.accounts.length)
                  color: root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.body
                  font.bold: true
                }

                Text {
                  width: parent.width
                  textFormat: Text.PlainText
                  wrapMode: Text.WordWrap
                  text: "Signing back in needs your password and 2FA code, and Proton may ask for a browser check."
                  color: root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.bodySmall
                }

                Row {
                  spacing: Style.space(6)

                  Button {
                    text: "Sign out"
                    iconText: "󰍃"
                    bordered: true
                    hasCursor: root.cursorActive && root.cursorZone === "accounts" && root.accountCursor === 0
                    foreground: root.highlight
                    fontFamily: root.fontFamily
                    fontSize: Style.font.bodySmall
                    onHovered: function(on) { if (on) root.setAccountCursor(0) }
                    onClicked: root.runAccountAction("confirm")
                  }

                  Button {
                    text: "Cancel"
                    bordered: true
                    hasCursor: root.cursorActive && root.cursorZone === "accounts" && root.accountCursor === 1
                    foreground: root.foreground
                    fontFamily: root.fontFamily
                    fontSize: Style.font.bodySmall
                    onHovered: function(on) { if (on) root.setAccountCursor(1) }
                    onClicked: root.runAccountAction("cancel")
                  }
                }
              }
            }
          }
        }

        PanelSeparator {
          visible: root.signedIn
          Layout.fillWidth: true
          foreground: root.foreground
        }

        RowLayout {
          visible: root.signedIn
          Layout.fillWidth: true
          spacing: Style.space(6)

          Button {
            text: "Inbox"
            iconText: "󰚇"
            tooltipText: "Open Proton Mail (o)"
            bordered: true
            foreground: root.foreground
            fontFamily: root.fontFamily
            fontSize: Style.font.bodySmall
            onClicked: root.openInbox()
          }

          Button {
            text: "Compose"
            iconText: "󰏫"
            tooltipText: "New message (c)"
            bordered: true
            foreground: root.foreground
            fontFamily: root.fontFamily
            fontSize: Style.font.bodySmall
            onClicked: root.compose()
          }

          Item { Layout.fillWidth: true }

          PanelActionButton {
            visible: root.anyUnreadShown
            iconText: "󰄭"
            tooltipText: "Mark these as read (a)"
            foreground: root.foreground
            fontFamily: root.fontFamily
            onClicked: root.markAllRead()
          }

          PanelActionButton {
            iconText: root.notifyEnabled ? "󰂚" : "󰂛"
            tooltipText: root.notifyEnabled ? "Notifications on (n)" : "Notifications paused (n)"
            foreground: root.notifyEnabled ? root.foreground : root.dim
            fontFamily: root.fontFamily
            onClicked: root.persist({ notify: !root.notifyEnabled })
          }

          Button {
            iconText: "󰀄"
            tooltipText: root.accountsOpen ? "Hide accounts (s)" : "Accounts: add another, sign out (s)"
            selected: root.accountsOpen
            foreground: root.foreground
            fontFamily: root.fontFamily
            horizontalPadding: Style.spacing.sm
            verticalPadding: Style.spacing.sm
            onClicked: root.toggleAccounts(false)
          }
        }
      }
    }
  }

  // ---- components ---------------------------------------------------------------------
  component MessageRow: CursorSurface {
    id: row
    property var message: ({})
    property int rowIndex: 0
    readonly property bool unread: message.unread === true
    readonly property string senderLine: {
      var name = message.senderName || ""
      var address = message.senderAddress || ""
      if (name && address && name !== address) return name + " <" + address + ">"
      return name || address || "Unknown sender"
    }

    hasCursor: root.cursorActive && root.cursorZone === "rows" && root.cursorIndex === rowIndex
    foreground: root.foreground
    implicitHeight: rowContent.implicitHeight + Style.space(14)

    MouseArea {
      id: rowMouse
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      acceptedButtons: Qt.LeftButton | Qt.MiddleButton
      onEntered: root.setCursor(row.rowIndex)
      onClicked: function(mouse) {
        if (mouse.button === Qt.MiddleButton) root.markRead(row.message)
        else root.openMessage(row.message)
      }
    }

    // The full sender, address and subject, which the row may elide;
    // wrapped to the panel's width (the shared tooltip does not wrap).
    PanelToolTip {
      visible: rowMouse.containsMouse
      text: Model.wrapLines(row.senderLine + "\n" + (row.message.subject || "(no subject)")
        + (root.mail && root.mail.multiAccount ? "\n" + String(row.message.account || "") : ""), 46)
      fontFamily: root.fontFamily
    }

    RowLayout {
      id: rowContent
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.space(8)
      anchors.rightMargin: Style.space(6)
      spacing: Style.space(10)

      SenderAvatar {
        message: row.message
        Layout.alignment: Qt.AlignVCenter
      }

      ColumnLayout {
        Layout.fillWidth: true
        spacing: Style.space(2)

        RowLayout {
          Layout.fillWidth: true
          spacing: Style.space(6)

          Text {
            textFormat: Text.PlainText
            Layout.fillWidth: true
            text: row.message.senderName || row.message.senderAddress || "Unknown sender"
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
            font.bold: row.unread
            elide: Text.ElideRight
          }

          Text {
            textFormat: Text.PlainText
            visible: (row.message.threadCount || 1) > 1
            text: "×" + row.message.threadCount
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }

          Text {
            textFormat: Text.PlainText
            text: Model.shortTime(row.message.time, root.now)
            color: row.unread ? root.foreground : root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }
        }

        RowLayout {
          Layout.fillWidth: true
          spacing: Style.space(6)

          Text {
            textFormat: Text.PlainText
            Layout.fillWidth: true
            text: row.message.subject || "(no subject)"
            color: row.unread ? root.foreground : root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            elide: Text.ElideRight
          }

          Text {
            textFormat: Text.PlainText
            visible: (row.message.attachments || 0) > 0
            text: "󰏢"
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
          }

          Text {
            textFormat: Text.PlainText
            visible: row.message.starred === true
            text: "󰓎"
            color: root.highlight
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
          }

          // Which account, kept short so it never crowds out the subject.
          Text {
            textFormat: Text.PlainText
            visible: root.mail ? root.mail.multiAccount : false
            Layout.maximumWidth: Style.space(84)
            text: String(row.message.account || "").split("@")[0]
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            elide: Text.ElideRight
          }
        }
      }

      Item {
        Layout.preferredWidth: Style.space(24)
        Layout.preferredHeight: Style.space(24)
        Layout.alignment: Qt.AlignVCenter

        Rectangle {
          visible: row.unread && !row.hasCursor
          anchors.centerIn: parent
          width: Style.space(7)
          height: width
          radius: width / 2
          color: root.highlight
        }

        PanelActionButton {
          anchors.centerIn: parent
          visible: row.unread && row.hasCursor
          iconText: "󰄬"
          tooltipText: "Mark as read (m)"
          foreground: root.foreground
          fontFamily: root.fontFamily
          onClicked: root.markRead(row.message)
        }
      }
    }
  }

  component SenderAvatar: Item {
    id: avatar
    property var message: ({})
    readonly property string source: message && message.avatar ? Util.fileUrl(message.avatar) : ""

    implicitWidth: Style.space(30)
    implicitHeight: Style.space(30)

    // Proton serves brand logos for light backgrounds (many are opaque with
    // their own white), so a logo sits on a plain white tile; otherwise a
    // themed initial. Both share the control border, and corners follow the
    // theme's radius rather than snapping to a circle.
    BorderSurface {
      anchors.fill: parent
      radius: Math.min(Style.cornerRadius, width / 2)
      color: logo.status === Image.Ready ? "#ffffff" : Style.normalFillFor(root.foreground, Commons.Color.accent)
      borderSpec: Border.controlSpec("normal", root.foreground, Commons.Color.accent)
    }

    Text {
      anchors.centerIn: parent
      visible: logo.status !== Image.Ready
      textFormat: Text.PlainText
      text: Model.initial(avatar.message.senderName, avatar.message.senderAddress)
      color: root.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.font.subtitle
      font.bold: true
    }

    Image {
      id: logo
      anchors.fill: parent
      anchors.margins: Style.space(4)
      source: avatar.source
      visible: status === Image.Ready
      asynchronous: true
      cache: true
      smooth: true
      mipmap: true
      fillMode: Image.PreserveAspectFit
      sourceSize.width: 64
      sourceSize.height: 64
    }
  }
}

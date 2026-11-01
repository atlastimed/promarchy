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
  // Counts from an earlier sync: shown, but not as if they were current.
  readonly property bool stale: status === "offline" || status === "error" || status === "auth-expired"
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
  property int cursorIndex: 0
  property bool cursorActive: false
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

  onBarChanged: resolveService()
  onMailChanged: pushConfig()
  onDaemonConfigChanged: pushConfig()
  onMessagesChanged: if (cursorIndex >= messages.length) cursorIndex = Math.max(0, messages.length - 1)
  onOpenedChanged: if (opened) {
    now = Date.now()
    cursorActive = false
    cursorIndex = 0
    accountsOpen = false
    confirmSignOut = false
    if (flick) flick.contentY = 0
    if (mail) mail.poll()
    Qt.callLater(function() { keyCatcher.forceActiveFocus() })
  }
  onAccountsOpenChanged: if (!accountsOpen) confirmSignOut = false

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

  function markRead(message) {
    if (mail && message && message.unreadIds && message.unreadIds.length)
      mail.markRead(message.account, message.unreadIds, message.kind)
  }

  // A row action needs a row the user can see: the first press only shows
  // the cursor, the next one acts on it.
  function markSelectedRead() {
    if (messages.length === 0) return
    if (!cursorActive) {
      cursorActive = true
      scrollCursorIntoView()
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
    cursorActive = true
    cursorIndex = index
  }

  function moveCursor(dx, dy) {
    if (dx !== 0) {
      stepWindow(dx)
      return
    }
    if (messages.length === 0) return
    if (!cursorActive) {
      cursorActive = true
      scrollCursorIntoView()
      return
    }
    cursorIndex = Math.max(0, Math.min(messages.length - 1, cursorIndex + dy))
    scrollCursorIntoView()
  }

  function activateCursor() {
    if (!signedIn) {
      if (mail) mail.signIn()
      close()
      return
    }
    if (cursorActive) openMessage(selectedMessage())
    else openInbox()
  }

  function handleKey(text) {
    switch (text) {
    case "r": if (mail) mail.refresh(); break
    case "m": markSelectedRead(); break
    case "a": markAllRead(); break
    case "o": openInbox(); break
    case "c": compose(); break
    case "n": persist({ notify: !notifyEnabled }); break
    case "1": setWindow("1h"); break
    case "2": setWindow("24h"); break
    case "3": setWindow("7d"); break
    case "4": setWindow("all"); break
    }
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

      // Offline, needs attention, or a sign-in not yet saved: visible without
      // opening the panel, and not by colour alone.
      Text {
        textFormat: Text.PlainText
        visible: text !== ""
        text: root.statusMark
        color: root.status === "offline" ? root.barDim : root.highlight
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

      // Header and footer stay put; only the list scrolls.
      ColumnLayout {
        id: layout
        anchors.fill: parent
        spacing: Style.space(12)

        PanelHero {
          id: hero
          Layout.fillWidth: true
          title: "Proton Mail"
          meta: root.signedIn && root.healthy
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

        // Sign-in, expired session, offline and error states, plus anything
        // the backend reported that the user can act on.
        CursorSurface {
          id: notice
          readonly property bool needsSignIn: root.needsSignIn
          readonly property string text: Model.noticeText(root.status, root.mail ? root.mail.message : "", root.warning)
          visible: text !== ""
          Layout.fillWidth: true
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

            Button {
              visible: notice.needsSignIn || root.status === "error"
              text: notice.needsSignIn ? "Sign in" : "Retry"
              iconText: notice.needsSignIn ? "󰍂" : "󰑐"
              bordered: true
              foreground: root.foreground
              fontFamily: root.fontFamily
              onClicked: {
                if (!root.mail) return
                if (notice.needsSignIn) {
                  root.mail.signIn()
                  root.close()
                } else {
                  root.mail.refresh()
                }
              }
            }
          }
        }

        PanelSeparator {
          visible: root.signedIn && root.listEnabled
          Layout.fillWidth: true
          foreground: root.foreground
        }

        PanelSectionHeader {
          visible: root.signedIn && root.listEnabled
          Layout.fillWidth: true
          text: "RECENT INBOX"
          foreground: root.foreground
          fontFamily: root.fontFamily
        }

        Text {
          visible: root.signedIn && root.listEnabled && root.messages.length === 0
          Layout.fillWidth: true
          textFormat: Text.PlainText
          wrapMode: Text.WordWrap
          text: Model.emptyText(root.status, root.daemonConfig.recentLimit)
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
        }

        Flickable {
          id: flick
          visible: root.signedIn && root.listEnabled && root.messages.length > 0
          Layout.fillWidth: true
          Layout.fillHeight: true
          Layout.preferredHeight: rowColumn.implicitHeight
          Layout.minimumHeight: Math.min(rowColumn.implicitHeight, Style.space(120))
          contentWidth: width
          contentHeight: rowColumn.implicitHeight
          clip: true
          boundsBehavior: Flickable.StopAtBounds
          flickableDirection: Flickable.VerticalFlick
          interactive: contentHeight > height
          ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

          Column {
            id: rowColumn
            width: flick.width
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
        }

        // Account maintenance, kept apart from everyday mail actions.
        Column {
          id: accountSection
          visible: root.signedIn && root.accountsOpen
          Layout.fillWidth: true
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
              foreground: root.foreground
              fontFamily: root.fontFamily
              fontSize: Style.font.bodySmall
              onClicked: {
                if (root.mail) root.mail.signIn()
                root.close()
              }
            }

            Item { Layout.fillWidth: true }

            Button {
              text: root.accounts.length > 1 ? "Sign out of all" : "Sign out"
              iconText: "󰍃"
              bordered: true
              foreground: root.foreground
              fontFamily: root.fontFamily
              fontSize: Style.font.bodySmall
              onClicked: root.confirmSignOut = true
            }
          }

          // Signing back in may need a CAPTCHA, so the scope is spelled out
          // and there is a plain way back.
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
                foreground: root.highlight
                fontFamily: root.fontFamily
                fontSize: Style.font.bodySmall
                onClicked: {
                  root.confirmSignOut = false
                  root.accountsOpen = false
                  if (root.mail) root.mail.signOut()
                }
              }

              Button {
                text: "Cancel"
                bordered: true
                foreground: root.foreground
                fontFamily: root.fontFamily
                fontSize: Style.font.bodySmall
                onClicked: root.confirmSignOut = false
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
            tooltipText: root.accountsOpen ? "Hide accounts" : "Accounts: add another, sign out"
            selected: root.accountsOpen
            foreground: root.foreground
            fontFamily: root.fontFamily
            horizontalPadding: Style.spacing.sm
            verticalPadding: Style.spacing.sm
            onClicked: root.accountsOpen = !root.accountsOpen
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

    hasCursor: root.cursorActive && root.cursorIndex === rowIndex
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

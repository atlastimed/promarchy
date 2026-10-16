import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import qs.Commons
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
    senderImages: flag("senderImages", true)
  })

  // ---- state -----------------------------------------------------------------
  readonly property string status: mail ? mail.status : "starting"
  readonly property var counts: mail ? mail.counts : ({})
  readonly property bool capped: mail ? mail.capped : false
  readonly property var messages: mail ? mail.messages : []
  readonly property bool signedIn: mail ? mail.signedIn : false
  readonly property int badgeCount: Model.countFor(counts, countWindow)
  readonly property int headerCount: Model.countFor(counts, headerWindow)
  readonly property bool healthy: status === "ok"
  readonly property bool anyUnreadShown: {
    for (var i = 0; i < messages.length; i++) if (messages[i].unread) return true
    return false
  }

  // ---- look --------------------------------------------------------------------
  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property color highlight: bar ? bar.urgent : Color.urgent
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property color barIconColor: healthy || status === "starting" ? barForeground : Qt.darker(barForeground, 1.6)

  // ---- cursor ------------------------------------------------------------------
  property int cursorIndex: 0
  property bool cursorActive: false
  property real now: Date.now()
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
    if (flick) flick.contentY = 0
    if (mail) mail.poll()
    Qt.callLater(function() { keyCatcher.forceActiveFocus() })
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

  Timer {
    id: signOutTimer
    interval: 4000
    onTriggered: root.confirmSignOut = false
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
    if (mail && message && message.unreadIds && message.unreadIds.length) mail.markRead(message.account, message.unreadIds)
  }

  function markAllRead() {
    if (!mail) return
    var byAccount = {}
    for (var i = 0; i < messages.length; i++) {
      var m = messages[i]
      if (!m.unread) continue
      byAccount[m.account] = (byAccount[m.account] || []).concat(m.unreadIds || [])
    }
    for (var account in byAccount) mail.markRead(account, byAccount[account])
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
    case "m": markRead(selectedMessage()); break
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
    var item = rows.itemAt(cursorIndex)
    if (!item || !flick) return
    Qt.callLater(function() {
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
    tooltipText: root.opened ? "" : Model.tooltip(root.status, root.badgeCount, root.countWindow, root.capped)

    onPressed: function(buttonCode) {
      if (buttonCode === Qt.RightButton) root.openInbox()
      else if (buttonCode === Qt.MiddleButton) { if (root.mail) root.mail.refresh() }
      else root.toggle()
    }

    Grid {
      id: badgeLayout
      anchors.centerIn: parent
      columns: root.vertical ? 1 : 2
      columnSpacing: Style.space(4)
      rowSpacing: Style.space(1)
      horizontalItemAlignment: Grid.AlignHCenter
      verticalItemAlignment: Grid.AlignVCenter

      ProtonMark {
        size: Style.bar.iconCanvas - 1
        color: root.badgeCount > 0 ? root.highlight : root.barIconColor
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
        color: root.highlight
        font.family: root.fontFamily
        font.pixelSize: root.vertical ? Style.font.caption : Style.font.bodySmall
        font.bold: true
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
    contentHeight: panel.fittedContentHeight(column.implicitHeight, Style.space(640))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onMoveRequested: function(dx, dy) { root.moveCursor(dx, dy) }
      onActivateRequested: root.activateCursor()
      onCloseRequested: root.close()
      onDeleteRequested: root.markRead(root.selectedMessage())
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(t) { root.handleKey(t) }

      Flickable {
        id: flick
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
          width: flick.width
          spacing: Style.space(12)

          PanelHero {
            id: hero
            width: parent.width
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

          RowLayout {
            visible: root.signedIn
            width: parent.width
            spacing: Style.space(10)

            ButtonGroup {
              options: [
                { value: "1h", label: "1h", tooltip: "Unread from the last hour (1)" },
                { value: "24h", label: "24h", tooltip: "Unread from the last 24 hours (2)" },
                { value: "7d", label: "7d", tooltip: "Unread from the last 7 days (3)" },
                { value: "all", label: "All", tooltip: "All unread in the inbox (4)" }
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
              text: !root.mail ? "" : (root.mail.accounts.length === 1 ? root.mail.accounts[0].email
                : root.mail.accounts.length + " accounts")
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              elide: Text.ElideLeft
            }
          }

          // Sign-in, expired session, offline and error states.
          CursorSurface {
            id: notice
            readonly property bool needsSignIn: root.status === "signed-out" || root.status === "auth-expired"
            visible: !root.healthy && root.status !== "starting"
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
                color: root.status === "error" ? root.highlight : root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.body
                text: {
                  if (root.status === "signed-out")
                    return "Sign in to see new mail here. Your password is only used on this machine for Proton's SRP handshake; the session is kept in your keyring."
                  if (root.status === "auth-expired") return "Your Proton session ended. Sign in again to keep mail flowing."
                  if (root.status === "offline") return "Offline. Showing the last mail Promarchy saw; it will catch up on its own."
                  return root.mail ? root.mail.message : ""
                }
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
            visible: root.signedIn
            foreground: root.foreground
          }

          PanelSectionHeader {
            visible: root.signedIn
            text: root.messages.length > 0 ? "RECENT" : "RECENT · NOTHING YET"
            foreground: root.foreground
            fontFamily: root.fontFamily
          }

          Column {
            id: rowColumn
            visible: root.signedIn && root.messages.length > 0
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

          PanelSeparator {
            visible: root.signedIn
            foreground: root.foreground
          }

          RowLayout {
            visible: root.signedIn
            width: parent.width
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

            PanelActionButton {
              iconText: "󰀔"
              tooltipText: "Add another Proton account"
              foreground: root.foreground
              fontFamily: root.fontFamily
              onClicked: {
                if (root.mail) root.mail.signIn()
                root.close()
              }
            }

            // Signing back in may need a CAPTCHA, so ask for a second click.
            PanelActionButton {
              iconText: "󰍃"
              tooltipText: root.confirmSignOut ? "Click again to sign out of every account" : "Sign out"
              foreground: root.confirmSignOut ? root.highlight : root.foreground
              fontFamily: root.fontFamily
              onClicked: {
                if (!root.confirmSignOut) {
                  root.confirmSignOut = true
                  signOutTimer.restart()
                  return
                }
                root.confirmSignOut = false
                if (root.mail) root.mail.signOut()
              }
            }
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

    hasCursor: root.cursorActive && root.cursorIndex === rowIndex
    foreground: root.foreground
    implicitHeight: rowContent.implicitHeight + Style.space(14)

    MouseArea {
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

          Text {
            textFormat: Text.PlainText
            visible: root.mail ? root.mail.multiAccount : false
            text: String(row.message.account || "").split("@")[0]
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
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

    // Proton serves brand logos for light backgrounds (as its web app shows
    // them), so a logo gets a light tile; otherwise a themed initial.
    BorderSurface {
      anchors.fill: parent
      radius: Style.cornerRadius > 0 ? width / 2 : 0
      color: logo.status === Image.Ready ? "#f4f4f5" : Style.normalFillFor(root.foreground, Color.accent)
      borderSpec: logo.status === Image.Ready ? Border.none() : Border.controlSpec("normal", root.foreground, Color.accent)
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
      anchors.margins: Style.space(5)
      source: avatar.source
      visible: status === Image.Ready
      asynchronous: true
      cache: true
      smooth: true
      fillMode: Image.PreserveAspectFit
      sourceSize.width: 64
      sourceSize.height: 64
    }
  }
}

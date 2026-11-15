// Keys in the dropdown act only on what the user can see. These run
// BarWidget.qml's own functions (taken from the file as written) against a
// stand-in for the scrolling list, so they test the code that ships.
import { test } from "node:test"
import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import vm from "node:vm"

const source = readFileSync(new URL("../BarWidget.qml", import.meta.url), "utf8")

function load(names, state) {
  const context = vm.createContext({ Qt: { callLater: (f) => f() }, Style: { space: (n) => n }, ...state })
  for (const name of names) {
    const match = source.match(new RegExp(`^  function ${name}\\([^]*?^  }`, "m"))
    assert.ok(match, `BarWidget.qml defines ${name}()`)
    vm.runInContext(match[0], context)
  }
  return context
}

// A list `height` tall over `contentHeight` of content, scrolled by
// flick.contentY; items sit at fixed positions in the content.
function list(height, contentHeight) {
  const flick = { visible: true, height, contentHeight, contentY: 0, contentItem: {} }
  const item = (y, h) => ({
    visible: true,
    height: h,
    mapToItem: (target) => ({ y: target === flick.contentItem ? y : y - flick.contentY }),
  })
  return { flick, item }
}

const ACCOUNTS = ["accountActions", "accountActionItem", "inView", "revealItem", "revealAccounts", "runAccountAction",
  "activateCursor", "signIn"]

function accountsPanel(cursor, confirming) {
  const { flick, item } = list(250, 1500)
  const calls = { signOut: 0, signIn: 0 }
  const panel = load(ACCOUNTS, {
    signedIn: true, demo: false, accountsOpen: true, confirmSignOut: confirming,
    cursorActive: true, cursorZone: "accounts", accountCursor: cursor,
    flick, accountSection: item(800, 500),
    addAccountButton: item(1260, 40), signOutButton: item(1260, 40),
    confirmSignOutButton: item(1260, 40), cancelSignOutButton: item(1260, 40),
    mail: { signOut: () => calls.signOut++, signIn: () => calls.signIn++ },
    close: () => {},
  })
  return { panel, flick, calls }
}

test("Enter on a sign-out confirmation out of sight only brings it into view", () => {
  const { panel, flick, calls } = accountsPanel(0, true)
  panel.activateCursor()
  assert.equal(calls.signOut, 0)
  assert.equal(flick.contentY, 1050)  // the button's bottom at the list's bottom
  panel.activateCursor()
  assert.equal(calls.signOut, 1)
})

test("an account action in sight runs at once", () => {
  const { panel, flick, calls } = accountsPanel(0, false)
  flick.contentY = 1050
  panel.activateCursor()
  assert.equal(calls.signIn, 1)
})

test("moving through a section taller than the list shows the action, not the section's top", () => {
  const { panel, flick } = accountsPanel(1, true)
  panel.revealAccounts()
  assert.equal(flick.contentY, 1050)
  assert.ok(panel.inView(panel.accountActionItem()))
})

test("a marks nothing read once the list is turned off", () => {
  // The old rows stay in memory until the backend's next snapshot.
  const marked = []
  const panel = load(["markAllRead", "handleKey"], {
    listEnabled: false,
    messages: [{ account: "me@proton.me", id: "m1", unread: true, unreadIds: ["m1"], kind: "message" }],
    mail: { markRead: (_account, ids) => marked.push(...ids) },
  })
  panel.handleKey("a")
  assert.deepEqual(marked, [])
  panel.listEnabled = true
  panel.handleKey("a")
  assert.deepEqual(marked, ["m1"])
})

test("m on a row out of sight only brings it into view", () => {
  const { flick, item } = list(300, 1500)
  const marked = []
  const row = item(900, 50)
  const panel = load(
    ["inView", "showsSelectedRow", "revealSelectedRow", "markSelectedRead", "markRead", "selectedMessage", "setCursor",
      "scrollCursorIntoView", "messageKey"],
    {
      cursorActive: true, cursorZone: "rows", cursorIndex: 0, cursorKey: "",
      messages: [{ account: "me@proton.me", id: "m1", unreadIds: ["m1"], kind: "message" }],
      rows: { itemAt: () => row },
      flick,
      mail: { markRead: (_account, ids) => marked.push(...ids) },
    })
  panel.markSelectedRead()
  assert.deepEqual(marked, [])
  assert.ok(panel.inView(row))
  panel.markSelectedRead()
  assert.deepEqual(marked, ["m1"])
})

// Service.qml's ingest(), run the same way: a snapshot that repeats a list
// must not hand the widget a new one, or every row showing it is rebuilt.
test("a repeated snapshot keeps the lists the widget already shows", () => {
  const service = readFileSync(new URL("../Service.qml", import.meta.url), "utf8")
  const match = service.match(/^  function ingest\([^]*?^  }/m)
  assert.ok(match, "Service.qml defines ingest()")
  const context = vm.createContext({
    stableTimer: { running: true, restart() {} }, _restarts: 0,
    _accountsJson: "", _countsJson: "", _messagesJson: "",
    accounts: [], counts: { "1h": 0, "24h": 0, "7d": 0, all: 0 }, messages: [], inboxUrl: "https://mail.proton.me/inbox",
  })
  vm.runInContext(match[0], context)
  const state = {
    type: "state", status: "ok", accounts: [{ email: "me@proton.me", status: "ok" }],
    counts: { "1h": 1, "24h": 2, "7d": 3, all: 4 }, messages: [{ id: "m1", subject: "Hello", unread: true }],
  }
  context.ingest(JSON.stringify(state))
  const first = { accounts: context.accounts, counts: context.counts, messages: context.messages }
  assert.equal(first.messages[0].subject, "Hello")
  assert.equal(first.counts["24h"], 2)

  // Only a count moved (mail aged out of the last hour): the rows stay.
  context.ingest(JSON.stringify({ ...state, counts: { ...state.counts, "1h": 0 } }))
  assert.equal(context.messages, first.messages)
  assert.equal(context.accounts, first.accounts)
  assert.notEqual(context.counts, first.counts)
  assert.equal(context.counts["1h"], 0)

  context.ingest(JSON.stringify({ ...state, messages: [{ id: "m1", subject: "Hello", unread: false }] }))
  assert.notEqual(context.messages, first.messages)
  assert.equal(context.messages[0].unread, false)

  // Not a snapshot, or not JSON: nothing changes.
  const kept = context.messages
  context.ingest("{oops")
  context.ingest(JSON.stringify({ type: "other", messages: [] }))
  assert.equal(context.messages, kept)
  context.ingest(JSON.stringify({ type: "state", status: "signed-out" }))
  assert.deepEqual(Array.from(context.messages), [])
  assert.equal(context.counts["24h"], 2)  // a snapshot without counts keeps the last ones
})

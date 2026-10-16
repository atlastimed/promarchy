// Pure helpers shared by the widget and the tests (node --test tests/model.test.mjs).
// No QML imports here so the logic can run anywhere.

.pragma library

var WINDOWS = ["1h", "24h", "7d", "all"]

function normalizeWindow(value) {
  var key = String(value || "").toLowerCase()
  if (key === "off" || WINDOWS.indexOf(key) !== -1) return key
  return "24h"
}

function countFor(counts, windowKey) {
  if (!counts || windowKey === "off") return 0
  var value = Number(counts[windowKey])
  return isFinite(value) && value > 0 ? Math.floor(value) : 0
}

function badgeText(count) {
  if (!(count > 0)) return ""
  return count > 99 ? "99+" : String(count)
}

function windowPhrase(windowKey) {
  switch (windowKey) {
  case "1h": return "in the last hour"
  case "24h": return "in the last 24 hours"
  case "7d": return "in the last 7 days"
  default: return "in your inbox"
  }
}

function windowShort(windowKey) {
  return windowKey === "all" ? "All" : windowKey
}

function headline(count, windowKey, capped) {
  var key = windowKey === "off" ? "24h" : windowKey
  if (!(count > 0)) return key === "all" ? "Inbox zero" : "Nothing new " + windowPhrase(key)
  var shown = capped && key !== "all" ? count + "+" : String(count)
  return shown + " unread " + windowPhrase(key)
}

function pad2(n) {
  return n < 10 ? "0" + n : String(n)
}

var DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

// Compact time like a mail list: "now", "12m", "14:05", "Yesterday", "Tue", "Oct 3".
function shortTime(epochSeconds, nowMs) {
  var t = Number(epochSeconds) * 1000
  if (!(t > 0)) return ""
  var now = nowMs === undefined ? Date.now() : nowMs
  var diff = Math.max(0, now - t)
  if (diff < 60 * 1000) return "now"
  if (diff < 60 * 60 * 1000) return Math.floor(diff / 60000) + "m"
  var d = new Date(t)
  var today = new Date(now)
  var startOfToday = new Date(today.getFullYear(), today.getMonth(), today.getDate()).getTime()
  if (t >= startOfToday) return pad2(d.getHours()) + ":" + pad2(d.getMinutes())
  if (t >= startOfToday - 86400000) return "Yesterday"
  if (t >= startOfToday - 6 * 86400000) return DAYS[d.getDay()]
  var label = MONTHS[d.getMonth()] + " " + d.getDate()
  return d.getFullYear() === today.getFullYear() ? label : label + " " + d.getFullYear()
}

function initial(name, address) {
  // QML's JS engine lacks Unicode property escapes, so trim common punctuation.
  var source = String(name || address || "?").replace(/^[\s"'`<>()[\]{}.,;:!?*#@_~=+\-]+/, "")
  var first = source.length > 0 ? source.codePointAt(0) : 63
  return String.fromCodePoint(first).toUpperCase()
}

function statusLine(status, message) {
  switch (status) {
  case "signed-out": return "Not signed in"
  case "auth-expired": return "Session expired"
  case "offline": return "Offline"
  case "error": return message || "Something went wrong"
  case "starting": return "Connecting…"
  default: return ""
  }
}

function tooltip(status, count, windowKey, capped) {
  if (status === "signed-out") return "Proton Mail · sign in"
  if (status === "auth-expired") return "Proton Mail · session expired"
  if (status === "offline") return "Proton Mail · offline"
  if (status === "starting") return "Proton Mail · connecting"
  if (status === "error") return "Proton Mail · error"
  if (windowKey === "off") return "Proton Mail"
  return "Proton Mail · " + headline(count, windowKey, capped)
}

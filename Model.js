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
  case "throttled": return "Paused by Proton"
  default: return ""
  }
}

function tooltip(status, count, windowKey, capped, warning) {
  if (status === "signed-out") return "Proton Mail · sign in"
  if (status === "auth-expired") return "Proton Mail · sign in again"
  if (status === "starting") return "Proton Mail · connecting"
  if (status === "error") return "Proton Mail · error"
  var parts = ["Proton Mail"]
  if (status === "offline") parts.push(count > 0 ? "offline, counts from the last sync" : "offline")
  if (status === "throttled") parts.push(count > 0 ? "paused by Proton, counts from the last sync" : "paused by Proton")
  if (windowKey !== "off" && status !== "offline" && status !== "throttled") parts.push(headline(count, windowKey, capped))
  if (status === "partial") parts.push("some accounts not counted")
  if (warning) parts.push("sign-in not yet saved")
  return parts.join(" · ")
}

// The dropdown's notice: guidance for the state first, then what the
// backend reported, which is often the part to act on (a locked keyring, a
// sign-out Proton could not confirm, credentials not yet saved).
var GUIDANCE = {
  "signed-out": "Connect your Proton account to see new mail here. Sign-in opens a terminal; you may need your 2FA code and a quick check in the browser.",
  "auth-expired": "Proton needs you to sign in again.",
  "offline": "Offline. Showing the last mail Promarchy saw; it catches up on its own.",
  "partial": "Some accounts are not counted right now.",
  "throttled": "Proton asked Promarchy to slow down, so these counts are paused for now."
}

function noticeText(status, message, warning) {
  var parts = []
  var guidance = GUIDANCE[status] || ""
  if (guidance) parts.push(guidance)
  var detail = String(message || "").trim()
  if (detail && detail !== guidance) parts.push(detail)
  var extra = String(warning || "").trim()
  if (extra) parts.push(extra)
  return parts.join("\n")
}

// What an empty list means, so "nothing to show" never hides a cause.
function emptyText(status, recentLimit) {
  if (!(recentLimit > 0)) return ""
  if (status === "starting") return "Loading recent mail…"
  if (status === "offline") return "Recent mail is unavailable while offline."
  if (status === "error") return "Recent mail could not be loaded."
  if (status === "throttled") return "Recent mail is paused until Proton's next check."
  if (status === "partial") return "No mail from the accounts that are working."
  return "No mail in your inbox."
}

// A small mark beside the bar icon, so the state shows without opening the
// panel and without relying on colour alone.
function statusMark(status, warning) {
  if (status === "offline") return String.fromCodePoint(0xf092e)  // nf-md-wifi_off, as the network widget
  if (status === "throttled") return String.fromCodePoint(0xf051f)  // nf-md-timer_sand: paused, not broken
  if (status === "auth-expired" || status === "error" || status === "partial" || warning)
    return String.fromCodePoint(0xf0026)  // nf-md-alert
  return ""
}

// ---- readable secondary text ------------------------------------------------
// Colours are Qt colours or plain {r, g, b} objects in 0..1; results are
// plain objects (wrap them in Qt.rgba).

function _linear(c) {
  return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4)
}

function luminance(c) {
  return 0.2126 * _linear(c.r) + 0.7152 * _linear(c.g) + 0.0722 * _linear(c.b)
}

function contrast(a, b) {
  var x = luminance(a)
  var y = luminance(b)
  return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05)
}

function mix(a, b, t) {
  return { r: a.r + (b.r - a.r) * t, g: a.g + (b.g - a.g) * t, b: a.b + (b.b - a.b) * t }
}

// The foreground eased toward the background as far as `ease` allows while
// keeping `floor` contrast. Unlike Qt.darker, this lowers emphasis on light
// themes too, and never pushes text below a readable floor; a theme pair that
// is already weak keeps its foreground rather than being weakened further.
function secondary(fg, bg, floor, ease) {
  var minimum = floor === undefined ? 4.5 : floor
  var most = ease === undefined ? 0.4 : ease
  if (contrast(fg, bg) <= minimum) return mix(fg, bg, 0)
  if (contrast(mix(fg, bg, most), bg) >= minimum) return mix(fg, bg, most)
  var lo = 0
  var hi = most
  for (var i = 0; i < 24; i++) {
    var mid = (lo + hi) / 2
    if (contrast(mix(fg, bg, mid), bg) >= minimum) lo = mid
    else hi = mid
  }
  return mix(fg, bg, lo)
}

// Middle-elide for monospace labels, where width is a character count.
function middleElide(text, maxChars) {
  var value = String(text || "")
  if (!(maxChars > 3) || value.length <= maxChars) return value
  var keep = maxChars - 1
  var head = Math.ceil(keep / 2)
  return value.slice(0, head) + "…" + value.slice(value.length - (keep - head))
}

// ---- accounts ------------------------------------------------------------------

function accountState(status) {
  switch (status) {
  case "ok": return "Connected"
  case "starting": return "Connecting…"
  case "auth-expired": return "Sign in again"
  case "offline": return "Offline"
  case "error": return "Not syncing"
  case "throttled": return "Paused"
  default: return ""
  }
}

function signOutQuestion(count) {
  return count > 1 ? "Sign out of all " + count + " accounts?" : "Sign out of this account?"
}

// Wrap plain text for a monospace tooltip, which does not wrap on its own:
// at spaces where possible, hard-breaking anything longer than a line.
function wrapLines(text, maxChars) {
  var width = maxChars > 8 ? maxChars : 8
  var out = []
  var paragraphs = String(text || "").split("\n")
  for (var p = 0; p < paragraphs.length; p++) {
    var words = paragraphs[p].split(" ")
    var line = ""
    for (var w = 0; w < words.length; w++) {
      var word = words[w]
      while (word.length > width) {
        if (line) { out.push(line); line = "" }
        out.push(word.slice(0, width))
        word = word.slice(width)
      }
      if (!line) line = word
      else if (line.length + 1 + word.length <= width) line += " " + word
      else { out.push(line); line = word }
    }
    out.push(line)
  }
  return out.join("\n")
}

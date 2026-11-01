// Tests for Model.js, the pure helpers behind the bar badge and dropdown.
// Run with: node --test tests/
import { test } from "node:test"
import assert from "node:assert/strict"
import { readFileSync } from "node:fs"

const source = readFileSync(new URL("../Model.js", import.meta.url), "utf8").replace(/^\.pragma library$/m, "")
const M = new Function(
  `${source}; return { WINDOWS, normalizeWindow, countFor, badgeText, headline, shortTime, initial, tooltip, statusLine, noticeText, emptyText, statusMark, secondary, contrast, middleElide, accountState, signOutQuestion, wrapLines }`
)()

test("window setting falls back to 24h", () => {
  assert.equal(M.normalizeWindow("7D"), "7d")
  assert.equal(M.normalizeWindow("off"), "off")
  assert.equal(M.normalizeWindow("week"), "24h")
  assert.equal(M.normalizeWindow(undefined), "24h")
})

test("badge count reads the configured window", () => {
  const counts = { "1h": 2, "24h": 9, "7d": 40, all: 500 }
  assert.equal(M.countFor(counts, "1h"), 2)
  assert.equal(M.countFor(counts, "all"), 500)
  assert.equal(M.countFor(counts, "off"), 0)
  assert.equal(M.countFor({}, "24h"), 0)
  assert.equal(M.countFor(null, "24h"), 0)
})

test("badge text caps at 99+", () => {
  assert.equal(M.badgeText(0), "")
  assert.equal(M.badgeText(5), "5")
  assert.equal(M.badgeText(99), "99")
  assert.equal(M.badgeText(100), "99+")
})

test("headline wording", () => {
  assert.equal(M.headline(3, "24h", false), "3 unread in the last 24 hours")
  assert.equal(M.headline(750, "7d", true), "750+ unread in the last 7 days")
  assert.equal(M.headline(0, "1h", false), "Nothing new in the last hour")
  assert.equal(M.headline(0, "all", false), "Inbox zero")
  assert.equal(M.headline(2, "off", false), "2 unread in the last 24 hours")
})

test("short times read like a mail list", () => {
  const now = new Date(2026, 9, 16, 15, 0, 0).getTime() // Friday
  const s = (secondsAgo) => M.shortTime(now / 1000 - secondsAgo, now)
  assert.equal(s(20), "now")
  assert.equal(s(12 * 60), "12m")
  assert.equal(s(3 * 3600), "12:00")
  assert.equal(s(20 * 3600), "Yesterday")
  assert.equal(s(3 * 86400), "Tue")
  assert.equal(s(40 * 86400), "Sep 6")
  assert.equal(s(400 * 86400), "Sep 11 2025")
  assert.equal(M.shortTime(0, now), "")
})

test("avatar initials skip punctuation", () => {
  assert.equal(M.initial('"GitHub"', ""), "G")
  assert.equal(M.initial("", "noreply@example.com"), "N")
  assert.equal(M.initial("éclair", ""), "É")
  assert.equal(M.initial("", ""), "?")
})

test("tooltip reflects status first", () => {
  assert.equal(M.tooltip("signed-out", 0, "24h", false), "Proton Mail · sign in")
  assert.equal(M.tooltip("ok", 4, "1h", false), "Proton Mail · 4 unread in the last hour")
  assert.equal(M.tooltip("ok", 4, "off", false), "Proton Mail")
})

const hex = (h) => ({ r: parseInt(h.slice(1, 3), 16) / 255, g: parseInt(h.slice(3, 5), 16) / 255, b: parseInt(h.slice(5, 7), 16) / 255 })

test("the notice keeps what the backend said (review: dropdown hid backend warnings)", () => {
  // Offline while renewed tokens are only in memory.
  const offline = M.noticeText("offline", "Cannot reach Proton (offline?)", "me@proton.me: renewed sign-in not yet saved to the keyring")
  assert.match(offline, /^Offline\./)
  assert.match(offline, /not yet saved to the keyring/)
  // A sign-out Proton could not confirm, after which no account is left.
  const signedOut = M.noticeText("signed-out", "me@proton.me: Proton no longer accepts its saved credentials… Check Proton's Settings → Security → Session management.", "")
  assert.match(signedOut, /^Connect your Proton account/)
  assert.match(signedOut, /Session management/)
  // An account whose keyring entry cannot be read.
  const locked = M.noticeText("auth-expired", "Could not find the saved session for me@proton.me. If your keyring is locked, unlock it; otherwise sign in again.", "")
  assert.match(locked, /unlock it/)
  assert.equal(M.noticeText("ok", "", ""), "")
  assert.equal(M.noticeText("starting", "Connecting to Proton…", ""), "")
})

test("empty lists say why", () => {
  assert.equal(M.emptyText("ok", 8), "No mail in your inbox.")
  assert.equal(M.emptyText("starting", 8), "Loading recent mail…")
  assert.equal(M.emptyText("offline", 8), "Recent mail is unavailable while offline.")
  assert.equal(M.emptyText("ok", 0), "")
})

test("the bar shows state without relying on colour", () => {
  assert.equal(M.statusMark("ok", ""), "")
  assert.equal(M.statusMark("offline", ""), String.fromCodePoint(0xf092e))
  for (const status of ["auth-expired", "error", "partial"]) assert.equal(M.statusMark(status, ""), String.fromCodePoint(0xf0026))
  assert.equal(M.statusMark("ok", "not yet saved"), String.fromCodePoint(0xf0026))
  assert.equal(M.statusMark("signed-out", ""), "")
  assert.equal(M.tooltip("offline", 3, "24h", false, ""), "Proton Mail · offline, counts from the last sync")
  assert.equal(M.tooltip("partial", 2, "1h", false, ""), "Proton Mail · 2 unread in the last hour · some accounts not counted")
})

// Stock Omarchy 4.0.6 popup pairs (foreground on background).
const STOCK = {
  "tokyo-night": ["#a9b1d6", "#1a1b26"],
  everforest: ["#d3c6aa", "#2d353b"],
  gruvbox: ["#d4be98", "#282828"],
  "catppuccin-latte": ["#4c4f69", "#eff1f5"],
  white: ["#000000", "#ffffff"],
}

test("secondary text stays readable and subordinate in every stock pair", () => {
  for (const [name, [fg, bg]] of Object.entries(STOCK)) {
    const dim = M.secondary(hex(fg), hex(bg))
    const main = M.contrast(hex(fg), hex(bg))
    const ratio = M.contrast(dim, hex(bg))
    assert.ok(ratio >= 4.5 - 1e-9, `${name}: ${ratio.toFixed(2)}`)
    assert.ok(ratio < main, `${name}: secondary must stay below the main text`)
  }
  // A weak theme pair is never weakened further.
  const weak = M.secondary(hex("#777777"), hex("#555555"))
  assert.equal(M.contrast(weak, hex("#555555")).toFixed(4), M.contrast(hex("#777777"), hex("#555555")).toFixed(4))
})

test("long addresses are elided in the middle", () => {
  assert.equal(M.middleElide("you@example.com", 20), "you@example.com")
  assert.equal(M.middleElide("ada.lovelace.engine@example.org", 18), "ada.lovel…mple.org")
})

test("account rows and sign-out say what will happen", () => {
  assert.equal(M.accountState("ok"), "Connected")
  assert.equal(M.accountState("auth-expired"), "Sign in again")
  assert.equal(M.signOutQuestion(1), "Sign out of this account?")
  assert.equal(M.signOutQuestion(3), "Sign out of all 3 accounts?")
})

test("tooltips wrap to the panel instead of running off it", () => {
  const wrapped = M.wrapLines("Professor Dr. Maximilian Theodor von Habsburg-Lothringen <max.habsburg@university-archive.example>", 30)
  for (const line of wrapped.split("\n")) assert.ok(line.length <= 30, line)
  assert.equal(wrapped.replace(/\n/g, " ").replace(/ +/g, " ").replace(/xample>$/, "xample>").length > 0, true)
  assert.equal(M.wrapLines("short", 30), "short")
  assert.equal(M.wrapLines("two\nlines", 30), "two\nlines")
  assert.equal(M.wrapLines("abcdefghij", 8), "abcdefgh\nij")
})

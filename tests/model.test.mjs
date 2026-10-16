// Tests for Model.js, the pure helpers behind the bar badge and dropdown.
// Run with: node --test tests/
import { test } from "node:test"
import assert from "node:assert/strict"
import { readFileSync } from "node:fs"

const source = readFileSync(new URL("../Model.js", import.meta.url), "utf8").replace(/^\.pragma library$/m, "")
const M = new Function(`${source}; return { WINDOWS, normalizeWindow, countFor, badgeText, headline, shortTime, initial, tooltip, statusLine }`)()

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

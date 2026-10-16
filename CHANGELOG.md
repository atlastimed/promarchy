# Changelog

## 0.1.0 (unreleased)

First release.

- Bar badge with unread mail from the last hour, 24 hours or 7 days (or all, or off), switchable from the dropdown.
- Dropdown of recent mail with sender logos, grouped threads, mark-as-read and keyboard navigation.
- Native Omarchy notifications for new mail; clicking opens the message.
- Inbox category filter and multiple accounts.
- Own Proton session via SRP (Proton's python-proton-core), TOTP 2FA and CAPTCHA verification; tokens in GNOME Keyring.
- Event-stream polling with locally aging window counts; stdlib HTTP client (~30 MB resident).
- `promarchyctl` CLI: login, logout, accounts, status, notify-test.

Hardening from the pre-release reviews:

- Diagnostics log only error types and code locations, never messages that could quote a token; stored tokens are shape-checked; the CAPTCHA link is no longer printed.
- Sign-out ends the session on Proton's side before forgetting it, reports failures, and never removes a newer sign-in; Ctrl+C during sign-in revokes the half-created session.
- Gzip responses are decompressed with a bound on the expanded size; logo downloads are capped and unsafe SVGs refused; expired logos are pruned.
- Notifications cannot skip mail that shares a timestamp, arrives slightly out of order, or comes in bursts larger than the detail window.
- Proton's Retry-After and error backoff are honoured by menu polls and logo lookups.
- Partial failures across accounts are shown instead of hidden; the dropdown lists conversations with exact counts.
- Private files are written via exclusive temp files; state directories are verified and tightened.
- Demo mode no longer busy-wakes; daemon restart backoff resets only after stable uptime; the QML lint gate fails closed.
- Sign-out re-checks the stored session before trusting a rejected refresh token (another process may have rotated it), keeps going if fresh tokens cannot be saved, and changes nothing when the saved session cannot be read.
- Sender logos are raster-only (requested as PNG, checked by file signature); SVG is never stored or shown.
- Conversation rows use the Inbox's own context when a thread spans folders.
- A Proton account is identified by its UserID, so signing in through another of its addresses replaces the old entry instead of counting the mailbox twice.
- Proton's Retry-After also holds off settings changes, mark-as-read and sign-out; unexpected keyring-helper exits are reported as errors.
- Renewing a session and saving the new tokens happen under one cross-process lock; a process that waited adopts the tokens another one just saved instead of spending its own, and sign-out reads the session under that lock, so it never mistakes a renewal in progress for a dead session.
- Signing in through another address saves the new session before removing the old entry, so a failed save loses nothing.
- A rate-limited sign-out keeps Proton's Retry-After.
- `scripts/proton_probe.py` checks parsers and counts against the live API without exposing the session; its `--roundtrip` only touches single-message conversations and always restores what it sent.

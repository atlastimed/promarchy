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
- Each session has one owner: the bar's background process while it runs, otherwise the CLI holding the same lock. `promarchyctl logout` and `promarchyctl login` ask the running bar over a private socket, so the bar both signs out and takes over new sign-ins, ending a replaced session with its own current tokens. Nothing else renews, replaces or ends a session, so no process has to guess whose tokens are newer; when Proton rejects the credentials, or the keyring refuses to forget an account, sign-out says so rather than claiming success. Socket requests are validated in full (a malformed account never means "all"), always answered, and never run late after the caller was told the bar was busy. Sign-out covers every listed account, including ones the bar could not load.
- Signing in through another address saves the new session before removing the old entry, so a failed save loses nothing; if the old keyring item cannot be deleted, the new sign-in still takes over and the leftover is reported.
- A rate-limited sign-out keeps Proton's Retry-After.
- A new sign-in is revoked only when its owner certainly did not save it; if the bar's answer is lost, the session is kept and `promarchyctl login` says it could not confirm the save. The bar switches to an adopted session at once, even if the keyring cannot be read back. Addresses are compared case-insensitively, session fields must have their exact types, and a session merely re-keyed under another address is never revoked. A replaced session whose credentials Proton rejects is reported as unconfirmed.
- Keyring writes report whether nothing changed, the session was saved (with any leftovers), or the outcome is unknown; only "nothing changed" lets a sign-in revoke its new session. The bar ends every session a sign-in replaces, including ones it holds but cannot read back; renewed tokens are never written over an unreadable entry, and tokens kept only in memory are shown as a warning. A cancelled sign-in that cannot be ended on Proton's side says so. Known limits are documented in the README.
- `scripts/proton_probe.py` checks parsers and counts against the live API without exposing the session; its `--roundtrip` only touches single-message conversations, always attempts the restore (also after Ctrl+C) and exits non-zero if it cannot confirm it.

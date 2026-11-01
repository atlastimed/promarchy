# Changelog

## 0.2.0 (2026-11-01)

Design refinements from an Omarchy design review, and fixes from several code reviews.

Interface:

- Secondary text (read subjects, times, labels) is derived against the popup's own background with a 4.5:1 contrast floor: readable in all 22 stock themes (8 fell below it before) and quieter than the main text on light themes (4 had it brighter).
- The bar shows state without opening the dropdown: a small mark beside the envelope when offline, while Proton's rate limit pauses checks (an hourglass), when a session needs signing in again, when some accounts are not counted, or when a renewed sign-in is not yet saved, and a count from an earlier sync, a paused one, or one still waiting for other accounts to connect steps back from the attention colour.
- The dropdown always shows what the backend reported (a locked keyring, a sign-out Proton could not confirm, credentials not yet saved) next to short guidance. Signing in starts with what to do rather than how it works.
- "Unread from" labels the count controls and "Recent inbox" the list; a hidden badge reads "Badge off". Empty lists say why (loading, offline, paused, empty inbox), and a list turned off is hidden.
- The header and footer stay in place while a long list scrolls; notices scroll with the list, so a long one never pushes a confirmation out of reach, and new feedback scrolls into view. Hovering a row shows the full sender, address and subject; account labels on rows stay short.
- *Add account* and *Sign out* moved into an accounts section that lists each account's state, also reachable from the keyboard (<kbd>s</kbd>, then arrows and <kbd>Enter</kbd>). Signing out asks in words, with the cursor on Cancel, instead of a second click within four seconds.
- Keys act only on what is in sight: with no row selected, or the selected row or account action scrolled out of view (or pushed down by a new notice), <kbd>Enter</kbd>, <kbd>m</kbd> or <kbd>x</kbd> first brings it into view; with the recent list turned off, <kbd>a</kbd> marks nothing read.
- Logo tiles follow the theme's corner radius, share the control border, and use plain white so Proton's opaque logos sit flush.
- Colours are read through the qualified `Commons.Color`, as the shell's own components do.
- Demo mode can show other states (`offline`, `throttled`, `expired`, `partial`, `warning`, `signed-out`, `empty`, `long`, `multi`), never signs in or acts on real accounts, and says what it did not do.
- A new marketplace cover (1600×900, sample data only), and README screenshots captured natively at 2× scale.

Fixes:

- A sign-in whose keyring write landed before the helper was interrupted is now registered, rather than reported as signed in while invisible to the bar and to `logout --all`. An unknown outcome is retried once with the session's owner before it is reported as unconfirmed.
- That retry can no longer overwrite a newer sign-in or bring back a session that was signed out meanwhile: replaced and signed-out sessions are remembered (by the running bar even when its state directory cannot be written or locked, and on disk as soon as it can be) and never saved again, a sign-out Proton confirmed counts even if the keyring kept the entry, and a retry goes ahead only on positive evidence seen by the owner that handled its first attempt and still owns the sessions (the CLI keeps ownership from a local first attempt to its retry): its own session saved with nothing around it but what it meant to replace, or nothing changed at all. A keyring that answers "nothing saved", a sign-in it did not expect, a session the bar has seen fail or has signed out, or a bar that restarted in between leave the sign-in kept and reported as unconfirmed, so a later sign-in or sign-out is never undone. A retry of a session that is already saved keeps the tokens renewed since, rather than writing its original ones back. A failure to record what a saved sign-in replaced no longer passes for "nothing saved", which had the new sign-in revoked.
- During a sign-in only the keyring's own refusal counts as "nothing saved": any other unexpected error keeps the session, unconfirmed, and a diagnostic that cannot be written (stderr closed, its reader gone) no longer changes what it describes.
- Any keyring-helper failure while saving counts as "may have saved" and is settled by reading the keyring back, never by assuming nothing changed. A damaged keyring entry reads as no session instead of stopping the bar.
- In Proton's single-message view the dropdown lists messages, so every link opens; view-mode changes are picked up while running and on refresh, and the links already shown follow at once. A failed read of the mail settings, or a re-sync that Proton's Refresh event asked for and that failed, is retried until it succeeds, and the settings are read again whenever the event cursor has to start afresh. Until Proton's view mode is known, or while it is being read again, rows link to the inbox rather than guess.
- A throttled session renewal (429 with Retry-After) triggered by the settings request or a logo request holds the whole account until Proton's deadline, and the account shows as paused rather than failed. Counts from an account that has not synced are not presented as current.
- Logos still pause on their own limit and keep loading on idle polls instead of stopping after the first batch. Logo requests have their own short timeout and back off when Proton's logo service fails, so a slow logo never holds up counts, and a server error is no longer remembered as "no logo".
- Store changes are tracked with a counter instead of a file timestamp, so two quick changes are never seen as one on filesystems with coarse timestamps.
- Malformed sign-in replies fail with a clear error instead of a traceback, and tokens from sign-in are shape-checked like every other.
- A refresh refused as wrong credentials (code 8002) means "sign in again", whatever its HTTP status.
- `promarchyctl status` asks the running bar whether it is alive and trusts only the snapshot that bar published last, so neither a stopped daemon's, a predecessor's, nor one left behind by a failed write is presented as current. With `--window` it prints a number only when the counts are current and complete, and otherwise says why and exits 1; `promarchyctl logout` with several accounts asks which one (or `--all`).
- Credentials in an `http://` proxy URL in `https_proxy` are sent to the proxy on CONNECT. `https://` and SOCKS proxy URLs, to which they would have gone in the clear, are refused with an error.
- The unsaved-credentials warning has its own field, so `partial` means only that some accounts are not counted. Mark-as-read ignores IDs that are not a list; an evicted duplicate account forgets its notification watermark.
- Signing in with a bare username fails if Proton rejects the session while its address is looked up, instead of going ahead under the username and replacing the account's working session. A logo request Proton still rejects after renewal counts as a rejected session, not a missing logo.
- `promarchyctl logout` asks the running bar before trusting the account list on disk, so a sign-in the list could not record is signed out too, and `status --window` also refuses a count that is only a lower bound (more unread mail than Promarchy pages through).
- `scripts/proton_probe.py --roundtrip` marks and restores exactly one message, so a reply arriving during the run is never touched.

## 0.1.0 (2026-10-16)

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
- A keyring helper that ends without stating a refusal (killed by a signal, say) counts as "may have saved", never as "nothing changed". Keyring lookups prefer Promarchy's own item, so a leftover from a pre-release can never shadow the current session. The unsaved-tokens warning stays visible while offline.
- `scripts/proton_probe.py` checks parsers and counts against the live API without exposing the session; its `--roundtrip` only touches single-message conversations, always attempts the restore (also after Ctrl+C) and exits non-zero if it cannot confirm it.

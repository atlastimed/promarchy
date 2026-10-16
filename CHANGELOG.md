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

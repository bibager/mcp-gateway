# Monarch Token Extractor

A tiny Chrome extension that captures your active Monarch Money session token
in one click. Reads from `localStorage`, `sessionStorage`, intercepted
`Authorization` headers, and cookies — surfaces whichever it finds first.

## Why this exists

Monarch put `api.monarch.com` behind Cloudflare's WAF, which blocks the
programmatic email/password login flow that the `monarchmoney` Python library
uses. The library reports `HTTP 404` because Cloudflare returns `403 / "You
Shall Not Pass"` on the login endpoint when it sees the SDK's user-agent.

The fix: skip the broken login and inject a token captured from a real
browser session. The gateway's monarch service already supports this via the
`MONARCH_TOKEN` env var — it just needs a fresh token every ~30 days.

This extension makes capture a one-click operation instead of a DevTools dive.

## Install (developer mode, local)

1. Open Chrome → `chrome://extensions`
2. Toggle **Developer mode** on (top right)
3. Click **Load unpacked**
4. Select the `tools/monarch-token-extractor/` folder in this repo
5. Pin the extension to the toolbar (puzzle-piece icon → pin "Monarch Token Extractor")

## Use

1. Go to https://app.monarch.com and log in
2. Click the **Monarch Token Extractor** toolbar icon
3. The popup shows the captured token. Click **Copy token**
4. Paste into Claude (or directly into DigitalOcean → Settings → `MONARCH_TOKEN`)

If the popup shows "No auth token found":
- Click anywhere in the Monarch UI to trigger an API request, then click **Re-scan**
- Or reload the Monarch tab and try again

## What it reads

In order, until one succeeds:
1. `localStorage` — Monarch's SPA typically persists session state here
2. `sessionStorage` — fallback location
3. `Authorization` header from intercepted API calls to `api.monarch.com` (via background.js)
4. `cookies` for `monarch.com` matching `token`/`auth`/`session` patterns

## Files

| File | Purpose |
|---|---|
| `manifest.json` | Extension declaration (Manifest V3) |
| `popup.html` | UI |
| `popup.js` | Logic — scans tab + copies to clipboard |
| `background.js` | Service worker — passively records the Authorization header from Monarch API requests |

## Permissions explained

- `cookies` — read cookies for monarch.com domains
- `scripting` + `activeTab` — run a one-line script in the active tab to read localStorage/sessionStorage
- `webRequest` + host permissions — observe outgoing requests' Authorization header (read-only; doesn't modify)
- `storage` — reserved for future use (not currently used)

No data leaves your machine. The extension only puts the token on your clipboard
when you click Copy.

## Trust model

Browser extensions are powerful. Before installing, you can inspect every file
in this folder — it's ~200 lines total and there's no obfuscation, no remote
code load, no analytics. Read `popup.js`, `background.js`, and `manifest.json`
top-to-bottom in a couple minutes.

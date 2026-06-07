# Monarch Token Extractor v2

Chrome extension that captures your active Monarch session's Authorization
header — and everything else that might be useful — in one click. No DevTools
required.

## Why v2

v1 relied on `chrome.webRequest` (the extension's service worker passively
observing network traffic). Chrome unloads service workers aggressively, so
v1 missed every request that happened before the popup was opened — falling
back to localStorage values that turned out to be session IDs, not auth
tokens.

v2 adds a **content script injected into the Monarch page itself** that
monkey-patches the page's own `fetch` and `XMLHttpRequest`. Every API call
is recorded as it's made, regardless of when the popup opens or whether the
service worker is alive.

## Install

1. Open Chrome → `chrome://extensions`
2. Toggle **Developer mode** on (top right)
3. Click **Load unpacked**
4. Select the `tools/monarch-token-extractor/` folder
5. Pin the extension to the toolbar

If upgrading from v1: click **Reload** on the extension card so v2 takes effect.

## Use

1. Visit https://app.monarch.com and log in (or reload if already logged in —
   the content script needs to attach **before** the page makes its first
   request, so a fresh page load is ideal)
2. Click anything in the UI — a transaction, a filter, switch months — to
   trigger an API call
3. Click the extension icon

The popup shows:
- The most recent captured `Authorization` header
- Source (`fetch` / `xhr` / `webrequest`)
- Prefix (`Bearer` / `Token` / none) — important, the gateway needs the right format
- The raw token value (prefix stripped)
- A full history of every header captured this session
- A preview of localStorage / sessionStorage / cookies

Two copy buttons:
- **Copy auth header value** — just the raw token (no prefix), ready to paste as a `MONARCH_TOKEN` env var
- **Copy full diagnostic JSON** — everything bundled into one JSON paste for debugging

## Files

| File | Purpose |
|---|---|
| `manifest.json` | Manifest V3 declaration |
| `content_main.js` | Runs in the page's JS context (`world: "MAIN"`). Monkey-patches `fetch` and `XMLHttpRequest`. Forwards captures to `content_bridge.js` via `window.postMessage`. |
| `content_bridge.js` | Runs in the extension's isolated world. Listens for postMessage events from the page and forwards them to the background worker via `chrome.runtime.sendMessage`. |
| `background.js` | Service worker. Persists captures to `chrome.storage.local`. Also runs a redundant `chrome.webRequest` listener as a safety net. |
| `popup.html` + `popup.js` | UI. Reads from storage, renders captures, copies to clipboard. |

## Architecture

```
Monarch page                    extension
─────────────                  ─────────────
            fetch()/XHR
            captured by
                ▼
        content_main.js  ──postMessage──▶  content_bridge.js
                                                     │
                                                     │  chrome.runtime.sendMessage
                                                     ▼
                                                background.js
                                                     │
                                                     │  chrome.storage.local.set
                                                     ▼
                                                  storage
                                                     │
                                                     │  chrome.storage.local.get
                                                     ▼
                                                  popup.js
                                                     │
                                                     │  clipboard.writeText
                                                     ▼
                                                   user
```

The redundant `chrome.webRequest` listener in `background.js` is a safety net
for requests fired before our content script attaches (e.g., very early page
load) or made from contexts the page patches can't reach (web workers).

## Permissions

| Permission | Why |
|---|---|
| `cookies` | Read HttpOnly cookies for monarch.com (visible only via chrome.cookies API, not document.cookie) |
| `scripting` + `activeTab` | Run a one-line script in the active tab to read localStorage/sessionStorage from the popup |
| `webRequest` + host permissions | Safety-net Authorization header observation |
| `storage` | Persist captured headers across service-worker restarts |
| `tabs` | Query the active tab URL |

## Trust model

~450 lines total across 5 files. No remote code load, no analytics, no
network calls of our own. Inspect every file in 10 minutes before installing.
Tokens stay on your machine — only go where you paste them.

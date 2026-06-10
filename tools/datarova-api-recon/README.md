# Datarova API Recon

A Chrome extension to capture Datarova's internal API calls so we can build
an MCP wrapper. Same pattern as `monarch-token-extractor` but tuned for API
reverse-engineering rather than just token capture.

## Why

Datarova has no public API. To build an MCP for it, we need to figure out
what endpoints their web app calls when you use the rank tracker. This
extension monkey-patches `fetch` + `XHR` in the page so every API call gets
recorded — URL, method, headers (esp. `Authorization`), and request body.

## Install

1. `chrome://extensions` → Developer mode → **Load unpacked**
2. Select `tools/datarova-api-recon/`
3. Pin it to the toolbar

## Use (the goal: produce a fat diagnostic JSON)

1. Visit https://app.datarova.com and log in
2. **Reload the page** after install (content script needs to attach before the app runs)
3. **Click around in the rank tracker** — this is where the value is. Hit:
   - Open the keyword list
   - Click into a tracked keyword to see rank history
   - Switch ASINs / brands
   - Apply a filter
   - Export / refresh
4. Click the extension icon — you'll see a count of total captures + unique endpoints
5. Click **Copy full diagnostic JSON** → paste in Claude

The JSON includes:
- Every captured API call (URL + method + headers + request body, truncated)
- Endpoint summary (deduped by URL + method, with call counts)
- localStorage / sessionStorage / cookies (including HttpOnly)
- Page URL + user-agent

That's enough for Claude to:
- Map the API surface
- Identify the auth model
- Design MCP tool signatures
- Implement the gateway wrapper

## Architecture

```
Datarova page ───fetch/XHR───▶ content_main.js (MAIN world, monkey-patches)
                                       │ window.postMessage
                                       ▼
                                content_bridge.js (isolated world)
                                       │ chrome.runtime.sendMessage
                                       ▼
                                background.js (service worker)
                                       │ chrome.storage.local
                                       ▼
                                   storage
                                       │
                                       ▼
                                  popup.js
```

`background.js` also runs a redundant `chrome.webRequest` listener as a
safety net for requests fired before content script attaches.

## Trust model

~400 lines of plain JS across 5 files. No remote code, no analytics, no
network calls of our own. Auditable in 10 minutes. Data stays on your
machine — only goes where you paste it.

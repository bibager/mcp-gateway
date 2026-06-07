// Background service worker: collects captured Authorization headers from
// two sources — (a) the in-page fetch/XHR monkey-patches (via content_main →
// content_bridge → chrome.runtime.sendMessage) and (b) chrome.webRequest as
// a redundant safety net. Persists everything to chrome.storage.local so the
// popup gets the data even when the worker has been unloaded.

const MAX_RECORDS = 40;

async function appendCapture(entry) {
  if (!entry.authHeader || entry.authHeader.length < 20) return;
  const { captured = [] } = await chrome.storage.local.get(["captured"]);
  // Deduplicate consecutive identical headers (Monarch retries with the same
  // auth, no need to record 50 copies)
  const last = captured[captured.length - 1];
  if (last && last.authHeader === entry.authHeader && (entry.capturedAt - last.capturedAt) < 60_000) {
    last.url = entry.url || last.url;
    last.via = `${last.via},${entry.via}`;
    last.count = (last.count || 1) + 1;
    last.lastSeenAt = entry.capturedAt;
  } else {
    captured.push({ ...entry, count: 1, lastSeenAt: entry.capturedAt });
  }
  // Keep only the most recent N entries
  while (captured.length > MAX_RECORDS) captured.shift();
  await chrome.storage.local.set({ captured });
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg && msg.type === "auth-captured") {
    appendCapture({
      url: msg.url || "",
      authHeader: msg.authHeader,
      via: msg.via || "unknown",
      capturedAt: Date.now(),
    });
  }
  if (msg && msg.type === "get-captures") {
    chrome.storage.local.get(["captured"]).then((data) => {
      sendResponse(data.captured || []);
    });
    return true; // async response
  }
  if (msg && msg.type === "clear-captures") {
    chrome.storage.local.set({ captured: [] }).then(() => sendResponse({ ok: true }));
    return true;
  }
});

// --- redundant webRequest listener (catches Authorization headers even if the
// page's fetch/XHR somehow slip past — e.g. requests fired before our content
// script attached, or made from a Web Worker the patches can't reach) -------

chrome.webRequest.onSendHeaders.addListener(
  (details) => {
    if (!details.requestHeaders) return;
    for (const h of details.requestHeaders) {
      if (h.name.toLowerCase() === "authorization" && h.value && h.value.length > 20) {
        appendCapture({
          url: details.url,
          authHeader: h.value,
          via: "webrequest",
          capturedAt: Date.now(),
        });
        return;
      }
    }
  },
  {
    urls: [
      "https://api.monarch.com/*",
      "https://api.monarchmoney.com/*",
    ],
  },
  ["requestHeaders"]
);

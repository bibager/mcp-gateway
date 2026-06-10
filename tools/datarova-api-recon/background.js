// Background service worker: persists captures to chrome.storage.local.

const MAX_RECORDS = 200;

async function appendCapture(entry) {
  // Skip pings + non-Datarova internal navigations
  if (entry.via === "ping" || !entry.url) return;

  const { captures = [] } = await chrome.storage.local.get(["captures"]);

  // Dedupe consecutive identical URLs (SPAs often retry the same query)
  const last = captures[captures.length - 1];
  if (last && last.url === entry.url && last.method === entry.method
      && (entry.capturedAt - last.capturedAt) < 10_000) {
    last.count = (last.count || 1) + 1;
    last.lastSeenAt = entry.capturedAt;
  } else {
    captures.push({ ...entry, count: 1, lastSeenAt: entry.capturedAt });
  }
  while (captures.length > MAX_RECORDS) captures.shift();
  await chrome.storage.local.set({ captures });
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg && msg.type === "request-captured") {
    appendCapture({
      url: msg.url || "",
      method: msg.method || "?",
      authHeader: msg.authHeader,
      requestHeaders: msg.requestHeaders || {},
      requestBody: msg.requestBody,
      via: msg.via || "unknown",
      capturedAt: Date.now(),
    });
  }
  if (msg && msg.type === "get-captures") {
    chrome.storage.local.get(["captures"]).then((d) => sendResponse(d.captures || []));
    return true;
  }
  if (msg && msg.type === "clear-captures") {
    chrome.storage.local.set({ captures: [] }).then(() => sendResponse({ ok: true }));
    return true;
  }
});

// Redundant webRequest safety net for anything the page patches missed
chrome.webRequest.onSendHeaders.addListener(
  (details) => {
    if (!details.requestHeaders) return;
    let auth = null;
    const hdrs = {};
    for (const h of details.requestHeaders) {
      hdrs[h.name.toLowerCase()] = h.value;
      if (h.name.toLowerCase() === "authorization") auth = h.value;
    }
    appendCapture({
      url: details.url,
      method: details.method,
      authHeader: auth,
      requestHeaders: hdrs,
      requestBody: null,
      via: "webrequest",
      capturedAt: Date.now(),
    });
  },
  { urls: ["https://*.datarova.com/*"] },
  ["requestHeaders"]
);

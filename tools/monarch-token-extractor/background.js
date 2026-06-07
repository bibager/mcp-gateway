// Background service worker: passively watches outbound GraphQL requests to
// api.monarch.com and remembers the most recent Authorization header.
// The popup queries this when localStorage/cookies don't surface the token.

const seen = { authHeader: null, capturedAt: null };

chrome.webRequest.onSendHeaders.addListener(
  (details) => {
    if (!details.requestHeaders) return;
    for (const h of details.requestHeaders) {
      if (h.name.toLowerCase() === "authorization" && h.value && h.value.length > 20) {
        seen.authHeader = h.value;
        seen.capturedAt = Date.now();
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

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === "get-captured-auth-header") {
    sendResponse(seen);
  }
});

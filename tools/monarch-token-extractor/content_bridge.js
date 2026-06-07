// ISOLATED-world content script. Has access to chrome.* APIs but runs in
// a separate JS context from the page. Acts as a bridge between
// content_main.js (in the page) and background.js (the service worker).

window.addEventListener("message", (event) => {
  if (event.source !== window) return;
  const m = event.data;
  if (!m || !m.__monarchExtractor) return;
  if (m.type === "auth-captured") {
    chrome.runtime.sendMessage({
      type: "auth-captured",
      url: m.url || "",
      authHeader: m.authHeader || null,
      via: m.via || "unknown",
    }).catch(() => {});
  }
});

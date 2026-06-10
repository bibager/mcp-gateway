// ISOLATED-world bridge: relays page postMessages to the background worker.

window.addEventListener("message", (event) => {
  if (event.source !== window) return;
  const m = event.data;
  if (!m || !m.__datarovaRecon) return;
  if (m.type === "request-captured") {
    chrome.runtime.sendMessage({
      type: "request-captured",
      url: m.url || "",
      method: m.method || "?",
      authHeader: m.authHeader || null,
      requestHeaders: m.requestHeaders || {},
      requestBody: m.requestBody || null,
      via: m.via || "unknown",
    }).catch(() => {});
  }
});

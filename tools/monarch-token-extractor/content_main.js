// MAIN-world content script: runs inside the Monarch page's own JS context
// (NOT the extension's isolated world), so it can monkey-patch fetch/XHR
// before the app's code grabs references to them.
//
// Captured Authorization headers are forwarded via window.postMessage to the
// isolated-world bridge (content_bridge.js), which calls chrome.runtime.
// sendMessage so the background worker can persist them to chrome.storage.

(function () {
  const sendToBridge = (entry) => {
    try {
      window.postMessage({ __monarchExtractor: true, type: "auth-captured", ...entry }, "*");
    } catch (e) {
      /* ignore */
    }
  };

  // --- Patch fetch() -----------------------------------------------------
  const originalFetch = window.fetch;
  if (originalFetch && !originalFetch.__monarchPatched) {
    window.fetch = function (input, init) {
      try {
        // Find Authorization header in (init.headers) or (Request.headers)
        let authValue = null;
        let url = "";
        if (typeof input === "string") {
          url = input;
        } else if (input && typeof input === "object") {
          url = input.url || "";
        }
        if (init && init.headers) {
          if (init.headers instanceof Headers) {
            authValue = init.headers.get("Authorization") || init.headers.get("authorization");
          } else if (Array.isArray(init.headers)) {
            for (const [k, v] of init.headers) {
              if (String(k).toLowerCase() === "authorization") { authValue = v; break; }
            }
          } else if (typeof init.headers === "object") {
            for (const k of Object.keys(init.headers)) {
              if (k.toLowerCase() === "authorization") { authValue = init.headers[k]; break; }
            }
          }
        }
        if (!authValue && input instanceof Request) {
          authValue = input.headers.get("Authorization") || input.headers.get("authorization");
        }
        if (authValue) {
          sendToBridge({ url, authHeader: authValue, via: "fetch" });
        }
      } catch (e) {
        /* never break the page if our patch errors */
      }
      return originalFetch.apply(this, arguments);
    };
    window.fetch.__monarchPatched = true;
  }

  // --- Patch XMLHttpRequest ----------------------------------------------
  const xhrProto = XMLHttpRequest.prototype;
  if (xhrProto && !xhrProto.__monarchPatched) {
    const originalOpen = xhrProto.open;
    const originalSetHeader = xhrProto.setRequestHeader;
    xhrProto.open = function (method, url) {
      try { this.__monarchUrl = url; } catch (e) {}
      return originalOpen.apply(this, arguments);
    };
    xhrProto.setRequestHeader = function (name, value) {
      try {
        if (String(name).toLowerCase() === "authorization" && value && value.length > 20) {
          sendToBridge({ url: this.__monarchUrl || "", authHeader: value, via: "xhr" });
        }
      } catch (e) {}
      return originalSetHeader.apply(this, arguments);
    };
    xhrProto.__monarchPatched = true;
  }

  // Tell the bridge we're ready (used by popup to display extension health)
  sendToBridge({ url: location.href, authHeader: null, via: "ping" });
})();

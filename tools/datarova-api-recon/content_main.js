// MAIN-world content script: runs inside the Datarova page's own JS context
// so it can monkey-patch fetch/XHR before the app's code grabs references.
//
// For API recon we record EVERY outgoing API call:
//   - URL (so we can map endpoints)
//   - HTTP method
//   - Authorization header value
//   - Request body (truncated to 4KB to avoid blowing up storage)
//
// Forwarded to content_bridge.js via window.postMessage; bridge stores in
// chrome.storage.local; popup reads + copies as diagnostic JSON.

(function () {
  const sendToBridge = (entry) => {
    try {
      window.postMessage({ __datarovaRecon: true, type: "request-captured", ...entry }, "*");
    } catch (e) { /* never break the page */ }
  };

  const truncate = (v, max = 4096) => {
    if (typeof v !== "string") return v;
    return v.length > max ? v.slice(0, max) + `…[truncated ${v.length - max} chars]` : v;
  };

  // --- Patch fetch() -----------------------------------------------------
  const originalFetch = window.fetch;
  if (originalFetch && !originalFetch.__datarovaPatched) {
    window.fetch = function (input, init) {
      try {
        let url = "";
        let method = (init && init.method) || "GET";
        if (typeof input === "string") url = input;
        else if (input && typeof input === "object") {
          url = input.url || "";
          if (input.method) method = input.method;
        }

        let authValue = null;
        const headersObj = {};
        if (init && init.headers) {
          if (init.headers instanceof Headers) {
            init.headers.forEach((v, k) => {
              headersObj[k.toLowerCase()] = v;
              if (k.toLowerCase() === "authorization") authValue = v;
            });
          } else if (Array.isArray(init.headers)) {
            for (const [k, v] of init.headers) {
              headersObj[String(k).toLowerCase()] = v;
              if (String(k).toLowerCase() === "authorization") authValue = v;
            }
          } else if (typeof init.headers === "object") {
            for (const k of Object.keys(init.headers)) {
              headersObj[k.toLowerCase()] = init.headers[k];
              if (k.toLowerCase() === "authorization") authValue = init.headers[k];
            }
          }
        }
        if (!authValue && input instanceof Request) {
          authValue = input.headers.get("Authorization") || input.headers.get("authorization");
        }

        let body = null;
        if (init && init.body) {
          if (typeof init.body === "string") body = truncate(init.body);
          else if (init.body instanceof FormData) body = "[FormData]";
          else if (init.body instanceof URLSearchParams) body = truncate(init.body.toString());
        }

        // Only record same-origin / API-shaped calls to keep noise down
        if (url.includes("datarova.com") || url.startsWith("/") || url.startsWith("api/")) {
          sendToBridge({
            url,
            method,
            authHeader: authValue,
            requestHeaders: headersObj,
            requestBody: body,
            via: "fetch",
          });
        }
      } catch (e) { /* keep patch safe */ }
      return originalFetch.apply(this, arguments);
    };
    window.fetch.__datarovaPatched = true;
  }

  // --- Patch XMLHttpRequest ----------------------------------------------
  const xhrProto = XMLHttpRequest.prototype;
  if (xhrProto && !xhrProto.__datarovaPatched) {
    const originalOpen = xhrProto.open;
    const originalSetHeader = xhrProto.setRequestHeader;
    const originalSend = xhrProto.send;
    xhrProto.open = function (method, url) {
      try {
        this.__datarovaUrl = url;
        this.__datarovaMethod = method;
        this.__datarovaHeaders = {};
      } catch (e) {}
      return originalOpen.apply(this, arguments);
    };
    xhrProto.setRequestHeader = function (name, value) {
      try {
        const lc = String(name).toLowerCase();
        if (this.__datarovaHeaders) this.__datarovaHeaders[lc] = value;
      } catch (e) {}
      return originalSetHeader.apply(this, arguments);
    };
    xhrProto.send = function (body) {
      try {
        const url = this.__datarovaUrl || "";
        if (url.includes("datarova.com") || url.startsWith("/")) {
          const h = this.__datarovaHeaders || {};
          let bodyStr = null;
          if (typeof body === "string") bodyStr = truncate(body);
          else if (body instanceof FormData) bodyStr = "[FormData]";
          else if (body instanceof URLSearchParams) bodyStr = truncate(body.toString());
          sendToBridge({
            url,
            method: this.__datarovaMethod || "GET",
            authHeader: h.authorization || null,
            requestHeaders: h,
            requestBody: bodyStr,
            via: "xhr",
          });
        }
      } catch (e) {}
      return originalSend.apply(this, arguments);
    };
    xhrProto.__datarovaPatched = true;
  }

  // Ping the bridge so popup can confirm content script attached
  sendToBridge({ url: location.href, method: "PING", via: "ping" });
})();

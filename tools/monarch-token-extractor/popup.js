// Popup logic: scan localStorage + cookies + intercepted Authorization header
// for the active Monarch session token. Surface the most likely candidate and
// let the user copy it.

const el = (id) => document.getElementById(id);

async function findToken() {
  el("status").textContent = "";
  el("status").className = "status";
  el("result").innerHTML = '<div class="sub">Reading active tab…</div>';
  el("copy").disabled = true;

  let foundToken = null;
  let foundLocation = null;
  let debugInfo = "";

  // 1. Get current tab and verify it's a Monarch tab
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !tab.url || !/monarch(money)?\.com/.test(tab.url)) {
    el("result").innerHTML = `
      <div class="result-box err">
        <strong>Open <a href="https://app.monarch.com" target="_blank">app.monarch.com</a> in this tab first.</strong>
        <div class="status">Then log in and click Re-scan.</div>
      </div>`;
    return;
  }

  // 2. PRIMARY SOURCE: intercepted Authorization header from a real API call.
  // This is the ground truth — whatever auth Monarch's backend actually accepts.
  try {
    const captured = await chrome.runtime.sendMessage({ type: "get-captured-auth-header" });
    if (captured && captured.authHeader) {
      // Strip "Bearer "/"Token " prefix so the user always gets the raw token value
      const raw = captured.authHeader.replace(/^(Bearer|Token)\s+/i, "");
      foundToken = raw;
      const ageSec = captured.capturedAt ? Math.round((Date.now() - captured.capturedAt) / 1000) : null;
      const prefixMatch = captured.authHeader.match(/^(Bearer|Token)\s+/i);
      const prefix = prefixMatch ? prefixMatch[1] : "raw";
      foundLocation = `intercepted Authorization header — format: "${prefix} &lt;token&gt;"${ageSec !== null ? `, captured ${ageSec}s ago` : ""}`;
    }
  } catch (e) {
    debugInfo += `(header intercept lookup failed: ${e.message})\n`;
  }

  // 3. FALLBACK: read localStorage / sessionStorage / cookies. Only reach
  // here if the user hasn't triggered an API request yet this session.
  let pageData = { localStorage: {}, sessionStorage: {}, cookies: "" };
  if (!foundToken) {
    try {
      const [scriptResult] = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: () => {
          const storage = {};
          for (let i = 0; i < localStorage.length; i++) {
            const k = localStorage.key(i);
            storage[k] = localStorage.getItem(k);
          }
          const sessionStore = {};
          for (let i = 0; i < sessionStorage.length; i++) {
            const k = sessionStorage.key(i);
            sessionStore[k] = sessionStorage.getItem(k);
          }
          return { localStorage: storage, sessionStorage: sessionStore, cookies: document.cookie };
        },
      });
      pageData = scriptResult.result || pageData;
    } catch (e) {
      debugInfo += `(localStorage read failed: ${e.message})\n`;
    }

    const lookForTokenIn = (obj, depth = 0) => {
      if (depth > 6) return null;
      if (typeof obj === "string") {
        if (/^[A-Za-z0-9_.-]{30,}$/.test(obj)) return obj;
        return null;
      }
      if (!obj || typeof obj !== "object") return null;
      for (const key of ["token", "authToken", "access_token", "accessToken", "auth", "Authorization", "bearerToken"]) {
        if (obj[key] && typeof obj[key] === "string" && obj[key].length > 20) {
          return obj[key];
        }
      }
      for (const v of Object.values(obj)) {
        const found = lookForTokenIn(v, depth + 1);
        if (found) return found;
      }
      return null;
    };

    for (const [key, value] of Object.entries(pageData.localStorage || {})) {
      let parsed;
      try { parsed = JSON.parse(value); } catch { parsed = value; }
      const t = lookForTokenIn(parsed);
      if (t) {
        foundToken = t;
        foundLocation = `localStorage["${key}"] (fallback — may be a session ID rather than auth token)`;
        break;
      }
    }
    if (!foundToken) {
      for (const [key, value] of Object.entries(pageData.sessionStorage || {})) {
        let parsed;
        try { parsed = JSON.parse(value); } catch { parsed = value; }
        const t = lookForTokenIn(parsed);
        if (t) {
          foundToken = t;
          foundLocation = `sessionStorage["${key}"] (fallback — may be a session ID rather than auth token)`;
          break;
        }
      }
    }
    if (!foundToken) {
      try {
        const cookies = await chrome.cookies.getAll({ domain: "monarch.com" });
        const authCookies = cookies.filter(
          (c) => /token|auth|session/i.test(c.name) && c.value.length > 20
        );
        authCookies.sort((a, b) => b.value.length - a.value.length);
        if (authCookies.length > 0) {
          foundToken = authCookies[0].value;
          foundLocation = `cookie: ${authCookies[0].name} (fallback — may be a session ID rather than auth token)`;
        }
      } catch (e) {
        debugInfo += `(cookie read failed: ${e.message})\n`;
      }
    }
  }

  // 4. Render
  if (!foundToken) {
    el("result").innerHTML = `
      <div class="result-box err">
        <strong>No auth token found in this tab.</strong>
        <div class="status">
          To capture the real token: (1) keep this Monarch tab focused,
          (2) click around in Monarch's UI so the app makes an API request,
          then (3) hit Re-scan.
        </div>
        <div class="status">${debugInfo}</div>
      </div>`;
    return;
  }

  el("result").innerHTML = `
    <div class="result-box">
      <div class="ok"><strong>✓ Captured token</strong></div>
      <div class="label">Source: ${foundLocation}</div>
      <div class="label">Length: ${foundToken.length} chars</div>
      <div class="token-value">${escapeHtml(foundToken)}</div>
    </div>`;

  el("copy").disabled = false;
  el("copy").onclick = async () => {
    try {
      await navigator.clipboard.writeText(foundToken);
      el("status").textContent = `✓ Copied ${foundToken.length} chars to clipboard. Paste into Claude.`;
      el("status").className = "status ok";
    } catch (e) {
      el("status").textContent = `Copy failed: ${e.message}`;
      el("status").className = "status err";
    }
  };
}

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

el("refresh").addEventListener("click", findToken);
findToken();

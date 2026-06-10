// Datarova recon popup: shows captured endpoints + copies full diagnostic JSON
// for reverse-engineering the API.

const el = (id) => document.getElementById(id);

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function getCaptures() {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage({ type: "get-captures" }, (res) => resolve(res || []));
  });
}

async function getPageData(tabId) {
  if (!tabId) return null;
  try {
    const [r] = await chrome.scripting.executeScript({
      target: { tabId },
      func: () => {
        const ls = {};
        for (let i = 0; i < localStorage.length; i++) ls[localStorage.key(i)] = localStorage.getItem(localStorage.key(i));
        const ss = {};
        for (let i = 0; i < sessionStorage.length; i++) ss[sessionStorage.key(i)] = sessionStorage.getItem(sessionStorage.key(i));
        return {
          url: location.href,
          userAgent: navigator.userAgent,
          documentCookie: document.cookie,
          localStorage: ls,
          sessionStorage: ss,
        };
      },
    });
    return r.result || null;
  } catch (e) { return { error: e.message }; }
}

async function getCookies() {
  try {
    return (await chrome.cookies.getAll({ domain: "datarova.com" })).map(c => ({
      domain: c.domain, name: c.name, value: c.value,
      httpOnly: c.httpOnly, secure: c.secure, sameSite: c.sameSite,
    }));
  } catch (e) { return [{ error: e.message }]; }
}

function uniqueEndpoints(captures) {
  // group by stripped URL (no querystring) + method
  const map = new Map();
  for (const c of captures) {
    if (c.via === "ping") continue;
    const u = (c.url || "").split("?")[0];
    const key = `${c.method} ${u}`;
    if (!map.has(key)) {
      map.set(key, {
        method: c.method,
        url: u,
        callCount: 0,
        sample: c,
        hasAuth: !!c.authHeader,
      });
    }
    const e = map.get(key);
    e.callCount += (c.count || 1);
  }
  return [...map.values()].sort((a, b) => b.callCount - a.callCount);
}

async function refresh() {
  el("status").textContent = "";
  el("result").innerHTML = '<div class="sub">Reading captures…</div>';

  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const tabId = tab && tab.id;
  const tabUrl = (tab && tab.url) || "";
  const isDatarovaTab = /datarova\.com/.test(tabUrl);

  const [captures, pageData, cookies] = await Promise.all([
    getCaptures(),
    isDatarovaTab ? getPageData(tabId) : Promise.resolve(null),
    getCookies(),
  ]);

  // Filter to API-shaped requests (skip image/css/js fetches)
  const apiCaptures = captures.filter(c => {
    if (c.via === "ping") return false;
    const u = (c.url || "").toLowerCase();
    if (u.endsWith(".js") || u.endsWith(".css") || u.endsWith(".png")
        || u.endsWith(".jpg") || u.endsWith(".svg") || u.endsWith(".woff2")
        || u.endsWith(".ico") || u.endsWith(".webp") || u.endsWith(".gif")) return false;
    return true;
  });

  const endpoints = uniqueEndpoints(apiCaptures);
  const authedCaptures = apiCaptures.filter(c => c.authHeader);

  el("result").innerHTML = `
    <div class="stat-row">
      <div class="stat"><strong>${apiCaptures.length}</strong>total API calls</div>
      <div class="stat"><strong>${endpoints.length}</strong>unique endpoints</div>
      <div class="stat"><strong>${authedCaptures.length}</strong>with Authorization</div>
      <div class="stat"><strong>${cookies.length}</strong>cookies</div>
    </div>
    <div class="status ${isDatarovaTab ? 'ok' : 'err'}" style="margin-top:10px">
      ${isDatarovaTab
        ? "✓ Active tab is on datarova.com"
        : "⚠ Open app.datarova.com in this tab and click around the rank tracker"}
    </div>`;

  // Endpoint list
  if (endpoints.length > 0) {
    el("endpoints-section").style.display = "block";
    el("endpoint-count").textContent = String(endpoints.length);
    el("endpoints").innerHTML = endpoints.slice(0, 30).map(e => `
      <div class="endpoint">
        <span class="method-pill method-${escapeHtml(e.method)}">${escapeHtml(e.method)}</span>
        ${escapeHtml(e.url)} <span style="color:#888">·</span>
        <span style="color:#666">×${e.callCount}</span>
        ${e.hasAuth ? '<span class="ok" style="font-size:9px"> 🔐 auth</span>' : ''}
      </div>
    `).join("");
  } else {
    el("endpoints-section").style.display = "none";
  }

  el("copy-diag").onclick = async () => {
    const blob = {
      version: "1.0.0",
      capturedAt: new Date().toISOString(),
      tab: { url: tabUrl, isDatarovaTab },
      captures: apiCaptures,  // full list
      endpointSummary: endpoints,
      page: pageData,
      cookies,
    };
    const json = JSON.stringify(blob, null, 2);
    await navigator.clipboard.writeText(json);
    el("status").textContent = `✓ Copied ${(json.length/1024).toFixed(1)} KB diagnostic JSON (${apiCaptures.length} captures, ${endpoints.length} endpoints)`;
    el("status").className = "status ok";
  };

  el("clear").onclick = async () => {
    await new Promise(res => chrome.runtime.sendMessage({ type: "clear-captures" }, res));
    refresh();
  };
}

el("refresh").addEventListener("click", refresh);
refresh();

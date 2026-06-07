// Popup logic: read everything we've captured, render it, expose
// copy-the-token AND copy-the-diagnostic-JSON buttons.

const el = (id) => document.getElementById(id);

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function stripPrefix(header) {
  // Strip the Bearer / Token prefix so we get the raw value
  const m = header && header.match(/^(Bearer|Token)\s+(.+)$/i);
  return m ? { prefix: m[1], value: m[2] } : { prefix: null, value: header };
}

async function getCaptures() {
  try {
    return new Promise((resolve) => {
      chrome.runtime.sendMessage({ type: "get-captures" }, (res) => resolve(res || []));
    });
  } catch (e) {
    return [];
  }
}

async function getPageData(tabId) {
  if (!tabId) return null;
  try {
    const [r] = await chrome.scripting.executeScript({
      target: { tabId },
      func: () => {
        const ls = {};
        for (let i = 0; i < localStorage.length; i++) {
          const k = localStorage.key(i);
          ls[k] = localStorage.getItem(k);
        }
        const ss = {};
        for (let i = 0; i < sessionStorage.length; i++) {
          const k = sessionStorage.key(i);
          ss[k] = sessionStorage.getItem(k);
        }
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
  } catch (e) {
    return { error: e.message };
  }
}

async function getCookies() {
  try {
    const monarchCom = await chrome.cookies.getAll({ domain: "monarch.com" });
    const monarchMoney = await chrome.cookies.getAll({ domain: "monarchmoney.com" });
    return [...monarchCom, ...monarchMoney].map((c) => ({
      domain: c.domain,
      name: c.name,
      value: c.value,
      httpOnly: c.httpOnly,
      secure: c.secure,
      sameSite: c.sameSite,
      expirationDate: c.expirationDate,
    }));
  } catch (e) {
    return [{ error: e.message }];
  }
}

async function refresh() {
  el("status").textContent = "";
  el("status").className = "status";
  el("result").innerHTML = '<div class="sub">Reading captures…</div>';

  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const tabId = tab && tab.id;
  const tabUrl = (tab && tab.url) || "";
  const isMonarchTab = /monarch(money)?\.com/.test(tabUrl);

  // Pull all data in parallel
  const [captures, pageData, cookies] = await Promise.all([
    getCaptures(),
    isMonarchTab ? getPageData(tabId) : Promise.resolve(null),
    getCookies(),
  ]);

  // Find the most recent capture that has a real auth header
  const auths = captures.filter((c) => c.authHeader && c.authHeader.length >= 20);
  const latest = auths[auths.length - 1] || null;

  // Build the rendered top-line result
  if (latest) {
    const { prefix, value } = stripPrefix(latest.authHeader);
    const ageSec = latest.lastSeenAt ? Math.round((Date.now() - latest.lastSeenAt) / 1000) : null;
    el("result").innerHTML = `
      <div class="result-box found">
        <div class="ok"><strong>✓ Captured ${auths.length} Authorization header(s)</strong></div>
        <div class="label" style="margin-top:6px">
          Latest source: <strong>${latest.via}</strong> ${prefix ? `· prefix: <strong>${prefix}</strong>` : "· no prefix"}
          ${ageSec !== null ? ` · seen ${ageSec}s ago` : ""}
          ${latest.count && latest.count > 1 ? ` · ×${latest.count}` : ""}
        </div>
        <div class="label">Full header (with prefix):</div>
        <div class="token-value">${escapeHtml(latest.authHeader)}</div>
        <div class="label" style="margin-top:6px">Raw token (prefix stripped, ${value.length} chars):</div>
        <div class="token-value">${escapeHtml(value)}</div>
      </div>`;
    el("copy-token").disabled = false;
    el("copy-token").onclick = async () => {
      await navigator.clipboard.writeText(value);
      el("status").textContent = `✓ Copied ${value.length}-char raw token`;
      el("status").className = "status ok";
    };
  } else {
    const tabHint = isMonarchTab
      ? "Click anything in the Monarch UI to trigger an API call, then hit Re-scan."
      : "Open https://app.monarch.com in this tab first.";
    el("result").innerHTML = `
      <div class="result-box miss">
        <div class="err"><strong>No Authorization header captured yet.</strong></div>
        <div class="status">${tabHint}</div>
      </div>`;
    el("copy-token").disabled = true;
  }

  // Render history
  if (auths.length > 0) {
    el("history-section").style.display = "block";
    el("history-count").textContent = String(auths.length);
    el("history").innerHTML = auths
      .slice()
      .reverse()
      .map((c) => {
        const { prefix, value } = stripPrefix(c.authHeader);
        const age = c.lastSeenAt ? Math.round((Date.now() - c.lastSeenAt) / 1000) + "s ago" : "";
        return `
          <div class="result-box" style="margin-top:6px">
            <div class="label">
              <span class="pill ${escapeHtml((c.via || "").split(",")[0])}">${escapeHtml(c.via || "?")}</span>
              · ${age} ${c.count && c.count > 1 ? `· ×${c.count}` : ""}
              ${prefix ? `· <strong>${prefix}</strong>` : ""}
            </div>
            <div class="label">URL: <code>${escapeHtml((c.url || "").slice(0, 80))}</code></div>
            <div class="small-pre">${escapeHtml(value)}</div>
          </div>`;
      })
      .join("");
  } else {
    el("history-section").style.display = "none";
  }

  // Render page-data preview
  if (pageData && !pageData.error) {
    el("page-section").style.display = "block";
    const ls = Object.entries(pageData.localStorage || {})
      .map(([k, v]) => `  ${k} (${v.length} chars)`)
      .join("\n");
    const ss = Object.entries(pageData.sessionStorage || {})
      .map(([k, v]) => `  ${k} (${v.length} chars)`)
      .join("\n");
    el("page-data").innerHTML = `
      <div class="small-pre">URL: ${escapeHtml(pageData.url)}\nUA: ${escapeHtml(pageData.userAgent)}\n\nlocalStorage (${Object.keys(pageData.localStorage).length} keys):\n${escapeHtml(ls)}\n\nsessionStorage (${Object.keys(pageData.sessionStorage).length} keys):\n${escapeHtml(ss)}\n\ndocument.cookie:\n  ${escapeHtml(pageData.documentCookie)}\n\nChrome cookies API (${cookies.length} entries):\n${cookies.map(c=>`  ${c.name}@${c.domain}${c.httpOnly?" [HttpOnly]":""} (${(c.value||"").length} chars)`).join("\n")}</div>
    `;
  } else {
    el("page-section").style.display = "none";
  }

  // Wire up "Copy full diagnostic JSON"
  el("copy-diag").onclick = async () => {
    const blob = {
      version: "2.0.0",
      capturedAt: new Date().toISOString(),
      tab: { url: tabUrl, isMonarchTab },
      authHeaders: auths.map((c) => ({
        via: c.via,
        url: c.url,
        authHeader: c.authHeader,
        ...stripPrefix(c.authHeader),
        capturedAt: c.capturedAt,
        lastSeenAt: c.lastSeenAt,
        count: c.count,
      })),
      page: pageData,
      cookies,
    };
    const json = JSON.stringify(blob, null, 2);
    await navigator.clipboard.writeText(json);
    el("status").textContent = `✓ Copied ${json.length}-char diagnostic JSON`;
    el("status").className = "status ok";
  };

  // Wire up "Clear history"
  el("clear").onclick = async () => {
    await new Promise((res) => chrome.runtime.sendMessage({ type: "clear-captures" }, res));
    refresh();
  };
}

el("refresh").addEventListener("click", refresh);
refresh();

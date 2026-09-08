/* Logs page client script.
   External file required by CSP: script-src 'self' without unsafe-inline.
*/
(function () {
  "use strict";

  const form = document.getElementById("logs-control-form");
  const sourceSel = document.getElementById("log-source");
  const siteSel = document.getElementById("site-name");
  const targetSel = document.getElementById("log-target");
  const linesSel = document.getElementById("lines-count");
  const filterInput = document.getElementById("log-filter");
  const display = document.getElementById("log-display");
  const statusEl = document.getElementById("log-status");
  const btnRefresh = document.getElementById("btn-refresh");
  const autoRefreshCb = document.getElementById("auto-refresh");

  let rawLogContent = display ? display.textContent : "";
  let autoRefreshTimer = null;

  function renderFiltered() {
    if (!display) return;
    const term = filterInput ? filterInput.value.trim().toLowerCase() : "";
    if (!term) {
      display.textContent = rawLogContent;
      return;
    }

    const lines = rawLogContent.split("\n");
    const matched = lines.filter((l) => l.toLowerCase().includes(term));
    if (matched.length === 0) {
      display.textContent = `[No lines matched filter "${term}"]`;
    } else {
      display.textContent = matched.join("\n");
    }
  }

  if (filterInput) {
    filterInput.addEventListener("input", renderFiltered);
  }

  // Reload page when scope or site changes so options are repopulated
  if (sourceSel) {
    sourceSel.addEventListener("change", () => {
      form?.submit();
    });
  }

  if (siteSel) {
    siteSel.addEventListener("change", () => {
      form?.submit();
    });
  }

  if (targetSel) {
    targetSel.addEventListener("change", () => {
      fetchLog();
    });
  }

  if (linesSel) {
    linesSel.addEventListener("change", () => {
      fetchLog();
    });
  }

  async function fetchLog() {
    if (!targetSel || !linesSel) return;
    const logId = targetSel.value;
    const lines = linesSel.value;
    const source = sourceSel ? sourceSel.value : "service";
    const siteName = siteSel ? siteSel.value : "";

    if (statusEl) statusEl.textContent = "Updating…";

    try {
      const url = `/logs/content?source=${encodeURIComponent(source)}&name=${encodeURIComponent(siteName)}&log_id=${encodeURIComponent(logId)}&lines=${encodeURIComponent(lines)}`;
      const res = await fetch(url);
      if (!res.ok) {
        throw new Error("HTTP " + res.status);
      }
      const data = await res.json();
      rawLogContent = data.content || "";
      renderFiltered();
      if (statusEl) {
        const now = new Date().toLocaleTimeString();
        statusEl.textContent = `Updated at ${now}`;
      }
      if (display) {
        display.scrollTop = display.scrollHeight;
      }
    } catch (err) {
      if (statusEl) statusEl.textContent = `Error: ${err.message}`;
    }
  }

  if (btnRefresh) {
    btnRefresh.addEventListener("click", fetchLog);
  }

  if (autoRefreshCb) {
    autoRefreshCb.addEventListener("change", () => {
      if (autoRefreshCb.checked) {
        fetchLog();
        autoRefreshTimer = setInterval(fetchLog, 5000);
      } else if (autoRefreshTimer) {
        clearInterval(autoRefreshTimer);
        autoRefreshTimer = null;
      }
    });
  }

  // Scroll to bottom on initial load
  if (display) {
    display.scrollTop = display.scrollHeight;
  }
})();

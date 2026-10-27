/* Node.js / Python site detail page: live run state, log viewer and the
   command preset picker.
   CSP is script-src 'self' with no 'unsafe-inline', hence its own file.
*/
(function () {
  "use strict";

  const panel = document.getElementById("app-panel");
  if (!panel) return;
  const siteId = panel.getAttribute("data-site-id");
  const badge = document.getElementById("app-state-badge");

  function refreshState() {
    fetch(`/sites/${encodeURIComponent(siteId)}/app/status`, { headers: { Accept: "application/json" } })
      .then((res) => res.json())
      .then((data) => {
        if (!badge || data.error) return;
        badge.textContent = data.active ? "running" : "stopped";
        badge.classList.toggle("success", !!data.active);
      })
      .catch(() => {});
  }

  const output = document.getElementById("app-logs-output");
  function refreshLogs() {
    if (!output) return;
    fetch(`/sites/${encodeURIComponent(siteId)}/app/logs?lines=200`, { headers: { Accept: "application/json" } })
      .then((res) => res.json())
      .then((data) => {
        output.textContent = data.logs || data.error || "No output yet.";
        output.scrollTop = output.scrollHeight;
      })
      .catch(() => {
        output.textContent = "Could not load logs.";
      });
  }

  const refreshButton = document.getElementById("app-logs-refresh");
  if (refreshButton) refreshButton.addEventListener("click", refreshLogs);

  const preset = document.querySelector("[data-app-preset]");
  const command = document.getElementById("app-command");
  if (preset && command) {
    preset.addEventListener("change", () => {
      if (preset.value) command.value = preset.value;
    });
  }

  refreshState();
  refreshLogs();
  setInterval(refreshState, 10000);
})();

/* Site detail page: worker status badges, per-worker log viewer and the
   preset picker.  CSP is script-src 'self', hence its own file. */
(function () {
  "use strict";

  const panel = document.getElementById("workers-panel");
  if (!panel) return;
  const siteId = panel.getAttribute("data-site-id");
  const base = `/sites/${encodeURIComponent(siteId)}/workers`;

  function refreshState() {
    fetch(`${base}/status`, { headers: { Accept: "application/json" } })
      .then((res) => res.json())
      .then((data) => {
        (data.workers || []).forEach((item) => {
          const row = panel.querySelector(`.worker-row[data-worker-id="${item.id}"]`);
          const badge = row && row.querySelector("[data-worker-badge]");
          if (!badge) return;
          badge.textContent = item.label;
          badge.classList.toggle("success", !!item.active);
        });
      })
      .catch(() => {});
  }

  panel.querySelectorAll("[data-worker-logs]").forEach((button) => {
    const row = button.closest(".worker-row");
    const output = row.querySelector("[data-worker-logs-output]");
    const id = row.getAttribute("data-worker-id");

    function load() {
      output.textContent = "Loading…";
      fetch(`${base}/${encodeURIComponent(id)}/logs?lines=200`, { headers: { Accept: "application/json" } })
        .then((res) => res.json())
        .then((data) => {
          output.textContent = data.logs || data.error || "No output yet.";
          output.scrollTop = output.scrollHeight;
        })
        .catch(() => {
          output.textContent = "Could not load logs.";
        });
    }

    button.addEventListener("click", () => {
      if (output.hidden) {
        output.hidden = false;
        load();
      } else {
        output.hidden = true;
      }
    });
  });

  const preset = panel.querySelector("[data-worker-preset]");
  const name = document.getElementById("worker-name");
  const command = document.getElementById("worker-command");
  const processes = document.getElementById("worker-processes");
  if (preset && command) {
    preset.addEventListener("change", () => {
      const option = preset.options[preset.selectedIndex];
      if (!preset.value) return;
      command.value = preset.value;
      if (name && !name.value) name.value = option.getAttribute("data-name") || "";
      if (processes) processes.value = option.getAttribute("data-processes") || "1";
    });
  }

  if (panel.querySelector(".worker-row")) setInterval(refreshState, 10000);
})();

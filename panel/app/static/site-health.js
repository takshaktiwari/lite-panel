/* Site detail page: shows whether nginx can actually serve the site, and
   highlights the Repair permissions button when it cannot.
   CSP is script-src 'self' with no 'unsafe-inline', hence its own file. */
(function () {
  "use strict";

  const panel = document.getElementById("health-panel");
  if (!panel) return;
  const siteId = panel.getAttribute("data-site-id");
  const badge = document.getElementById("health-badge");
  const message = document.getElementById("health-message");
  const repair = document.getElementById("health-repair");

  fetch(`/sites/${encodeURIComponent(siteId)}/health`, { headers: { Accept: "application/json" } })
    .then((res) => res.json())
    .then((data) => {
      if (data.error) return;
      message.textContent = data.message;
      badge.textContent = data.ok ? "ok" : data.problem === "no_index" ? "no index file" : "blocked";
      badge.classList.toggle("success", !!data.ok);
      if (!data.ok && data.problem === "permissions") {
        badge.style.color = "var(--danger)";
        message.style.color = "var(--danger)";
        repair.classList.add("primary");
      }
    })
    .catch(() => {
      badge.textContent = "unknown";
      message.textContent = "Could not check this site.";
    });
})();

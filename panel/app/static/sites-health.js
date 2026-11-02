/* Sites page: tests every site with a real request and lists the ones that
   answer 403 / 500, with a button to repair permissions.
   CSP is script-src 'self' with no 'unsafe-inline', hence its own file. */
(function () {
  "use strict";

  const banner = document.getElementById("sites-health-banner");
  const list = document.getElementById("sites-health-list");
  if (!banner || !list) return;

  fetch("/sites/health", { headers: { Accept: "application/json" } })
    .then((res) => res.json())
    .then((data) => {
      const problems = data.problems || [];
      if (!problems.length) return;
      problems.forEach((item) => {
        const li = document.createElement("li");
        const link = document.createElement("a");
        link.href = `/sites/${encodeURIComponent(item.id)}`;
        link.textContent = item.domain;
        li.appendChild(link);
        li.appendChild(document.createTextNode(` — ${item.message}`));
        list.appendChild(li);
      });
      banner.hidden = false;
    })
    .catch(() => {});
})();

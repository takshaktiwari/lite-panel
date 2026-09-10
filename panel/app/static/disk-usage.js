/* Disk usage on the site detail page: fetched right after load, off the
   main render, since walking every file under a large site can take a
   couple of seconds and the rest of the page needs none of it.
   CSP is script-src 'self' with no 'unsafe-inline', hence its own file.
*/
(function () {
  "use strict";

  const el = document.getElementById("disk-usage-value");
  if (!el) return;

  const siteId = el.getAttribute("data-site-id");

  fetch(`/sites/${encodeURIComponent(siteId)}/disk-usage`, {
    headers: { "Accept": "application/json" },
  })
    .then((res) => res.json().then((data) => ({ ok: res.ok, data })))
    .then(({ ok, data }) => {
      el.textContent = ok ? data.human : (data.error || "Unavailable");
    })
    .catch(() => {
      el.textContent = "Unavailable";
    });
})();

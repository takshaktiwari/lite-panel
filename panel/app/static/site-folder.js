/* New-site form: fill the website folder from the domain as it is typed,
   until the folder is edited by hand.  Mirrors _name_from_domain in
   routers/sites.py (blog.example.com -> blog-example) so the field shows the
   folder that will really be used.
   CSP is script-src 'self' with no 'unsafe-inline', hence its own file.
*/
(function () {
  "use strict";

  const domain = document.getElementById("domain");
  const folder = document.getElementById("folder");
  if (!domain || !folder) return;

  function derive(value) {
    const parts = value.trim().toLowerCase().split(".");
    const stem = parts.length > 1 ? parts.slice(0, -1).join("-") : parts[0];
    return stem.replace(/[^a-z0-9-]/g, "-").replace(/^-+|-+$/g, "").slice(0, 32);
  }

  // Follow the domain while the folder is empty or still shows its own
  // derived value; typing anything else takes over until it is cleared.
  let following = !folder.value || folder.value === derive(domain.value);

  domain.addEventListener("input", () => {
    if (following) folder.value = derive(domain.value);
  });
  folder.addEventListener("input", () => {
    following = folder.value === "" || folder.value === derive(domain.value);
  });
})();

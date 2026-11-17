/* New-site form: fill the website folder from the domain as it is typed,
   until the folder is edited by hand.  Mirrors _name_from_domain in
   routers/sites.py (blog.example.com -> blog-example_com) so the field shows the
   folder that will really be used.
   CSP is script-src 'self' with no 'unsafe-inline', hence its own file.
*/
(function () {
  "use strict";

  const domain = document.getElementById("domain");
  const folder = document.getElementById("folder");
  if (!domain || !folder) return;

  // site_<name> is the Linux user, and Linux allows 32 characters.
  const MAX = 27;

  function derive(value) {
    const clean = (text) => text.toLowerCase().replace(/[^a-z0-9-]/g, "-").replace(/^-+|-+$/g, "");
    const parts = value.trim().split(".").filter(Boolean);
    if (parts.length < 2) return clean(value.trim()).slice(0, MAX).replace(/-+$/, "");
    const tld = clean(parts[parts.length - 1]);
    const suffix = tld ? `_${tld}` : "";
    const stem = clean(parts.slice(0, -1).join("-")).slice(0, MAX - suffix.length).replace(/^-+|-+$/g, "");
    return (stem ? stem + suffix : tld) || "";
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

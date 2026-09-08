/* FTP page: picking a site fills in the path field with that site's own
   folder (still editable); when the path exactly matches a listed site's
   folder, the account will be that site's own login, so the username field
   is disabled and a hint explains why. Purely cosmetic -- the server makes
   the same decision independently from the submitted path, so this still
   behaves correctly with JS disabled. */
(function () {
  "use strict";

  const sitePicker = document.querySelector("[data-ftp-site-picker]");
  const pathInput = document.querySelector("[data-ftp-path]");
  const usernameInput = document.querySelector("[data-ftp-username]");
  const hint = document.getElementById("ftp-site-hint");
  if (!sitePicker || !pathInput || !usernameInput || !hint) return;

  const hintDomain = document.getElementById("ftp-site-hint-domain");
  const hintUser = document.getElementById("ftp-site-hint-user");

  function siteForPath(value) {
    return Array.from(sitePicker.options).find(
      (opt) => opt.value && opt.getAttribute("data-root") === value
    );
  }

  function refreshHint() {
    const match = siteForPath(pathInput.value);
    hint.hidden = !match;
    usernameInput.disabled = !!match;
    if (match) {
      hintDomain.textContent = match.getAttribute("data-domain");
      hintUser.textContent = match.getAttribute("data-user");
    }
  }

  sitePicker.addEventListener("change", () => {
    const option = sitePicker.selectedOptions[0];
    const root = option && option.getAttribute("data-root");
    if (root) pathInput.value = root;
    refreshHint();
  });

  pathInput.addEventListener("input", refreshHint);
})();

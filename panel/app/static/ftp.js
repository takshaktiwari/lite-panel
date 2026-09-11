/* FTP page: picking a site fills in the path field with that site's own
   folder (still editable); when the path exactly matches a listed site's
   folder, the account will be that site's own login, so the username field
   is auto-filled with that login and made read-only, with a hint explaining
   why. Purely cosmetic -- the server makes the same decision independently
   from the submitted path, so this still behaves correctly with JS disabled
   (the server ignores the submitted username in that case either way). */
(function () {
  "use strict";

  const sitePicker = document.querySelector("[data-ftp-site-picker]");
  const pathInput = document.querySelector("[data-ftp-path]");
  const usernameInput = document.querySelector("[data-ftp-username]");
  const hint = document.getElementById("ftp-site-hint");
  if (!sitePicker || !pathInput || !usernameInput || !hint) return;

  const hintDomain = document.getElementById("ftp-site-hint-domain");
  const hintUser = document.getElementById("ftp-site-hint-user");

  let lastTypedUsername = usernameInput.value;
  usernameInput.addEventListener("input", () => {
    if (!usernameInput.readOnly) lastTypedUsername = usernameInput.value;
  });

  function siteForPath(value) {
    return Array.from(sitePicker.options).find(
      (opt) => opt.value && opt.getAttribute("data-root") === value
    );
  }

  function refreshHint() {
    const match = siteForPath(pathInput.value);
    hint.hidden = !match;
    if (match) {
      const siteUsername = match.getAttribute("data-user");
      usernameInput.value = siteUsername;
      usernameInput.readOnly = true;
      hintDomain.textContent = match.getAttribute("data-domain");
      hintUser.textContent = siteUsername;
    } else {
      usernameInput.readOnly = false;
      usernameInput.value = lastTypedUsername;
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

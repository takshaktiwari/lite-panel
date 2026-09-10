/* Site detail page: the pencil next to "Webroot" opens a dialog pre-filled
   with the current document path, since the field is inline HTML (not a
   templated attribute) it just opens/closes -- no dynamic fill-in needed. */
(function () {
  "use strict";

  document.addEventListener("click", (event) => {
    if (event.target.closest("#edit-webroot")) {
      document.getElementById("webroot-dialog")?.showModal();
      return;
    }
    const closer = event.target.closest("[data-dialog-close]");
    if (closer) closer.closest("dialog")?.close();
  });
})();

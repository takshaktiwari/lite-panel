/* Site-wide interactive helpers (confirm, dialogs).
   External file required by CSP (script-src 'self'). */
(function () {
  "use strict";

  // data-confirm confirmation prompt on submit buttons or forms
  document.addEventListener("submit", (event) => {
    const submitter = event.submitter;
    const msg = (submitter && submitter.getAttribute("data-confirm")) ||
                event.target.getAttribute("data-confirm");
    if (!msg) return;
    if (!window.confirm(msg)) {
      event.preventDefault();
    }
  });

  // Global dialog opener for [data-dialog-open]
  document.addEventListener("click", (e) => {
    const openBtn = e.target.closest("[data-dialog-open]");
    if (openBtn) {
      const targetId = openBtn.getAttribute("data-dialog-open");
      const dlg = document.getElementById(targetId);
      if (dlg && typeof dlg.showModal === "function") {
        dlg.showModal();
      }
      return;
    }

    // Global dialog closer for [data-close]
    const closeBtn = e.target.closest("[data-close]");
    if (closeBtn) {
      const dlg = closeBtn.closest("dialog");
      if (dlg) dlg.close();
      return;
    }

    // Trigger for Change Password dialog
    const pwdTrigger = e.target.closest("#btn-change-password-modal");
    if (pwdTrigger) {
      const details = pwdTrigger.closest("details");
      if (details) details.removeAttribute("open");

      const dlg = document.getElementById("dlg-change-user-password");
      if (dlg) {
        const curr = document.getElementById("current_password");
        const newP = document.getElementById("new_password");
        const confP = document.getElementById("confirm_password");
        if (curr) curr.value = "";
        if (newP) newP.value = "";
        if (confP) confP.value = "";
        dlg.showModal();
        if (curr) curr.focus();
      }
    }
  });
})();


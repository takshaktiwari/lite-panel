/* Site-wide interactive helpers (confirm, dialogs).
   External file required by CSP (script-src 'self'). */
(function () {
  "use strict";

  // data-confirm confirmation prompt on submit buttons
  document.addEventListener("submit", (event) => {
    const submitter = event.submitter;
    if (!submitter || !submitter.hasAttribute("data-confirm")) return;
    if (!window.confirm(submitter.getAttribute("data-confirm"))) {
      event.preventDefault();
    }
  });

  // Global dialog closer for [data-close]
  document.addEventListener("click", (e) => {
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


/* Databases page interactions.
   External file required by CSP: script-src 'self' without unsafe-inline.
*/
(function () {
  "use strict";

  // Toggle create database user mode
  const modeNew = document.getElementById("user_mode_new");
  const modeExisting = document.getElementById("user_mode_existing");
  const newWrap = document.getElementById("new_user_wrap");
  const existWrap = document.getElementById("existing_user_wrap");
  const pwdInput = document.getElementById("password");
  const pwdHint = document.getElementById("password_hint");

  const pwdWrap = document.getElementById("password_wrap");

  function updateCreateUserMode() {
    if (modeExisting && modeExisting.checked) {
      if (newWrap) newWrap.style.display = "none";
      if (existWrap) existWrap.style.display = "block";
      if (pwdWrap) pwdWrap.style.display = "none";
      if (pwdInput) {
        pwdInput.removeAttribute("required");
        pwdInput.value = "";
      }
    } else {
      if (newWrap) newWrap.style.display = "block";
      if (existWrap) existWrap.style.display = "none";
      if (pwdWrap) pwdWrap.style.display = "block";
      if (pwdInput) {
        pwdInput.setAttribute("required", "required");
        if (!pwdInput.value && pwdInput.dataset.suggested) {
          pwdInput.value = pwdInput.dataset.suggested;
        }
      }
    }
  }

  if (modeNew && modeExisting) {
    if (pwdInput && pwdInput.value) {
      pwdInput.dataset.suggested = pwdInput.value;
    }
    modeNew.addEventListener("change", updateCreateUserMode);
    modeExisting.addEventListener("change", updateCreateUserMode);
    updateCreateUserMode();
  }

  // Also support clicking labels directly if radio button isn't target
  document.querySelectorAll('input[name="user_mode"]').forEach((radio) => {
    radio.addEventListener("change", updateCreateUserMode);
  });

  // Dialog handling
  const dlgReassign = document.getElementById("dlg-reassign");
  const dlgPassword = document.getElementById("dlg-password");
  const dlgDelete = document.getElementById("dlg-delete");

  // Close dialog buttons
  document.querySelectorAll("dialog [data-close]").forEach((btn) => {
    btn.addEventListener("click", () => {
      btn.closest("dialog").close();
    });
  });

  // Reassign modal mode toggle
  const reassignModeExisting = document.getElementById("reassign_mode_existing");
  const reassignModeNew = document.getElementById("reassign_mode_new");
  const reassignExistWrap = document.getElementById("reassign-existing-wrap");
  const reassignNewWrap = document.getElementById("reassign-new-wrap");
  const reassignPwdWrap = document.getElementById("reassign-password-wrap");
  const reassignPwd = document.getElementById("reassign-password");

  function updateReassignMode() {
    if (reassignModeNew && reassignModeNew.checked) {
      if (reassignExistWrap) reassignExistWrap.style.display = "none";
      if (reassignNewWrap) reassignNewWrap.style.display = "block";
      if (reassignPwdWrap) reassignPwdWrap.style.display = "block";
      if (reassignPwd) reassignPwd.setAttribute("required", "required");
    } else {
      if (reassignExistWrap) reassignExistWrap.style.display = "block";
      if (reassignNewWrap) reassignNewWrap.style.display = "none";
      if (reassignPwdWrap) reassignPwdWrap.style.display = "none";
      if (reassignPwd) {
        reassignPwd.removeAttribute("required");
        reassignPwd.value = "";
      }
    }
  }

  if (reassignModeExisting && reassignModeNew) {
    reassignModeExisting.addEventListener("change", updateReassignMode);
    reassignModeNew.addEventListener("change", updateReassignMode);
  }

  // Action buttons
  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-action]");
    if (!btn) return;

    // Close any open details dropdown
    const details = btn.closest("details");
    if (details) details.removeAttribute("open");

    const action = btn.getAttribute("data-action");

    if (action === "reassign") {
      const dbId = btn.getAttribute("data-db-id");
      const dbName = btn.getAttribute("data-db-name");
      const currentUser = btn.getAttribute("data-current-user");

      document.getElementById("form-reassign").action = "/databases/" + dbId + "/user";
      document.getElementById("dlg-reassign-title").textContent = "Assign user for " + dbName;
      document.getElementById("dlg-reassign-desc").textContent = "Current user: " + currentUser;

      const sel = document.getElementById("reassign-existing-user");
      if (sel) sel.value = currentUser;
      if (reassignModeExisting) reassignModeExisting.checked = true;
      updateReassignMode();
      if (reassignPwd) reassignPwd.value = "";

      dlgReassign?.showModal();
    } else if (action === "password") {
      const dbId = btn.getAttribute("data-db-id");
      const dbUser = btn.getAttribute("data-db-user");

      document.getElementById("form-password").action = "/databases/" + dbId + "/password";
      document.getElementById("dlg-password-title").textContent = "Reset password for user: " + dbUser;
      document.getElementById("dlg-password-desc").textContent = "Used by this database.";
      document.getElementById("pwd-db-user").value = dbUser;
      document.getElementById("dlg-new-password").value = "";

      dlgPassword?.showModal();
    } else if (action === "user-password") {
      const dbUser = btn.getAttribute("data-db-user");

      document.getElementById("form-password").action = "/databases/user/password";
      document.getElementById("dlg-password-title").textContent = "Change password for " + dbUser;
      document.getElementById("dlg-password-desc").textContent =
        "This updates the password across all databases granted to " + dbUser + ".";
      document.getElementById("pwd-db-user").value = dbUser;
      document.getElementById("dlg-new-password").value = "";

      dlgPassword?.showModal();
    } else if (action === "delete") {
      const dbId = btn.getAttribute("data-db-id");
      const dbName = btn.getAttribute("data-db-name");

      document.getElementById("form-delete").action = "/databases/" + dbId + "/delete";
      document.getElementById("del-confirm-target").textContent = dbName;
      document.getElementById("del-confirm").value = "";

      dlgDelete?.showModal();
    }
  });
})();

/* File manager page behavior. data-confirm buttons (rename/delete/bulk
   delete's own confirm) are handled by the site-wide static/confirm.js;
   everything here is specific to this page: select-all, closing other open
   action dropdowns when one opens, prompting for a value (new name, copy
   destination, archive name) before a submit proceeds, and opening the
   shared "permissions inside a folder" dialog. */
(function () {
  "use strict";

  document.addEventListener("change", (event) => {
    if (event.target.id !== "select-all") return;
    document.querySelectorAll(".bulk-checkbox").forEach((checkbox) => {
      checkbox.checked = event.target.checked;
    });
  });

  document.addEventListener(
    "toggle",
    (event) => {
      const el = event.target;
      if (!(el instanceof HTMLDetailsElement) || !el.classList.contains("dropdown")) return;
      if (!el.open) return;
      document.querySelectorAll("details.dropdown[open]").forEach((other) => {
        if (other !== el) other.open = false;
      });
    },
    true
  );

  document.addEventListener("click", (event) => {
    document.querySelectorAll("details.dropdown[open]").forEach((el) => {
      if (!el.contains(event.target)) el.open = false;
    });
  });

  // Folder rows' "Permissions inside…" action opens the one shared dialog
  // (#chmod-dialog in files/browse.html) instead of cramming a mode field
  // and a scope <select> into the dropdown menu itself.
  document.addEventListener("click", (event) => {
    const opener = event.target.closest("[data-chmod-dialog]");
    if (!opener) return;

    const dialog = document.getElementById("chmod-dialog");
    if (!dialog) return;

    const dropdown = opener.closest("details.dropdown");
    if (dropdown) dropdown.open = false;

    dialog.querySelector('[name="target"]').value = opener.getAttribute("data-target") || "";
    dialog.querySelector("#chmod-dialog-name").textContent = opener.getAttribute("data-name") || "";
    dialog.querySelector('[name="mode"]').value = "";
    dialog.showModal();
  });

  document.addEventListener("click", (event) => {
    const closer = event.target.closest("[data-dialog-close]");
    if (!closer) return;
    closer.closest("dialog")?.close();
  });

  // A submit button marked data-prompt-field="foo" asks for a value via
  // prompt() and fills it into the form's "foo" field before submitting --
  // renaming, the bulk copy destination, and the archive name all use this
  // same pattern. Cancelling the prompt cancels the submit.
  document.addEventListener("submit", (event) => {
    const submitter = event.submitter;
    const fieldName = submitter && submitter.getAttribute("data-prompt-field");
    if (!fieldName) return;

    const message = submitter.getAttribute("data-prompt-message") || "Value";
    const fallback = submitter.getAttribute("data-prompt-default") || "";
    const value = window.prompt(message, fallback);
    if (!value) {
      event.preventDefault();
      return;
    }

    const field = submitter.form.elements.namedItem(fieldName);
    if (field) field.value = value;
  });
})();

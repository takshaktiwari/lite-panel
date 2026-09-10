/* Dashboard service toggles. External file for strict CSP compatibility. */
(function () {
  "use strict";

  document.querySelectorAll(".service-toggle-input").forEach((checkbox) => {
    checkbox.addEventListener("change", () => {
      const confirmMsg = checkbox.getAttribute("data-confirm");
      if (confirmMsg && !window.confirm(confirmMsg)) {
        checkbox.checked = !checkbox.checked;
        return;
      }
      checkbox.closest("form")?.submit();
    });
  });
})();

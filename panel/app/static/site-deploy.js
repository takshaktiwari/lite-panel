/* Site page, Deployment section: script presets and copy buttons.
   CSP is script-src 'self' with no 'unsafe-inline', hence its own file. */
(function () {
  "use strict";

  const panel = document.getElementById("deploy-panel");
  if (!panel) return;

  panel.querySelectorAll("[data-deploy-preset]").forEach((select) => {
    select.addEventListener("change", () => {
      const option = select.options[select.selectedIndex];
      const script = select.closest("form").querySelector("textarea[name=script]");
      if (!option || !select.value || !script) return;
      script.value = option.getAttribute("data-script") || "";
      select.value = "";
    });
  });

  // The panel cannot know its public https://host:port behind nginx; the browser can.
  panel.querySelectorAll("[data-origin-path]").forEach((field) => {
    field.value = window.location.origin + field.getAttribute("data-origin-path");
  });

  panel.querySelectorAll("[data-copy-target]").forEach((button) => {
    button.addEventListener("click", () => {
      const field = document.getElementById(button.getAttribute("data-copy-target"));
      if (!field) return;
      const done = () => {
        const label = button.textContent;
        button.textContent = "Copied";
        setTimeout(() => { button.textContent = label; }, 1500);
      };
      if (navigator.clipboard) {
        navigator.clipboard.writeText(field.value).then(done).catch(() => { field.select(); });
      } else {
        field.select();
        document.execCommand("copy");
        done();
      }
    });
  });
})();

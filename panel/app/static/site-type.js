/* New-site form: show the PHP options or the Node.js / Python application
   options depending on the chosen site type, and let a preset fill in the
   start command.
   CSP is script-src 'self' with no 'unsafe-inline', hence its own file.
*/
(function () {
  "use strict";

  const select = document.querySelector("[data-site-type]");
  if (!select) return;

  const webSection = document.querySelector('[data-type-section="web"]');
  const runtimeSection = document.querySelector('[data-type-section="runtime"]');
  const command = document.getElementById("app_command");

  function apply() {
    const type = select.value;
    const isRuntime = type !== "web";

    if (webSection) {
      webSection.hidden = isRuntime;
      webSection.querySelectorAll("select, input").forEach((el) => { el.disabled = isRuntime; });
    }
    if (runtimeSection) {
      runtimeSection.hidden = !isRuntime;
      runtimeSection.querySelectorAll("input[name], textarea[name]").forEach((el) => {
        el.disabled = !isRuntime;
      });
    }

    document.querySelectorAll("[data-preset-for]").forEach((el) => {
      el.hidden = el.getAttribute("data-preset-for") !== type;
    });
    document.querySelectorAll("[data-hint-for]").forEach((el) => {
      el.hidden = el.getAttribute("data-hint-for") !== type;
    });
    document.querySelectorAll("[data-runtime-warning]").forEach((el) => {
      el.hidden = el.getAttribute("data-runtime-warning") !== type;
    });
    if (command && isRuntime) {
      const hint = document.querySelector('[data-hint-for="' + type + '"]');
      if (hint) command.placeholder = hint.getAttribute("data-default-command") || "";
    }
  }

  document.querySelectorAll("[data-preset-for]").forEach((preset) => {
    preset.addEventListener("change", () => {
      if (preset.value && command) command.value = preset.value;
    });
  });

  select.addEventListener("change", apply);
  apply();
})();

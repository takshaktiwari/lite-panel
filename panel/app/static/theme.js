/* Theme (Dark / Light) switcher for LitePanel.
   Compliant with CSP: script-src 'self' without unsafe-inline.
*/
(function () {
  "use strict";

  const STORAGE_KEY = "litepanel_theme";

  function getSystemPreference() {
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches
      ? "dark"
      : "light";
  }

  function getActiveTheme() {
    try {
      const saved = localStorage.getItem(STORAGE_KEY);
      if (saved === "dark" || saved === "light") {
        return saved;
      }
    } catch (e) {}
    return getSystemPreference();
  }

  function updateIconsAndLabels(theme) {
    const sunIcons = document.querySelectorAll(".theme-icon-sun");
    const moonIcons = document.querySelectorAll(".theme-icon-moon");
    const dropdownIcon = document.getElementById("dropdown-theme-icon");
    const dropdownText = document.getElementById("dropdown-theme-text");
    const toggleBtns = document.querySelectorAll("[data-theme-toggle]");

    if (theme === "dark") {
      sunIcons.forEach((el) => (el.style.display = "block"));
      moonIcons.forEach((el) => (el.style.display = "none"));
      if (dropdownIcon) dropdownIcon.textContent = "☀️";
      if (dropdownText) dropdownText.textContent = "Light mode";
      toggleBtns.forEach((b) =>
        b.setAttribute("title", "Switch to Light mode (currently Dark)")
      );
    } else {
      sunIcons.forEach((el) => (el.style.display = "none"));
      moonIcons.forEach((el) => (el.style.display = "block"));
      if (dropdownIcon) dropdownIcon.textContent = "🌙";
      if (dropdownText) dropdownText.textContent = "Dark mode";
      toggleBtns.forEach((b) =>
        b.setAttribute("title", "Switch to Dark mode (currently Light)")
      );
    }
  }

  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    updateIconsAndLabels(theme);
  }

  function toggleTheme() {
    const current = document.documentElement.getAttribute("data-theme") || getActiveTheme();
    const next = current === "dark" ? "light" : "dark";
    try {
      localStorage.setItem(STORAGE_KEY, next);
    } catch (e) {}
    applyTheme(next);
  }

  // 1. Immediately apply theme to avoid any visual flicker
  applyTheme(getActiveTheme());

  // 2. React to OS preference changes if user hasn't explicitly saved a choice
  if (window.matchMedia) {
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", (e) => {
      try {
        if (!localStorage.getItem(STORAGE_KEY)) {
          applyTheme(e.matches ? "dark" : "light");
        }
      } catch (err) {}
    });
  }

  // 3. Attach click handlers when DOM is ready
  function init() {
    updateIconsAndLabels(document.documentElement.getAttribute("data-theme") || getActiveTheme());

    document.querySelectorAll("[data-theme-toggle]").forEach((btn) => {
      btn.addEventListener("click", toggleTheme);
    });

    const dropBtn = document.getElementById("btn-theme-dropdown");
    if (dropBtn) {
      dropBtn.addEventListener("click", toggleTheme);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();

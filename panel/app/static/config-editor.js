/* Config editor — initialises CodeMirror on the editor page.
   Reads data-mode from the script tag that loaded this file.        */
(function () {
  "use strict";

  var scriptEl = document.currentScript;
  var mode = scriptEl ? (scriptEl.getAttribute("data-mode") || "nginx") : "nginx";

  // Prefer dark theme when user's system is in dark mode.
  var prefersDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  var theme = prefersDark ? "dracula" : "default";

  var textarea = document.getElementById("config-content");
  var mount = document.getElementById("editor-mount");
  var form = document.getElementById("config-editor-form");
  var saveBtn = document.getElementById("btn-save-config");

  if (!textarea || !mount || !form) return;

  // Mount CodeMirror into the mount div (not the textarea, so we can
  // control the layout height with CSS on #editor-mount).
  var cm = CodeMirror(mount, {
    value: textarea.value,
    mode: mode,
    theme: theme,
    lineNumbers: true,
    indentUnit: 4,
    tabSize: 4,
    indentWithTabs: false,
    lineWrapping: false,
    autofocus: true,
    extraKeys: {
      "Ctrl-S": function () { submitForm(); },
      "Cmd-S":  function () { submitForm(); },
    },
  });

  // Sync CodeMirror → textarea on submit so the POST body has the latest value.
  function submitForm() {
    textarea.value = cm.getValue();
    form.submit();
  }

  if (saveBtn) {
    saveBtn.addEventListener("click", function () {
      submitForm();
    });
  }

  // Make the editor fill the remaining vertical space.
  function resizeEditor() {
    var editorWrap = document.querySelector(".editor-wrap");
    if (!editorWrap) return;
    var rect = editorWrap.getBoundingClientRect();
    var toolbarH = (document.querySelector(".editor-toolbar") || {offsetHeight: 50}).offsetHeight;
    var subtitleH = (document.querySelector(".muted") || {offsetHeight: 0}).offsetHeight;
    var panelPad = 0;
    var available = window.innerHeight - rect.top - toolbarH - subtitleH - panelPad - 40;
    mount.style.height = Math.max(300, available) + "px";
    cm.setSize("100%", Math.max(300, available) + "px");
    cm.refresh();
  }

  resizeEditor();
  window.addEventListener("resize", resizeEditor);
}());

/* A plain <textarea> sends focus to the next field on Tab, which makes it
   unusable for indented code or config -- basic as this editor is meant to
   stay, that one behavior is worth the few lines to fix. External file, not
   inline: the panel's CSP is script-src 'self' with no unsafe-inline. */
(function () {
  "use strict";
  const editor = document.getElementById("editor");
  if (!editor) return;

  editor.addEventListener("keydown", (event) => {
    if (event.key !== "Tab") return;
    event.preventDefault();
    const start = editor.selectionStart;
    const end = editor.selectionEnd;
    editor.value = editor.value.slice(0, start) + "\t" + editor.value.slice(end);
    editor.selectionStart = editor.selectionEnd = start + 1;
  });
})();

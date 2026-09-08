/* Site-wide: a submit button marked data-confirm="..." asks before its form
   submits. Loaded on every authenticated page rather than per-page, since
   several unrelated pages need exactly this (deleting a site, removing a
   stack provider, disabling FTP). External file, not inline onclick: the
   panel's CSP is script-src 'self' with no unsafe-inline, which silently
   blocks inline event handler attributes -- not just <script> blocks -- so
   an onclick="return confirm(...)" attribute never runs in the browser at
   all. Found because several buttons across the panel had exactly that
   attribute and were submitting immediately with no dialog ever appearing. */
(function () {
  "use strict";
  document.addEventListener("submit", (event) => {
    const submitter = event.submitter;
    if (!submitter || !submitter.hasAttribute("data-confirm")) return;
    if (!window.confirm(submitter.getAttribute("data-confirm"))) {
      event.preventDefault();
    }
  });
})();

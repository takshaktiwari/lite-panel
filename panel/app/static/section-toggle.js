/* Generic show/hide toggle, site-wide: a button marked
   data-toggle-target="some-id" shows or hides the element with that id, and
   swaps its own label between data-toggle-label-show / data-toggle-label-hide
   when both are given. Used to keep a big "create ___" form out of the way
   until someone actually wants it, without a bespoke handler on every page
   that needs the same interaction.
*/
(function () {
  "use strict";

  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-toggle-target]");
    if (!button) return;

    const target = document.getElementById(button.getAttribute("data-toggle-target"));
    if (!target) return;

    target.hidden = !target.hidden;

    const showLabel = button.getAttribute("data-toggle-label-show");
    const hideLabel = button.getAttribute("data-toggle-label-hide");
    if (showLabel && hideLabel) {
      button.textContent = target.hidden ? showLabel : hideLabel;
    }

    if (!target.hidden) {
      target.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  });
})();

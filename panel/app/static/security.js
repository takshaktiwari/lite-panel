/* Malware Scan page: show the day-of-week / day-of-month picker that matches
   the chosen schedule frequency. External file required by CSP (script-src 'self'). */
(function () {
  "use strict";

  const freq = document.getElementById("sched-freq");
  if (!freq) return;

  function sync() {
    const dow = document.getElementById("sched-dow-wrap");
    const dom = document.getElementById("sched-dom-wrap");
    if (dow) dow.style.display = freq.value === "weekly" ? "flex" : "none";
    if (dom) dom.style.display = freq.value === "monthly" ? "flex" : "none";
  }

  freq.addEventListener("change", sync);
  sync();
})();

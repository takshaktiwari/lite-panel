/* Cron page behavior. Choosing a schedule preset fills the five raw
   minute/hour/day/month/weekday fields and hides them; choosing "Custom"
   reveals those fields for manual entry. Runs on both the new-job form
   (cron/list.html) and the edit form (cron/edit.html), which share the same
   data-cron-preset / data-cron-custom markup. */
(function () {
  "use strict";

  function apply(select) {
    const form = select.form;
    if (!form) return;
    const customBlocks = form.querySelectorAll("[data-cron-custom]");

    if (select.value === "custom") {
      customBlocks.forEach((el) => { el.hidden = false; });
      return;
    }

    customBlocks.forEach((el) => { el.hidden = true; });
    const parts = select.value.split(" ");
    if (parts.length !== 5) return;
    const [minute, hour, dayOfMonth, month, dayOfWeek] = parts;
    form.elements.minute.value = minute;
    form.elements.hour.value = hour;
    form.elements.day_of_month.value = dayOfMonth;
    form.elements.month.value = month;
    form.elements.day_of_week.value = dayOfWeek;
  }

  document.addEventListener("change", (event) => {
    if (!event.target.matches("[data-cron-preset]")) return;
    apply(event.target);
  });

  document.querySelectorAll("[data-cron-preset]").forEach(apply);
})();

/* Backups page: the "Backup Now" / "Automated Schedule" tabs and the
   weekly / monthly fields of the schedule form.
   CSP is script-src 'self' with no 'unsafe-inline', hence its own file --
   an inline <script> block here silently did nothing. */
document.addEventListener("DOMContentLoaded", function() {
  const tabNow = document.getElementById("tab-btn-now");
  const tabSchedule = document.getElementById("tab-btn-schedule");
  const formNow = document.getElementById("form-backup-now");
  const formSchedule = document.getElementById("form-backup-schedule");
  const freqSelect = document.getElementById("frequency");
  const dowWrap = document.getElementById("day_of_week_wrap");
  const domWrap = document.getElementById("day_of_month_wrap");

  if (tabNow && tabSchedule && formNow && formSchedule) {
    tabNow.addEventListener("click", function() {
      tabNow.classList.add("active");
      tabSchedule.classList.remove("active");
      formNow.style.display = "block";
      formSchedule.style.display = "none";
    });

    tabSchedule.addEventListener("click", function() {
      tabSchedule.classList.add("active");
      tabNow.classList.remove("active");
      formSchedule.style.display = "block";
      formNow.style.display = "none";
    });
  }

  if (freqSelect && dowWrap && domWrap) {
    function updateFrequencyFields() {
      const val = freqSelect.value;
      dowWrap.style.display = (val === "weekly") ? "block" : "none";
      domWrap.style.display = (val === "monthly") ? "block" : "none";
    }
    freqSelect.addEventListener("change", updateFrequencyFields);
    updateFrequencyFields();
  }
});

/* Generic table filtering, site-wide:
   An input marked data-table-filter="#table-selector" filters rows in that
   table as the user types.
   Features:
   - Real-time case-insensitive multi-word search across row text
   - Dynamically updates [data-filter-count-for="#table-selector"] count badge
   - Displays a clean "No matching results" row when all rows are filtered out
   - Responds to 'input', 'search' (clear 'x'), and 'Escape' key
   - Global '/' keyboard shortcut to quickly focus the search bar
*/
(function () {
  "use strict";

  function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
  }

  function filterTable(input) {
    const targetSelector = input.getAttribute("data-table-filter");
    if (!targetSelector) return;

    const table = document.querySelector(targetSelector);
    if (!table) return;

    const tbody = table.querySelector("tbody") || table;
    const rows = Array.from(tbody.querySelectorAll("tr:not(.table-filter-empty)"));
    const countEl = document.querySelector(`[data-filter-count-for="${targetSelector}"]`);

    if (countEl && !countEl.dataset.initialText) {
      countEl.dataset.initialText = countEl.textContent.trim();
    }

    const query = input.value.trim().toLowerCase();
    let emptyRow = tbody.querySelector(".table-filter-empty");

    if (!query) {
      for (let i = 0; i < rows.length; i++) {
        rows[i].style.display = "";
      }
      if (emptyRow) emptyRow.remove();
      if (countEl && countEl.dataset.initialText) {
        countEl.textContent = countEl.dataset.initialText;
      }
      return;
    }

    const words = query.split(/\s+/).filter(Boolean);
    let visibleCount = 0;

    for (let i = 0; i < rows.length; i++) {
      const row = rows[i];
      const text = row.textContent.toLowerCase();
      const matches = words.every((word) => text.includes(word));
      if (matches) {
        row.style.display = "";
        visibleCount++;
      } else {
        row.style.display = "none";
      }
    }

    if (countEl) {
      if (visibleCount === 0) {
        countEl.textContent = "No matches";
      } else if (visibleCount === rows.length) {
        countEl.textContent = countEl.dataset.initialText || `${visibleCount} total`;
      } else {
        countEl.textContent = `Showing ${visibleCount} of ${rows.length}`;
      }
    }

    if (visibleCount === 0) {
      const ths = table.querySelectorAll("thead th");
      const colCount = ths.length || 10;
      if (!emptyRow) {
        emptyRow = document.createElement("tr");
        emptyRow.className = "table-filter-empty";
        tbody.appendChild(emptyRow);
      }
      emptyRow.innerHTML = `<td colspan="${colCount}" class="muted empty" style="text-align:center;padding:24px 8px;">No matching results for "<strong>${escapeHtml(input.value.trim())}</strong>"</td>`;
      emptyRow.style.display = "";
    } else if (emptyRow) {
      emptyRow.remove();
    }
  }

  document.addEventListener("input", (e) => {
    if (e.target && e.target.matches("[data-table-filter]")) {
      filterTable(e.target);
    }
  });

  document.addEventListener("search", (e) => {
    if (e.target && e.target.matches("[data-table-filter]")) {
      filterTable(e.target);
    }
  });

  document.addEventListener("change", (e) => {
    const select = e.target && e.target.closest("[data-site-filter-url]");
    if (!select) return;
    const base = select.getAttribute("data-site-filter-url");
    window.location.href = select.value ? base + encodeURIComponent(select.value) : base.split("?")[0];
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && e.target && e.target.matches("[data-table-filter]")) {
      e.target.value = "";
      filterTable(e.target);
      e.target.blur();
      return;
    }

    if (e.key === "/" && !e.ctrlKey && !e.metaKey && !e.altKey) {
      const tag = document.activeElement ? document.activeElement.tagName : "";
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || (document.activeElement && document.activeElement.isContentEditable)) {
        return;
      }
      const firstFilter = document.querySelector(".table-filter-input");
      if (firstFilter && firstFilter.offsetParent !== null) {
        e.preventDefault();
        firstFilter.focus();
        firstFilter.select();
      }
    }
  });
})();

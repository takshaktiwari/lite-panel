/* Server Monitor & Statistics client script.
   Compliant with CSP: script-src 'self' without unsafe-inline.
*/
(function () {
  "use strict";

  const csrfToken = document.getElementById("csrf-token")?.value || "";

  // Elements
  const valCpu = document.getElementById("val-cpu");
  const barCpu = document.getElementById("bar-cpu");
  const subCpu = document.getElementById("sub-cpu");

  const valMem = document.getElementById("val-mem");
  const barMem = document.getElementById("bar-mem");
  const subMem = document.getElementById("sub-mem");

  const valDisk = document.getElementById("val-disk");
  const barDisk = document.getElementById("bar-disk");
  const subDisk = document.getElementById("sub-disk");

  const valNet = document.getElementById("val-net");
  const subNet = document.getElementById("sub-net");

  const procTbody = document.getElementById("tbody-processes");
  const procFilter = document.getElementById("proc-filter");
  const btnRefresh = document.getElementById("btn-refresh-monitor");

  const chartCpuLatest = document.getElementById("chart-cpu-latest");
  const chartMemLatest = document.getElementById("chart-mem-latest");

  let currentSort = "cpu";
  let activeRange = "24h";

  // Parse initial history data
  let historyData = [];
  try {
    const rawHistory = document.getElementById("initial-history-data")?.textContent;
    if (rawHistory) {
      historyData = JSON.parse(rawHistory);
    }
  } catch (e) {
    historyData = [];
  }

  // -------------------------------------------------------------------------
  // SVG Sparkline / Area Chart Rendering
  // -------------------------------------------------------------------------

  function renderSvgChart(svgId, dataKey, colorStroke, colorFill) {
    const svg = document.getElementById(svgId);
    if (!svg) return;

    if (!historyData || historyData.length === 0) {
      svg.innerHTML = `<text x="250" y="60" text-anchor="middle" fill="#888" font-size="12">Collecting data snapshots (takes 1-2 mins)…</text>`;
      return;
    }

    const width = 500;
    const height = 120;
    const padding = 10;
    const innerW = width - padding * 2;
    const innerH = height - padding * 2;

    const values = historyData.map((d) => d[dataKey] || 0);
    const maxVal = Math.max(100, Math.max(...values));
    const minVal = 0;

    const points = values.map((val, idx) => {
      const x = padding + (idx / (values.length - 1 || 1)) * innerW;
      const y = height - padding - ((val - minVal) / (maxVal - minVal || 1)) * innerH;
      return [x, y];
    });

    const pathD = points.map((pt, idx) => (idx === 0 ? `M ${pt[0]} ${pt[1]}` : `L ${pt[0]} ${pt[1]}`)).join(" ");
    const areaD = `${pathD} L ${points[points.length - 1][0]} ${height} L ${points[0][0]} ${height} Z`;

    svg.innerHTML = `
      <defs>
        <linearGradient id="grad-${dataKey}" x1="0%" y1="0%" x2="0%" y2="100%">
          <stop offset="0%" stop-color="${colorFill}" stop-opacity="0.35"/>
          <stop offset="100%" stop-color="${colorFill}" stop-opacity="0.0"/>
        </linearGradient>
      </defs>
      <line x1="${padding}" y1="${height - padding}" x2="${width - padding}" y2="${height - padding}" stroke="#23272e" stroke-width="1"/>
      <line x1="${padding}" y1="${height / 2}" x2="${width - padding}" y2="${height / 2}" stroke="#23272e" stroke-dasharray="3,3" stroke-width="1"/>
      <path d="${areaD}" fill="url(#grad-${dataKey})"/>
      <path d="${pathD}" fill="none" stroke="${colorStroke}" stroke-width="2"/>
    `;
  }

  function renderAllCharts() {
    renderSvgChart("svg-cpu", "cpu", "var(--accent)", "#2563eb");
    renderSvgChart("svg-mem", "memory", "var(--warn, #eab308)", "#eab308");
  }

  renderAllCharts();

  // -------------------------------------------------------------------------
  // Live Metrics Polling & DOM Updating
  // -------------------------------------------------------------------------

  async function fetchLiveMetrics() {
    try {
      const res = await fetch(`/monitor/live?sort_by=${currentSort}`);
      if (!res.ok) return;
      const data = await res.json();
      updateLiveUi(data.stats);
      updateProcessesUi(data.processes);
    } catch (e) {
      // transient network error
    }
  }

  function updateLiveUi(stats) {
    if (!stats) return;

    if (valCpu) valCpu.textContent = `${stats.cpu.percent}%`;
    if (barCpu) barCpu.style.width = `${Math.min(stats.cpu.percent, 100)}%`;
    if (subCpu) subCpu.textContent = `${stats.cpu.cores} cores · Load: ${stats.load.join(" ")}`;
    if (chartCpuLatest) chartCpuLatest.textContent = `${stats.cpu.percent}%`;

    if (valMem) valMem.textContent = `${stats.memory.percent}%`;
    if (barMem) barMem.style.width = `${Math.min(stats.memory.percent, 100)}%`;
    if (subMem) subMem.textContent = `${stats.memory.used_mb} / ${stats.memory.total_mb} MB`;
    if (chartMemLatest) chartMemLatest.textContent = `${stats.memory.percent}%`;

    if (valDisk) valDisk.textContent = `${stats.disk.percent}%`;
    if (barDisk) barDisk.style.width = `${Math.min(stats.disk.percent, 100)}%`;

    if (valNet) valNet.textContent = `↓ ${stats.network.rx_kb_s} KB/s`;
    if (subNet) subNet.textContent = `↑ ${stats.network.tx_kb_s} KB/s outgoing`;
  }

  function updateProcessesUi(procs) {
    if (!procTbody || !procs) return;
    const filter = procFilter ? procFilter.value.trim().toLowerCase() : "";

    const rowsHtml = procs
      .filter((p) => !filter || p.name.toLowerCase().includes(filter) || p.username.toLowerCase().includes(filter))
      .map((p) => {
        const badgeClass = p.status === "running" || p.status === "active" ? "success" : "";
        return `
          <tr data-pid="${p.pid}" data-name="${p.name}">
            <td><code>${p.pid}</code></td>
            <td><strong>${p.name}</strong></td>
            <td class="muted">${p.username}</td>
            <td style="text-align:right;font-family:var(--mono);">${p.cpu_percent}%</td>
            <td style="text-align:right;font-family:var(--mono);">${p.memory_mb} MB</td>
            <td style="text-align:right;font-family:var(--mono);color:var(--muted);">${p.memory_percent}%</td>
            <td><span class="badge ${badgeClass}">${p.status}</span></td>
            <td style="text-align:right;">
              <button type="button" class="button link-button danger" data-kill-pid="${p.pid}" data-proc-name="${p.name}" title="End Process">Kill</button>
            </td>
          </tr>
        `;
      })
      .join("");

    procTbody.innerHTML = rowsHtml || `<tr><td colspan="8" class="muted empty">No matching processes found.</td></tr>`;
  }

  // -------------------------------------------------------------------------
  // Historical Range Selector
  // -------------------------------------------------------------------------

  document.querySelectorAll("[data-range]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      document.querySelectorAll("[data-range]").forEach((b) => b.classList.remove("primary"));
      btn.classList.add("primary");
      activeRange = btn.getAttribute("data-range") || "24h";

      try {
        const res = await fetch(`/monitor/history?range=${encodeURIComponent(activeRange)}`);
        if (res.ok) {
          const data = await res.json();
          historyData = data.history || [];
          renderAllCharts();
        }
      } catch (e) {
        // history fetch error
      }
    });
  });

  // -------------------------------------------------------------------------
  // Process Sorting & Filtering & Killing
  // -------------------------------------------------------------------------

  document.querySelectorAll('input[name="proc_sort"]').forEach((radio) => {
    radio.addEventListener("change", () => {
      currentSort = radio.value;
      fetchLiveMetrics();
    });
  });

  if (procFilter) {
    procFilter.addEventListener("input", () => {
      const term = procFilter.value.trim().toLowerCase();
      procTbody?.querySelectorAll("tr[data-name]").forEach((tr) => {
        const name = tr.getAttribute("data-name")?.toLowerCase() || "";
        tr.style.display = !term || name.includes(term) ? "" : "none";
      });
    });
  }

  document.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-kill-pid]");
    if (!btn) return;
    const pid = btn.getAttribute("data-kill-pid");
    const name = btn.getAttribute("data-proc-name");

    if (!window.confirm(`Are you sure you want to terminate process "${name}" (PID ${pid})?`)) {
      return;
    }

    try {
      const formData = new FormData();
      formData.append("csrf_token", csrfToken);

      const res = await fetch(`/monitor/process/${pid}/kill`, {
        method: "POST",
        body: formData,
      });
      if (res.ok) {
        fetchLiveMetrics();
      } else {
        const err = await res.json();
        alert(`Failed to terminate process: ${err.error || "Unknown error"}`);
      }
    } catch (err) {
      alert(`Error: ${err.message}`);
    }
  });

  if (btnRefresh) {
    btnRefresh.addEventListener("click", fetchLiveMetrics);
  }

  // Poll live metrics every 3 seconds
  setInterval(fetchLiveMetrics, 3000);
})();

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

  function formatTs(ts) {
    if (!ts) return "";
    // ts is "YYYY-MM-DDTHH:MM:SS" (UTC from server)
    const d = new Date(ts + "Z"); // treat as UTC
    const pad = (n) => String(n).padStart(2, "0");
    const months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
    return `${months[d.getMonth()]} ${d.getDate()}, ${d.getFullYear()}  ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }

  function renderSvgChart(svgId, dataKey, colorStroke, colorFill, label) {
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
    const timestamps = historyData.map((d) => d.ts || "");
    const maxVal = Math.max(100, Math.max(...values));
    const minVal = 0;

    const points = values.map((val, idx) => {
      const x = padding + (idx / (values.length - 1 || 1)) * innerW;
      const y = height - padding - ((val - minVal) / (maxVal - minVal || 1)) * innerH;
      return [x, y];
    });

    const pathD = points.map((pt, idx) => (idx === 0 ? `M ${pt[0]} ${pt[1]}` : `L ${pt[0]} ${pt[1]}`)).join(" ");
    const areaD = `${pathD} L ${points[points.length - 1][0]} ${height} L ${points[0][0]} ${height} Z`;

    // Build hit rects + crosshair elements per point
    const hitWidth = Math.max(8, innerW / Math.max(values.length - 1, 1));
    const hitRects = points.map((pt, idx) => {
      const x = Math.max(padding, pt[0] - hitWidth / 2);
      return `<rect class="chart-hit" x="${x}" y="${padding}" width="${hitWidth}" height="${innerH}"
                fill="transparent" data-idx="${idx}" style="cursor:crosshair;"/>`;
    }).join("");

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
      ${hitRects}
      <line id="crosshair-${dataKey}" x1="0" y1="${padding}" x2="0" y2="${height - padding}"
            stroke="${colorStroke}" stroke-width="1" stroke-dasharray="4,3" opacity="0" pointer-events="none"/>
      <circle id="dot-${dataKey}" cx="0" cy="0" r="4" fill="${colorStroke}" stroke="var(--bg)" stroke-width="2"
              opacity="0" pointer-events="none"/>
      <g id="tip-${dataKey}" opacity="0" pointer-events="none">
        <rect id="tip-bg-${dataKey}" rx="5" ry="5" fill="#1a1d23" stroke="${colorStroke}" stroke-width="1" opacity="0.95"/>
        <text id="tip-val-${dataKey}" fill="${colorStroke}" font-size="13" font-weight="700" font-family="var(--mono,monospace)"></text>
        <text id="tip-ts-${dataKey}" fill="#9ca3af" font-size="10" font-family="var(--sans,sans-serif)"></text>
      </g>
    `;

    // Wire up hover events
    svg.querySelectorAll(".chart-hit").forEach((rect) => {
      rect.addEventListener("mouseenter", () => {
        const idx = parseInt(rect.getAttribute("data-idx"), 10);
        const pt = points[idx];
        const val = values[idx];
        const ts = timestamps[idx];

        // Crosshair
        const ch = document.getElementById(`crosshair-${dataKey}`);
        if (ch) { ch.setAttribute("x1", pt[0]); ch.setAttribute("x2", pt[0]); ch.setAttribute("opacity", "1"); }

        // Dot
        const dot = document.getElementById(`dot-${dataKey}`);
        if (dot) { dot.setAttribute("cx", pt[0]); dot.setAttribute("cy", pt[1]); dot.setAttribute("opacity", "1"); }

        // Tooltip
        const tip = document.getElementById(`tip-${dataKey}`);
        const tipBg = document.getElementById(`tip-bg-${dataKey}`);
        const tipVal = document.getElementById(`tip-val-${dataKey}`);
        const tipTs = document.getElementById(`tip-ts-${dataKey}`);
        if (!tip || !tipBg || !tipVal || !tipTs) return;

        const valLabel = `${label}: ${val}%`;
        const tsLabel = formatTs(ts);
        tipVal.textContent = valLabel;
        tipTs.textContent = tsLabel;

        // Measure text widths roughly (chars * px)
        const valW = valLabel.length * 8.5;
        const tsW = tsLabel.length * 6.2;
        const boxW = Math.max(valW, tsW) + 16;
        const boxH = 38;

        // Position: above the dot, flip left if near right edge
        let tx = pt[0] - boxW / 2;
        if (tx + boxW > width - 4) tx = width - boxW - 4;
        if (tx < 4) tx = 4;
        const ty = Math.max(padding + 2, pt[1] - boxH - 10);

        tipBg.setAttribute("x", tx); tipBg.setAttribute("y", ty);
        tipBg.setAttribute("width", boxW); tipBg.setAttribute("height", boxH);

        tipVal.setAttribute("x", tx + 8); tipVal.setAttribute("y", ty + 16);
        tipTs.setAttribute("x", tx + 8); tipTs.setAttribute("y", ty + 30);

        tip.setAttribute("opacity", "1");
      });

      rect.addEventListener("mouseleave", () => {
        document.getElementById(`crosshair-${dataKey}`)?.setAttribute("opacity", "0");
        document.getElementById(`dot-${dataKey}`)?.setAttribute("opacity", "0");
        document.getElementById(`tip-${dataKey}`)?.setAttribute("opacity", "0");
      });
    });
  }


  function renderAllCharts() {
    renderSvgChart("svg-cpu", "cpu", "var(--accent)", "#2563eb", "CPU");
    renderSvgChart("svg-mem", "memory", "var(--warn, #eab308)", "#eab308", "Memory");
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

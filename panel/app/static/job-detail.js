/* Wires SSE and polling for /jobs/<id>.
   Kept as its own file rather than inline because the panel's CSP is
   `script-src 'self'` with no 'unsafe-inline' -- an inline <script> block
   is blocked by the browser!
*/
(function () {
  "use strict";

  const logEl = document.getElementById("log");
  if (!logEl) return;

  const jobId = logEl.getAttribute("data-job-id");
  if (!jobId) return;

  const isLive = logEl.getAttribute("data-live") === "true";
  const returnTo = logEl.getAttribute("data-return-to") || "";
  const returnNotice = logEl.getAttribute("data-notice") || "";
  const badgeEl = document.getElementById("job-badge");
  const statusTextEl = document.getElementById("job-status-text");
  const pulseEl = document.getElementById("job-pulse");
  const timerEl = document.getElementById("job-timer");
  const errorEl = document.getElementById("job-error-msg");
  const statsEl = document.getElementById("log-stats");
  const autoscrollToggle = document.getElementById("autoscroll-toggle");

  let isTerminal = !isLive;
  let sseDoneReceived = false;

  // Track lines in the log viewer
  let initialText = logEl.textContent || "";
  // Split lines (ignoring trailing newline)
  let existingLines = initialText ? initialText.split(/\r?\n/) : [];
  if (existingLines.length > 0 && existingLines[existingLines.length - 1] === "") {
    existingLines.pop();
  }
  let lineCount = existingLines.length;
  let nextOffset = lineCount;

  function updateLineStats() {
    if (statsEl) {
      statsEl.textContent = `${lineCount} line${lineCount === 1 ? '' : 's'}`;
    }
  }
  updateLineStats();

  // Initial scroll to bottom
  if (logEl.scrollHeight > logEl.clientHeight) {
    logEl.scrollTop = logEl.scrollHeight;
  }

  function appendLines(lines) {
    if (!lines || !lines.length) return;
    let textToAppend = "";
    for (const line of lines) {
      textToAppend += line + "\n";
      lineCount++;
    }
    logEl.textContent += textToAppend;
    updateLineStats();
    if (!autoscrollToggle || autoscrollToggle.checked) {
      logEl.scrollTop = logEl.scrollHeight;
    }
  }

  function setFullLog(fullText) {
    if (typeof fullText !== "string") return;
    logEl.textContent = fullText;
    let lines = fullText ? fullText.split(/\r?\n/) : [];
    if (lines.length > 0 && lines[lines.length - 1] === "") lines.pop();
    lineCount = lines.length;
    nextOffset = lineCount;
    updateLineStats();
    if (!autoscrollToggle || autoscrollToggle.checked) {
      logEl.scrollTop = logEl.scrollHeight;
    }
  }

  // On success, hand the browser back to wherever the action was started
  // from (the job's own page stays put on failure so the error is visible).
  // A one-time notice tied to the job -- e.g. a generated password -- rides
  // along on the query string so it still surfaces on the destination page.
  function finish(status) {
    if (status === "success" && returnTo) {
      let url = returnTo;
      if (returnNotice) {
        url += (url.indexOf("?") === -1 ? "?" : "&") + "notice=" + encodeURIComponent(returnNotice);
      }
      window.location.href = url;
      return;
    }
    window.location.reload();
  }

  function markFinished(status, errorMsg) {
    if (isTerminal) return;
    isTerminal = true;
    if (pulseEl) pulseEl.remove();
    if (badgeEl) badgeEl.className = "badge " + status;
    if (statusTextEl) statusTextEl.textContent = status;

    if (errorMsg && errorEl) {
      errorEl.textContent = errorMsg;
      errorEl.style.display = "inline-block";
    }

    if (pollTimer) clearInterval(pollTimer);
    if (clockTimer) clearInterval(clockTimer);
  }

  if (!isLive) {
    // The job had already finished by the time this page loaded (common for
    // fast jobs like cron.sync) -- the SSE/poll "done" handlers below never
    // fire, so hand off to return_to here instead.
    const initialStatus = logEl.getAttribute("data-status") || "";
    if (initialStatus === "success" && returnTo) {
      finish(initialStatus);
    }
    return;
  }

  // --- 1. Live SSE Stream ---
  let source = null;
  try {
    source = new EventSource("/jobs/" + jobId + "/stream");

    source.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.lines && data.lines.length > 0) {
          appendLines(data.lines);
          nextOffset += data.lines.length;
        }
      } catch (err) {
        console.error("SSE parse error", err);
      }
    };

    source.addEventListener("done", (event) => {
      sseDoneReceived = true;
      let status = "success";
      try {
        const data = JSON.parse(event.data);
        status = data.status || "success";
      } catch (e) {}
      markFinished(status);
      if (source) source.close();
      setTimeout(() => finish(status), 500);
    });

    source.onerror = () => {
      // Browser reconnects SSE automatically; fallback poll ensures continuous updates meanwhile
    };
  } catch (err) {
    console.warn("EventSource setup failed", err);
  }

  // --- 2. Live Polling Fallback & Status Sync (every 2.5s) ---
  const pollTimer = setInterval(async () => {
    if (isTerminal) return;
    try {
      const res = await fetch("/jobs/" + jobId + "/poll?offset=" + nextOffset, {
        headers: { "Accept": "application/json" }
      });
      if (!res.ok) return;
      const data = await res.json();

      if (data.status && badgeEl && statusTextEl) {
        badgeEl.className = "badge " + data.status;
        statusTextEl.textContent = data.status;
      }

      if (data.lines && data.lines.length > 0) {
        appendLines(data.lines);
        nextOffset = data.next_offset || (nextOffset + data.lines.length);
      } else if (data.is_terminal && data.log && lineCount === 0) {
        setFullLog(data.log);
      }

      if (data.is_terminal) {
        markFinished(data.status, data.error);
        if (source) source.close();
        setTimeout(() => finish(data.status), 500);
      }
    } catch (e) {
      // Ignore network hiccups during poll
    }
  }, 2500);

  // --- 3. Live Elapsed Time Counter ---
  const startedAttr = timerEl ? timerEl.getAttribute("data-started") : "";
  const startTime = startedAttr ? new Date(startedAttr).getTime() : Date.now();

  function formatDuration(ms) {
    const totalSec = Math.max(0, Math.floor(ms / 1000));
    const mins = Math.floor(totalSec / 60);
    const secs = totalSec % 60;
    if (mins > 0) {
      return `${mins}m ${secs}s`;
    }
    return `${secs}s`;
  }

  const clockTimer = setInterval(() => {
    if (isTerminal) return;
    const elapsed = Date.now() - startTime;
    if (timerEl) {
      timerEl.textContent = `Running for ${formatDuration(elapsed)}`;
    }
  }, 1000);

})();

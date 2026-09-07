/* Wires xterm.js to /terminal/ws. Kept as its own file rather than inline
   because the panel's CSP is script-src 'self' with no 'unsafe-inline' --
   an inline <script> block would simply be silently blocked. */
(function () {
  "use strict";

  const container = document.getElementById("terminal-container");
  const banner = document.getElementById("terminal-banner");

  const term = new Terminal({
    cursorBlink: true,
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
    fontSize: 13,
    theme: { background: "#0d0f12" },
  });
  const fitAddon = new FitAddon.FitAddon();
  term.loadAddon(fitAddon);
  term.open(container);
  fitAddon.fit();

  function showBanner(html) {
    banner.innerHTML = html;
    banner.classList.add("visible");
  }

  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(protocol + "//" + window.location.host + "/terminal/ws");
  socket.binaryType = "arraybuffer";

  socket.addEventListener("open", () => {
    sendResize();
  });

  socket.addEventListener("message", (event) => {
    if (typeof event.data === "string") {
      try {
        const message = JSON.parse(event.data);
        if (message.type === "timeout") {
          showBanner('Session timed out from inactivity.<br><a href="/terminal">Reload to start a new one</a>');
          socket.close();
        }
      } catch (err) {
        // Not JSON -- ignore.
      }
      return;
    }
    term.write(new Uint8Array(event.data));
  });

  socket.addEventListener("close", () => {
    if (!banner.classList.contains("visible")) {
      showBanner('Terminal session ended.<br><a href="/terminal">Reload to start a new one</a>');
    }
  });

  socket.addEventListener("error", () => {
    showBanner('Could not connect to the terminal.<br><a href="/terminal">Try again</a>');
  });

  term.onData((data) => {
    if (socket.readyState === WebSocket.OPEN) {
      socket.send(new TextEncoder().encode(data));
    }
  });

  function sendResize() {
    if (socket.readyState !== WebSocket.OPEN) return;
    socket.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
  }

  let resizeTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      fitAddon.fit();
      sendResize();
    }, 100);
  });

  term.focus();
})();

/* DNS propagation check on the site detail page: fetched on click only, so
   the page load itself never waits on a dozen outbound DNS lookups.
   CSP is script-src 'self' with no 'unsafe-inline', hence its own file.
*/
(function () {
  "use strict";

  const panel = document.getElementById("dns-check-panel");
  if (!panel) return;

  const siteId = panel.getAttribute("data-site-id");
  const btn = document.getElementById("btn-check-dns");
  const resultEl = document.getElementById("dns-check-result");

  function escapeHtml(value) {
    const div = document.createElement("div");
    div.textContent = value == null ? "" : String(value);
    return div.innerHTML;
  }

  function render(data) {
    const expected = data.expected_ip;
    let html = "";

    if (expected) {
      html += `<p class="muted" style="margin-bottom:10px;">This server's public IP: <strong>${escapeHtml(expected)}</strong></p>`;
    } else {
      html += `<p class="muted" style="margin-bottom:10px;">Could not determine this server's own public IP, so results below aren't marked as matching or not.</p>`;
    }

    html += `<table class="table"><thead><tr>
      <th>Resolver</th><th>Network</th><th>Answer</th><th>Status</th>
    </tr></thead><tbody>`;

    for (const r of data.resolvers) {
      let status;
      if (r.error) {
        status = `<span class="badge failed">${escapeHtml(r.error)}</span>`;
      } else if (r.matches === true) {
        status = `<span class="badge success">Matches</span>`;
      } else if (r.matches === false) {
        status = `<span class="badge failed">Different IP</span>`;
      } else {
        status = `<span class="muted">—</span>`;
      }
      const answer = r.answers && r.answers.length ? r.answers.join(", ") : "—";
      html += `<tr>
        <td>${escapeHtml(r.label)}</td>
        <td class="muted">${escapeHtml(r.region)}</td>
        <td>${escapeHtml(answer)}</td>
        <td>${status}</td>
      </tr>`;
    }

    html += "</tbody></table>";
    html += `<p class="muted" style="margin-top:10px;font-size:13px;">Each row is an independent public resolver -- this shows whether that resolver has already picked up the current record, not a simulation of every country's local ISP.</p>`;
    resultEl.innerHTML = html;
  }

  btn.addEventListener("click", async () => {
    btn.disabled = true;
    const originalLabel = btn.textContent;
    btn.textContent = "Checking…";
    resultEl.innerHTML = `<p class="muted">Querying public resolvers…</p>`;

    try {
      const res = await fetch(`/sites/${encodeURIComponent(siteId)}/dns-check`, {
        headers: { "Accept": "application/json" },
      });
      const data = await res.json();
      if (!res.ok) {
        resultEl.innerHTML = `<p class="muted">${escapeHtml(data.error || "Check failed.")}</p>`;
      } else {
        render(data);
      }
    } catch (err) {
      resultEl.innerHTML = `<p class="muted">Network error while checking DNS.</p>`;
    } finally {
      btn.disabled = false;
      btn.textContent = originalLabel;
    }
  });
})();

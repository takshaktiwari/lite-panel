/* The file manager's upload modal: queues one or more files, uploads each
   in fixed-size chunks (so a large file never rides on one giant request),
   shows a progress bar per file, and guards against losing an in-progress
   upload if the dialog or tab is closed early. */
(function () {
  "use strict";

  const CHUNK_SIZE = 8 * 1024 * 1024; // 8MB
  const MAX_CONCURRENT_UPLOADS = 2;
  const MAX_CHUNK_ATTEMPTS = 3;

  const dialog = document.getElementById("upload-dialog");
  const fileInput = document.getElementById("upload-file-input");
  const queueList = document.getElementById("upload-queue");
  const openButton = document.getElementById("open-upload-dialog");
  const closeButton = document.getElementById("upload-dialog-close");
  const bulkForm = document.getElementById("bulk-form");

  const conflictDialog = document.getElementById("upload-conflict-dialog");
  const conflictNameEl = document.getElementById("upload-conflict-name");
  const conflictCancelBtn = document.getElementById("upload-conflict-cancel");
  const conflictRenameBtn = document.getElementById("upload-conflict-rename");
  const conflictReplaceBtn = document.getElementById("upload-conflict-replace");

  if (!dialog || !fileInput || !queueList || !openButton || !closeButton || !bulkForm) return;

  const csrfToken = () => bulkForm.elements.csrf_token.value;
  const currentPath = () => bulkForm.elements.path.value;

  const SETTLED = new Set(["done", "failed", "cancelled"]);
  const rows = [];
  const pending = [];
  let activeCount = 0;

  async function nameExists(filename) {
    const res = await fetch(
      `/files/upload-check?path=${encodeURIComponent(currentPath())}&filename=${encodeURIComponent(filename)}`,
      { headers: { "Accept": "application/json" } }
    );
    if (!res.ok) return false; // let init_upload surface the real error instead
    const body = await res.json();
    return !!body.exists;
  }

  // Two uploads can run at once (MAX_CONCURRENT_UPLOADS), but the conflict
  // dialog is a single shared element -- this chain makes a second row's
  // prompt wait for the first one's to be dismissed instead of clobbering it.
  let conflictQueue = Promise.resolve();

  function askConflict(filename) {
    const run = () =>
      new Promise((resolve) => {
        conflictNameEl.textContent = filename;
        let settled = false;
        const finish = (choice) => {
          if (settled) return;
          settled = true;
          conflictDialog.removeEventListener("close", onClose);
          conflictReplaceBtn.removeEventListener("click", onReplace);
          conflictRenameBtn.removeEventListener("click", onRename);
          conflictCancelBtn.removeEventListener("click", onCancel);
          conflictDialog.close();
          resolve(choice);
        };
        const onClose = () => finish("cancel");
        const onReplace = () => finish("replace");
        const onRename = () => finish("rename");
        const onCancel = () => finish("cancel");
        conflictDialog.addEventListener("close", onClose);
        conflictReplaceBtn.addEventListener("click", onReplace);
        conflictRenameBtn.addEventListener("click", onRename);
        conflictCancelBtn.addEventListener("click", onCancel);
        conflictDialog.showModal();
      });
    const result = conflictQueue.then(run);
    conflictQueue = result.catch(() => {});
    return result;
  }

  openButton.addEventListener("click", () => dialog.showModal());

  fileInput.addEventListener("change", (event) => {
    Array.from(event.target.files).forEach(enqueue);
    event.target.value = ""; // allow picking the same file again later
  });

  function enqueue(file) {
    const row = { file, status: "waiting", uploadId: null, cancelled: false };
    buildRow(row);
    rows.push(row);
    pending.push(row);
    fillSlots();
  }

  function buildRow(row) {
    const li = document.createElement("li");
    li.className = "upload-row";

    const main = document.createElement("div");
    main.className = "upload-row-main";

    const name = document.createElement("span");
    name.className = "upload-row-name";
    name.textContent = row.file.name;

    const status = document.createElement("span");
    status.className = "upload-row-status";
    status.textContent = "Waiting…";

    const progress = document.createElement("progress");
    progress.max = 100;
    progress.value = 0;

    main.append(name, status, progress);

    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "upload-row-cancel";
    cancel.textContent = "✕";
    cancel.title = "Cancel upload";
    cancel.setAttribute("aria-label", "Cancel upload");
    cancel.addEventListener("click", () => cancelRow(row));

    li.append(main, cancel);
    queueList.appendChild(li);

    row.nameEl = name;
    row.statusEl = status;
    row.progressEl = progress;
    row.cancelEl = cancel;
  }

  function fillSlots() {
    while (activeCount < MAX_CONCURRENT_UPLOADS && pending.length > 0) {
      const row = pending.shift();
      if (row.cancelled) continue;
      activeCount++;
      runUpload(row).finally(() => {
        activeCount--;
        fillSlots();
        maybeReload();
      });
    }
  }

  async function runUpload(row) {
    row.status = "uploading";
    setStatus(row, "Checking…");
    try {
      let overwrite = true;
      if (await nameExists(row.file.name)) {
        const choice = await askConflict(row.file.name);
        if (row.cancelled) throw new Error("cancelled");
        if (choice === "cancel") {
          row.status = "cancelled";
          setStatus(row, "Skipped");
          row.cancelEl.disabled = true;
          return;
        }
        overwrite = choice === "replace";
      }

      setStatus(row, "Starting…");
      const initRes = await fetch("/files/chunk-upload/init", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
        body: JSON.stringify({
          path: currentPath(),
          filename: row.file.name,
          size: row.file.size,
          overwrite,
        }),
      });
      const initBody = await initRes.json();
      if (!initRes.ok) throw new Error(initBody.error || "Could not start upload.");
      row.uploadId = initBody.upload_id;
      if (initBody.final_name && initBody.final_name !== row.file.name) {
        row.nameEl.textContent = initBody.final_name;
      }

      const totalChunks = Math.max(1, Math.ceil(row.file.size / CHUNK_SIZE));
      for (let index = 0; index < totalChunks; index++) {
        if (row.cancelled) throw new Error("cancelled");
        const start = index * CHUNK_SIZE;
        const blob = row.file.slice(start, Math.min(start + CHUNK_SIZE, row.file.size));
        await uploadChunk(row, index, blob);
        updateProgress(row, Math.min(start + blob.size, row.file.size));
      }
      if (row.cancelled) throw new Error("cancelled");

      const completeForm = new FormData();
      completeForm.append("csrf_token", csrfToken());
      const completeRes = await fetch(`/files/chunk-upload/${row.uploadId}/complete`, {
        method: "POST",
        body: completeForm,
      });
      const completeBody = await completeRes.json();
      if (!completeRes.ok) throw new Error(completeBody.error || "Could not finish upload.");

      row.status = "done";
      setStatus(row, "Done");
      row.cancelEl.disabled = true;
    } catch (err) {
      if (row.cancelled) {
        row.status = "cancelled";
        setStatus(row, "Cancelled");
      } else {
        row.status = "failed";
        setStatus(row, err.message || "Upload failed.", true);
      }
      row.cancelEl.disabled = true;
      if (row.uploadId) abortUpload(row.uploadId);
    }
  }

  async function uploadChunk(row, index, blob, attempt = 1) {
    const form = new FormData();
    form.append("index", String(index));
    form.append("chunk", blob);
    form.append("csrf_token", csrfToken());
    const res = await fetch(`/files/chunk-upload/${row.uploadId}/chunk`, { method: "POST", body: form });
    if (res.ok) return;
    if (attempt >= MAX_CHUNK_ATTEMPTS) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.error || "Upload failed after retrying.");
    }
    await new Promise((resolve) => setTimeout(resolve, 500 * attempt));
    return uploadChunk(row, index, blob, attempt + 1);
  }

  function abortUpload(uploadId) {
    const form = new FormData();
    form.append("csrf_token", csrfToken());
    fetch(`/files/chunk-upload/${uploadId}/abort`, { method: "POST", body: form, keepalive: true });
  }

  function cancelRow(row) {
    if (SETTLED.has(row.status)) return;
    row.cancelled = true;
    if (row.status === "waiting") {
      // Never started -- nothing to abort server-side, just mark it done.
      row.status = "cancelled";
      setStatus(row, "Cancelled");
      row.cancelEl.disabled = true;
    }
    // If it's mid-upload, runUpload()'s own loop notices row.cancelled and
    // unwinds itself (including the abort call) on its next check.
  }

  function setStatus(row, text, isError) {
    row.statusEl.textContent = text;
    row.statusEl.classList.toggle("failed", !!isError);
  }

  function updateProgress(row, sentBytes) {
    const pct = row.file.size === 0 ? 100 : Math.round((sentBytes / row.file.size) * 100);
    row.progressEl.value = pct;
    setStatus(row, `Uploading… ${pct}%`);
  }

  function anyInFlight() {
    return rows.some((row) => !SETTLED.has(row.status));
  }

  function maybeReload() {
    if (activeCount === 0 && pending.length === 0 && rows.some((row) => row.status === "done")) {
      // Fully server-rendered listing -- reload to show the new file(s),
      // same as every other file-manager action (POST -> redirect -> fresh
      // render).
      window.location.reload();
    }
  }

  function requestClose() {
    if (!anyInFlight()) return true;
    if (!window.confirm("Uploads are still in progress — closing will cancel them. Close anyway?")) {
      return false;
    }
    rows.forEach((row) => {
      if (!SETTLED.has(row.status)) cancelRow(row);
    });
    return true;
  }

  closeButton.addEventListener("click", () => {
    if (requestClose()) dialog.close();
  });

  // The native Esc-to-close path fires a cancelable "cancel" event.
  dialog.addEventListener("cancel", (event) => {
    if (!requestClose()) event.preventDefault();
  });

  window.addEventListener("beforeunload", (event) => {
    if (anyInFlight()) {
      event.preventDefault();
      event.returnValue = "";
    }
  });
})();

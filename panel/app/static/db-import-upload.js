/* Import Database Dump modal: uploads the .sql/.sql.gz file in fixed-size
   chunks -- the same strategy file-upload.js uses for the file manager, so a
   multi-gigabyte dump never rides on one giant request -- with a progress
   bar for the upload itself. Once every chunk has landed, the browser is
   handed off to the import job's own page (job-detail.js), which already
   shows a live progress bar and log for the "applying it to MySQL" phase.
*/
(function () {
  "use strict";

  const CHUNK_SIZE = 8 * 1024 * 1024; // 8MB
  const MAX_CHUNK_ATTEMPTS = 3;

  const dialog = document.getElementById("dlg-import");
  const form = document.getElementById("form-import");
  const fileInput = document.getElementById("import-file");
  const pickerWrap = document.getElementById("import-picker-wrap");
  const queueList = document.getElementById("import-upload-queue");
  const errorEl = document.getElementById("import-error");
  const submitBtn = document.getElementById("btn-submit-import");

  if (!dialog || !form || !fileInput || !queueList || !submitBtn) return;

  const csrfToken = () => form.elements.csrf_token.value;

  let uploadId = null;
  let inFlight = false;
  let row = null;

  function resetUI() {
    uploadId = null;
    inFlight = false;
    row = null;
    fileInput.value = "";
    fileInput.disabled = false;
    pickerWrap.hidden = false;
    queueList.hidden = true;
    queueList.innerHTML = "";
    submitBtn.disabled = false;
    submitBtn.textContent = "Start import";
    hideError();
  }

  function showError(message) {
    errorEl.textContent = message;
    errorEl.style.display = "block";
  }

  function hideError() {
    errorEl.style.display = "none";
    errorEl.textContent = "";
  }

  function buildRow(filename) {
    const li = document.createElement("li");
    li.className = "upload-row";

    const main = document.createElement("div");
    main.className = "upload-row-main";

    const name = document.createElement("span");
    name.className = "upload-row-name";
    name.textContent = filename;

    const status = document.createElement("span");
    status.className = "upload-row-status";
    status.textContent = "Starting…";

    const progress = document.createElement("progress");
    progress.max = 100;
    progress.value = 0;

    main.append(name, status, progress);
    li.append(main);
    queueList.innerHTML = "";
    queueList.hidden = false;
    queueList.appendChild(li);

    return { statusEl: status, progressEl: progress };
  }

  function setStatus(text, isError) {
    if (!row) return;
    row.statusEl.textContent = text;
    row.statusEl.classList.toggle("failed", !!isError);
  }

  function updateProgress(sentBytes, totalBytes) {
    if (!row) return;
    const pct = totalBytes === 0 ? 100 : Math.round((sentBytes / totalBytes) * 100);
    row.progressEl.value = pct;
    setStatus(`Uploading… ${pct}%`);
  }

  window.addEventListener("db-import:open", resetUI);

  submitBtn.addEventListener("click", () => {
    if (inFlight) return;
    const file = fileInput.files[0];
    if (!file) {
      showError("Choose a dump file first.");
      return;
    }
    const lower = file.name.toLowerCase();
    if (!lower.endsWith(".sql") && !lower.endsWith(".sql.gz") && !lower.endsWith(".gz")) {
      showError("Only .sql and .sql.gz dump files are supported.");
      return;
    }
    runUpload(file);
  });

  async function runUpload(file) {
    inFlight = true;
    hideError();
    fileInput.disabled = true;
    pickerWrap.hidden = true;
    submitBtn.disabled = true;
    row = buildRow(file.name);

    const dbId = dialog.dataset.dbId;
    const dbName = dialog.dataset.dbName || "";

    try {
      const initRes = await fetch(`/databases/${dbId}/import/chunk-upload/init`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
        body: JSON.stringify({ filename: file.name, size: file.size }),
      });
      const initBody = await initRes.json();
      if (!initRes.ok) throw new Error(initBody.error || "Could not start upload.");
      uploadId = initBody.upload_id;

      const totalChunks = Math.max(1, Math.ceil(file.size / CHUNK_SIZE));
      for (let index = 0; index < totalChunks; index++) {
        const start = index * CHUNK_SIZE;
        const blob = file.slice(start, Math.min(start + CHUNK_SIZE, file.size));
        await uploadChunk(dbId, index, blob);
        updateProgress(Math.min(start + blob.size, file.size), file.size);
      }

      setStatus("Upload complete — starting import…");
      const completeForm = new FormData();
      completeForm.append("csrf_token", csrfToken());
      const completeRes = await fetch(`/databases/${dbId}/import/chunk-upload/${uploadId}/complete`, {
        method: "POST",
        body: completeForm,
      });
      const completeBody = await completeRes.json();
      if (!completeRes.ok) throw new Error(completeBody.error || "Could not finish upload.");

      const returnTo = window.location.pathname + window.location.search;
      const notice = `Imported ${file.name} into ${dbName}`;
      window.location.href =
        `/jobs/${completeBody.job_id}?return_to=${encodeURIComponent(returnTo)}&notice=${encodeURIComponent(notice)}`;
    } catch (err) {
      inFlight = false;
      setStatus(err.message || "Upload failed.", true);
      showError(err.message || "Upload failed.");
      fileInput.disabled = false;
      pickerWrap.hidden = false;
      submitBtn.disabled = false;
      if (uploadId) abortUpload(dbId, uploadId);
    }
  }

  async function uploadChunk(dbId, index, blob, attempt = 1) {
    const chunkForm = new FormData();
    chunkForm.append("index", String(index));
    chunkForm.append("chunk", blob);
    chunkForm.append("csrf_token", csrfToken());
    const res = await fetch(`/databases/${dbId}/import/chunk-upload/${uploadId}/chunk`, {
      method: "POST",
      body: chunkForm,
    });
    if (res.ok) return;
    if (attempt >= MAX_CHUNK_ATTEMPTS) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.error || "Upload failed after retrying.");
    }
    await new Promise((resolve) => setTimeout(resolve, 500 * attempt));
    return uploadChunk(dbId, index, blob, attempt + 1);
  }

  function abortUpload(dbId, id) {
    const abortForm = new FormData();
    abortForm.append("csrf_token", csrfToken());
    fetch(`/databases/${dbId}/import/chunk-upload/${id}/abort`, {
      method: "POST",
      body: abortForm,
      keepalive: true,
    });
  }

  // Closing the dialog mid-upload (Cancel button or Esc) should not leave an
  // orphaned session on the server -- best-effort cleanup, no confirmation
  // prompt: this is a single file, not a queue with work to lose.
  dialog.addEventListener("close", () => {
    if (inFlight && uploadId) abortUpload(dialog.dataset.dbId, uploadId);
    inFlight = false;
  });
})();

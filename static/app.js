(() => {
  const form = document.getElementById("search-form");
  const submitBtn = document.getElementById("submit-btn");
  const statusLine = document.getElementById("status-line");
  const resultsCard = document.getElementById("results-card");
  const resultsBody = document.getElementById("results-body");
  const summaryEl = document.getElementById("summary");
  const exportCsvBtn = document.getElementById("export-csv");
  const exportJsonBtn = document.getElementById("export-json");
  const validEmailsBox = document.getElementById("valid-emails");
  const validEmailsTitle = document.getElementById("valid-emails-title");
  const validEmailsList = document.getElementById("valid-emails-list");
  const copyValidBtn = document.getElementById("copy-valid");

  const companyInput = document.getElementById("company");
  const domainInput = document.getElementById("domain");

  let currentSource = null;
  let rows = {}; // email -> {email, pattern, status, detail, smtp_code}
  let rowOrder = [];

  const STATUS_LABEL = {
    pending: "checking…",
    not_checked: "not checked",
    valid: "valid",
    invalid: "invalid",
    unknown: "unknown",
    catch_all_domain: "catch-all",
    error: "error",
    skipped: "skipped",
  };

  // Best-effort auto-fill of domain from company name, without clobbering
  // a domain the user already typed themselves.
  let domainTouchedByUser = false;
  domainInput.addEventListener("input", () => { domainTouchedByUser = true; });

  let guessTimer = null;
  companyInput.addEventListener("input", () => {
    clearTimeout(guessTimer);
    if (domainTouchedByUser && domainInput.value.trim()) return;
    const company = companyInput.value.trim();
    if (!company) return;
    guessTimer = setTimeout(async () => {
      try {
        const res = await fetch(`/api/guess-domain?company=${encodeURIComponent(company)}`);
        const data = await res.json();
        if (data.domain && !domainTouchedByUser) {
          domainInput.value = data.domain;
        }
      } catch (e) { /* ignore, non-critical */ }
    }, 400);
  });

  const GROUP_LABELS = {
    personal: (domain) => `Name guesses @ ${domain}`,
    role: (domain) => `Generic addresses @ ${domain}`,
    "alt-domain": (domain) => `Long shot @ ${domain}`,
  };

  function groupKey(row) {
    const domain = row.email.split("@")[1] || "";
    return row.tier === "alt-domain" ? `alt:${domain}` : (row.tier || "personal");
  }

  function groupLabel(row) {
    const domain = row.email.split("@")[1] || "";
    const fn = GROUP_LABELS[row.tier] || GROUP_LABELS.personal;
    return fn(domain);
  }

  function rowHtml(row) {
    const badgeClass = row.status in STATUS_LABEL ? row.status : "pending";
    const label = STATUS_LABEL[row.status] || row.status;
    const trClass = row.status === "valid" ? "row-valid" : "";
    return `<tr data-email="${escapeHtml(row.email)}" class="${trClass}">
      <td><span class="badge ${badgeClass}">${label}</span></td>
      <td class="email-cell">${escapeHtml(row.email)}</td>
      <td>${escapeHtml(row.pattern || "")}</td>
      <td class="detail-cell">${escapeHtml(row.detail || "")}</td>
    </tr>`;
  }

  function groupRowHtml(row) {
    return `<tr class="group-row"><td colspan="4">${escapeHtml(groupLabel(row))}</td></tr>`;
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function renderAll() {
    let html = "";
    let lastGroup = null;
    rowOrder.forEach((email) => {
      const row = rows[email];
      const gk = groupKey(row);
      if (gk !== lastGroup) {
        html += groupRowHtml(row);
        lastGroup = gk;
      }
      html += rowHtml(row);
    });
    resultsBody.innerHTML = html;
    updateSummary();
  }

  function updateRow(email, patch) {
    rows[email] = { ...rows[email], ...patch };
    const tr = resultsBody.querySelector(`tr[data-email="${cssEscape(email)}"]`);
    if (tr) {
      tr.outerHTML = rowHtml(rows[email]);
    }
    updateSummary();
  }

  function cssEscape(s) {
    return s.replace(/["\\]/g, "\\$&");
  }

  function updateSummary() {
    const counts = {};
    rowOrder.forEach((email) => {
      const s = rows[email].status;
      counts[s] = (counts[s] || 0) + 1;
    });
    const parts = [];
    if (counts.valid) parts.push(`${counts.valid} valid`);
    if (counts.catch_all_domain) parts.push(`${counts.catch_all_domain} catch-all`);
    if (counts.unknown) parts.push(`${counts.unknown} unknown`);
    if (counts.invalid) parts.push(`${counts.invalid} invalid`);
    if (counts.error) parts.push(`${counts.error} error`);
    summaryEl.textContent = parts.length ? parts.join(" · ") : `${rowOrder.length} candidates`;
  }

  function setBusy(busy) {
    submitBtn.disabled = busy;
    submitBtn.textContent = busy ? "Searching…" : "Find emails";
  }

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    if (currentSource) {
      currentSource.close();
      currentSource = null;
    }

    const params = new URLSearchParams({
      first: document.getElementById("first").value.trim(),
      last: document.getElementById("last").value.trim(),
      domain: domainInput.value.trim(),
      verify: document.getElementById("verify").checked ? "1" : "0",
      stop_on_valid: document.getElementById("stop_on_valid").checked ? "1" : "0",
      delay: document.getElementById("delay").value || "1.5",
      timeout: document.getElementById("timeout").value || "10",
      helo_domain: document.getElementById("helo_domain").value.trim(),
      include_roles: document.getElementById("include_roles").checked ? "1" : "0",
      include_alt_tlds: document.getElementById("include_alt_tlds").checked ? "1" : "0",
      alt_tlds: document.getElementById("alt_tlds").value.trim() || "fr,co,us",
      max_concurrent_domains: document.getElementById("max_concurrent_domains").value || "4",
    });

    rows = {};
    rowOrder = [];
    resultsBody.innerHTML = "";
    resultsCard.classList.remove("hidden");
    exportCsvBtn.disabled = true;
    exportJsonBtn.disabled = true;
    statusLine.textContent = "";
    validEmailsBox.classList.add("hidden");
    validEmailsList.innerHTML = "";
    setBusy(true);

    const source = new EventSource(`/api/search?${params.toString()}`);
    currentSource = source;

    source.addEventListener("candidates", (ev) => {
      const data = JSON.parse(ev.data);
      const verifying = document.getElementById("verify").checked;
      data.candidates.forEach((c) => {
        rows[c.email] = {
          email: c.email,
          pattern: c.pattern,
          tier: c.tier,
          status: verifying ? "pending" : "not_checked",
          detail: "",
        };
        rowOrder.push(c.email);
      });
      renderAll();
      statusLine.textContent = verifying
        ? `Checking ${rowOrder.length} candidates…`
        : `Generated ${rowOrder.length} candidates (not verified).`;
    });

    source.addEventListener("result", (ev) => {
      const data = JSON.parse(ev.data);
      const detail = data.detail || (data.smtp_code ? `SMTP ${data.smtp_code}` : "");
      updateRow(data.email, { status: data.status, detail, smtp_code: data.smtp_code });
    });

    source.addEventListener("done", (ev) => {
      statusLine.textContent = "Done.";
      setBusy(false);
      exportCsvBtn.disabled = rowOrder.length === 0;
      exportJsonBtn.disabled = rowOrder.length === 0;

      const data = ev.data ? JSON.parse(ev.data) : {};
      if (data.verified) {
        showValidEmails();
      }

      source.close();
      currentSource = null;
    });

    source.addEventListener("error", () => {
      statusLine.textContent = "Connection to server lost or search failed.";
      setBusy(false);
      source.close();
      currentSource = null;
    });
  });

  function showValidEmails() {
    const valid = rowOrder.filter((email) => rows[email].status === "valid");

    validEmailsBox.classList.remove("hidden");

    if (valid.length === 0) {
      validEmailsBox.classList.add("empty");
      validEmailsTitle.textContent = "No valid emails confirmed";
      validEmailsList.innerHTML = `<p class="valid-emails-empty-msg">None of the candidates could be confirmed as deliverable. Check the full results below for catch-all/unknown/error entries — they may still be worth trying manually.</p>`;
      copyValidBtn.classList.add("hidden");
      return;
    }

    validEmailsBox.classList.remove("empty");
    validEmailsTitle.textContent = `${valid.length} valid email${valid.length > 1 ? "s" : ""} found`;
    copyValidBtn.classList.remove("hidden");
    validEmailsList.innerHTML = valid
      .map((email) => `<li>${escapeHtml(email)} <span class="via">(${escapeHtml(rows[email].pattern || "")})</span></li>`)
      .join("");
  }

  copyValidBtn.addEventListener("click", async () => {
    const valid = rowOrder.filter((email) => rows[email].status === "valid");
    const text = valid.join("\n");
    try {
      await navigator.clipboard.writeText(text);
      const original = copyValidBtn.textContent;
      copyValidBtn.textContent = "Copied!";
      setTimeout(() => { copyValidBtn.textContent = original; }, 1500);
    } catch (e) {
      downloadBlob(text, "valid-emails.txt", "text/plain");
    }
  });

  function downloadBlob(content, filename, type) {
    const blob = new Blob([content], { type });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
  }

  exportCsvBtn.addEventListener("click", () => {
    const header = "email,pattern,status,detail";
    const lines = rowOrder.map((email) => {
      const r = rows[email];
      const esc = (v) => `"${String(v || "").replace(/"/g, '""')}"`;
      return [esc(r.email), esc(r.pattern), esc(r.status), esc(r.detail)].join(",");
    });
    downloadBlob([header, ...lines].join("\n"), "email-finder-results.csv", "text/csv");
  });

  exportJsonBtn.addEventListener("click", () => {
    const data = rowOrder.map((email) => rows[email]);
    downloadBlob(JSON.stringify(data, null, 2), "email-finder-results.json", "application/json");
  });
})();

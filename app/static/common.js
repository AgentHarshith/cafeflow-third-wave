/* Shared helpers for the customer app and staff dashboard. No secrets live here. */
(function () {
  const escapeHtml = (value) =>
    String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const inr = (n) => "₹" + Number(n).toLocaleString("en-IN");

  async function api(path, options = {}) {
    const init = { headers: { "Content-Type": "application/json" }, ...options };
    if (init.body && typeof init.body !== "string") init.body = JSON.stringify(init.body);
    let res;
    try {
      res = await fetch(path, init);
    } catch {
      throw new Error("Can't reach the CaféFlow server. Check your connection and try again.");
    }
    let data = null;
    try { data = await res.json(); } catch { /* non-JSON */ }
    if (!res.ok) {
      const detail = data && (typeof data.detail === "string" ? data.detail : null);
      const err = new Error(detail || `Request failed (${res.status}).`);
      err.status = res.status;
      throw err;
    }
    return data;
  }

  // Minimal, safe markdown: escape first, then bold/italics/lists/paragraphs.
  function renderMarkdown(text) {
    const inline = (s) =>
      escapeHtml(s)
        .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
        .replace(/(^|[^*])\*(?!\s)(.+?)\*(?!\*)/g, "$1<em>$2</em>");
    const blocks = String(text || "").trim().split(/\n{2,}/);
    return blocks
      .map((block) => {
        const lines = block.split("\n");
        if (lines.every((l) => /^\s*[-*•]\s+/.test(l))) {
          return "<ul>" + lines.map((l) => `<li>${inline(l.replace(/^\s*[-*•]\s+/, ""))}</li>`).join("") + "</ul>";
        }
        if (lines.every((l) => /^\s*\d+[.)]\s+/.test(l))) {
          return "<ol>" + lines.map((l) => `<li>${inline(l.replace(/^\s*\d+[.)]\s+/, ""))}</li>`).join("") + "</ol>";
        }
        // Mixed block: a lead-in line followed by bullets.
        const firstBullet = lines.findIndex((l) => /^\s*[-*•]\s+/.test(l));
        if (firstBullet > 0 && lines.slice(firstBullet).every((l) => /^\s*[-*•]\s+/.test(l))) {
          return `<p>${lines.slice(0, firstBullet).map(inline).join("<br>")}</p><ul>` +
            lines.slice(firstBullet).map((l) => `<li>${inline(l.replace(/^\s*[-*•]\s+/, ""))}</li>`).join("") + "</ul>";
        }
        return `<p>${lines.map(inline).join("<br>")}</p>`;
      })
      .join("");
  }

  const SHORT_LABELS = { connected: "Gemini live", demo: "Demo mode", error: "AI offline", checking: "Checking…" };

  function setStatusPill(el, state, label, title) {
    if (!el) return;
    el.className = "status-pill status-" + state;
    el.querySelector(".label").textContent = label;
    el.dataset.short = SHORT_LABELS[state] || label;
    el.title = title || label;
  }

  function describeGemini(gemini) {
    switch (gemini?.state) {
      case "connected":
        return ["connected", "Gemini connected", `Live Gemini requests succeeding · model ${gemini.model}`];
      case "not_configured":
        return ["demo", "Demo mode", "GEMINI_API_KEY is not configured — using rule-based fallback on the real menu."];
      case "error":
        return ["error", "Gemini unavailable", (gemini.detail || "Gemini request failed") + " Using demo fallback."];
      default:
        return ["checking", "Checking AI…", "Verifying the Gemini connection"];
    }
  }

  const timeOf = (iso) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

  window.CF = { escapeHtml, inr, api, renderMarkdown, setStatusPill, describeGemini, timeOf };
})();

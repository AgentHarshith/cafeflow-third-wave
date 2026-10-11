/* CaféFlow AI — staff dashboard: orders (with delay alerts), menu prices, and the "Room". */
(function () {
  const { escapeHtml, inr, api, timeOf } = window.CF;
  const $ = (id) => document.getElementById(id);

  // ---------------------------------------------------------------- PIN

  let pin = "";
  try { pin = sessionStorage.getItem("cf-staff-pin") || ""; } catch { /* storage blocked */ }

  async function staffApi(path, options = {}) {
    try {
      return await api(path, { ...options, headers: { "X-Staff-Pin": pin } });
    } catch (err) {
      if (err.status === 401) showPinGate();
      throw err;
    }
  }

  function showPinGate(message) {
    $("pin-gate").hidden = false;
    $("pin-error").hidden = !message;
    $("pin-error").textContent = message || "";
    $("pin-input").focus();
  }

  async function signIn() {
    pin = $("pin-input").value.trim();
    try {
      await api("/api/staff/login", { method: "POST", headers: { "X-Staff-Pin": pin } });
      try { sessionStorage.setItem("cf-staff-pin", pin); } catch { /* fine */ }
      $("pin-gate").hidden = true;
      refreshActive(true);
    } catch {
      showPinGate("That PIN didn't work.");
    }
  }
  $("pin-go").addEventListener("click", signIn);
  $("pin-input").addEventListener("keydown", (e) => { if (e.key === "Enter") signIn(); });

  // ---------------------------------------------------------------- tabs

  let active = "orders";
  document.querySelector(".tabs").addEventListener("click", (e) => {
    const tab = e.target.closest("[data-tab]");
    if (!tab) return;
    active = tab.dataset.tab;
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("on", t === tab));
    ["orders", "menu", "room"].forEach((name) => { $("tab-" + name).hidden = name !== active; });
    refreshActive(true);
  });

  function stamp() {
    $("updated").textContent = "Live · updated " + new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }

  function showError(message) {
    $("board-error").hidden = !message;
    $("board-error").textContent = message || "";
  }

  // ---------------------------------------------------------------- orders

  const ACTIONS = {
    confirmed: [["preparing", "Start preparing", "btn-primary"], ["cancelled", "Cancel", "btn-ghost"]],
    preparing: [["ready", "Mark ready", "btn-green"]],
    ready: [["completed", "Collected", "btn-primary"]],
  };

  function minutesUntil(iso) {
    return Math.round((new Date(iso).getTime() - Date.now()) / 60000);
  }

  function ticket(order) {
    const lines = order.lines
      .map((l) => `<li><span><span class="q">${l.quantity}×</span>${escapeHtml(l.name)}</span><span>${inr(l.line_total_inr)}</span></li>`)
      .join("");
    const due = minutesUntil(order.estimated_ready_at);
    const dueText = order.status === "ready"
      ? "Ready"
      : due >= 0 ? `in ~${due} min` : `<span class="overdue">${-due} min over estimate</span>`;
    const prefs = order.customer_preferences || {};
    const tags = [prefs.dietary_preference, prefs.deadline_minutes ? `needs it in ${prefs.deadline_minutes} min` : null]
      .filter(Boolean).map((p) => `<span class="tag">${escapeHtml(p)}</span>`);
    if (order.delay_minutes) tags.push(`<span class="tag tag-delay">Delayed +${order.delay_minutes} min · guest notified</span>`);
    const actions = (ACTIONS[order.status] || [])
      .map(([status, label, cls]) => `<button class="btn btn-sm ${cls}" type="button" data-order="${escapeHtml(order.order_id)}" data-status="${status}">${label}</button>`)
      .join("");
    const delay = ["confirmed", "preparing"].includes(order.status)
      ? `<div class="ticket-actions delay-actions"><span class="muted-line">Running late?</span>${[5, 10].map((m) => `<button class="btn btn-sm btn-ghost" type="button" data-order="${escapeHtml(order.order_id)}" data-delay="${m}">Delayed +${m}m</button>`).join("")}</div>`
      : "";
    return `
      <article class="ticket">
        <div class="ticket-top"><span class="ticket-id">${escapeHtml(order.order_id)}</span><span class="muted-line">placed ${timeOf(order.created_at)}</span></div>
        ${tags.length ? `<div class="tags" style="margin-bottom:8px">${tags.join("")}</div>` : ""}
        <ul class="ticket-lines">${lines}</ul>
        <div class="ticket-meta"><span>Total <b>${inr(order.total_inr)}</b></span><span>Est. ready ${timeOf(order.estimated_ready_at)} · ${dueText}</span></div>
        <div class="ticket-actions">${actions}</div>
        ${delay}
      </article>`;
  }

  let lastOrdersSig = "";
  function renderOrders(data) {
    const signature = JSON.stringify([data.orders.map((o) => [o.order_id, o.status, o.updated_at]), new Date().getMinutes()]);
    if (signature === lastOrdersSig) return; // don't rebuild under the cursor unless something changed
    lastOrdersSig = signature;
    const by = { confirmed: [], preparing: [], ready: [], done: [] };
    for (const o of data.orders) (by[o.status] || by.done).push(o);
    for (const key of ["confirmed", "preparing", "ready"]) {
      by[key].sort((a, b) => a.created_at.localeCompare(b.created_at));
      $("col-" + key).innerHTML = by[key].length
        ? by[key].map(ticket).join("")
        : `<div class="column-empty">${key === "confirmed" ? "No new orders yet. Place one from the customer app." : "Nothing here."}</div>`;
      $("c-" + key).textContent = by[key].length;
      $("s-" + key).textContent = by[key].length;
    }
    $("s-active").textContent = data.active_count;
    const b = data.queue_backlog_minutes || {};
    $("s-backlog").innerHTML = `${Math.ceil(Math.max(b.bar || 0, b.kitchen || 0))}<small> min · bar ${b.bar || 0} / kitchen ${b.kitchen || 0}</small>`;
    $("c-done").textContent = by.done.length;
    $("history-body").innerHTML = by.done.length
      ? `<table><thead><tr><th>Order</th><th>Items</th><th>Total</th><th>Status</th><th>Updated</th></tr></thead><tbody>${by.done
          .map((o) => `<tr><td class="ticket-id">${escapeHtml(o.order_id)}</td><td>${o.lines.map((l) => `${l.quantity}× ${escapeHtml(l.name)}`).join(", ")}</td><td>${inr(o.total_inr)}</td><td><span class="badge ${o.status === "completed" ? "badge-green" : "badge-red"}">${o.status}</span></td><td>${timeOf(o.updated_at)}</td></tr>`)
          .join("")}</tbody></table>`
      : `<p class="column-empty">No completed orders yet.</p>`;
  }

  $("tab-orders").addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-status], [data-delay]");
    if (!btn) return;
    btn.disabled = true;
    try {
      if (btn.dataset.delay) {
        btn.textContent = "Notifying…";
        await staffApi(`/api/orders/${encodeURIComponent(btn.dataset.order)}/delay`, { method: "POST", body: { minutes: Number(btn.dataset.delay) } });
      } else {
        await staffApi(`/api/orders/${encodeURIComponent(btn.dataset.order)}/status`, { method: "PATCH", body: { status: btn.dataset.status } });
      }
      lastOrdersSig = "";
      await refreshActive(true);
    } catch (err) {
      btn.disabled = false;
      showError(err.message);
    }
  });

  // ---------------------------------------------------------------- menu editor

  async function loadMenu() {
    const data = await staffApi("/api/staff/menu");
    const byCat = {};
    data.items.forEach((i) => (byCat[i.category] = byCat[i.category] || []).push(i));
    $("menu-editor").innerHTML = data.categories.map((cat) => `
      <section class="menu-section">
        <h2>${escapeHtml(cat)}</h2>
        ${(byCat[cat] || []).map((i) => `
          <div class="menu-row ${i.available ? "" : "sold-out"}" data-item="${escapeHtml(i.id)}">
            <div class="menu-name">
              <b>${escapeHtml(i.name)}</b>
              <div class="tags"><span class="tag ${i.diet_marker === "veg" ? "tag-veg" : i.diet_marker === "egg" ? "tag-egg" : "tag-nonveg"}">${i.diet_marker}</span>${i.badges.includes("must-try") ? '<span class="tag tag-must">must try</span>' : ""}${i.edited ? '<span class="tag tag-delay">edited</span>' : ""}</div>
            </div>
            <label class="price-field">R ₹<input type="number" min="1" max="2000" value="${i.price_inr}" data-field="price_inr" aria-label="Regular price"></label>
            ${i.default_price_large_inr ? `<label class="price-field">L ₹<input type="number" min="1" max="2000" value="${i.price_large_inr}" data-field="price_large_inr" aria-label="Large price"></label>` : '<span class="price-field muted-line">—</span>'}
            <label class="stock"><input type="checkbox" data-field="available" ${i.available ? "checked" : ""}> In stock</label>
            <div class="menu-actions">
              <button class="btn btn-sm btn-primary" type="button" data-save>Save</button>
              ${i.edited ? `<button class="btn btn-sm btn-ghost" type="button" data-reset title="Printed menu: ₹${i.default_price_inr}${i.default_price_large_inr ? " / ₹" + i.default_price_large_inr : ""}">Reset</button>` : ""}
            </div>
          </div>`).join("")}
      </section>`).join("");
  }

  $("menu-editor").addEventListener("click", async (e) => {
    const row = e.target.closest("[data-item]");
    const btn = e.target.closest("[data-save], [data-reset]");
    if (!row || !btn) return;
    const id = row.dataset.item;
    btn.disabled = true;
    try {
      if (btn.hasAttribute("data-reset")) {
        await staffApi(`/api/staff/menu/${encodeURIComponent(id)}/reset`, { method: "POST" });
      } else {
        const body = {};
        row.querySelectorAll("[data-field]").forEach((input) => {
          body[input.dataset.field] = input.type === "checkbox" ? input.checked : Number(input.value);
        });
        await staffApi(`/api/staff/menu/${encodeURIComponent(id)}`, { method: "PATCH", body });
      }
      showError("");
      await loadMenu();
      const saved = document.querySelector(`[data-item="${CSS.escape(id)}"] [data-save]`);
      if (saved) { saved.textContent = "Saved"; setTimeout(() => { saved.textContent = "Save"; }, 1500); }
    } catch (err) {
      btn.disabled = false;
      showError(`${id}: ${err.message}`);
    }
  });

  // ---------------------------------------------------------------- room

  const SENT = { positive: "Delighted", neutral: "Neutral", negative: "Unhappy" };

  async function loadRoom() {
    const room = await staffApi("/api/staff/room");
    const fb = room.feedback, dm = room.demand;
    $("room-stats").innerHTML = `
      <div class="stat-tile"><div class="k">Avg rating</div><div class="v">${fb.average_rating ?? "–"}<small> / 5</small></div></div>
      <div class="stat-tile"><div class="k">Feedback</div><div class="v">${fb.count}</div></div>
      <div class="stat-tile"><div class="k">Mood</div><div class="v">${fb.sentiment.positive || 0}<small> delighted · ${fb.sentiment.neutral || 0} neutral · ${fb.sentiment.negative || 0} unhappy</small></div></div>
      <div class="stat-tile"><div class="k">Unmet requests</div><div class="v">${dm.unmet}<small> of ${dm.requests} (${dm.window_hours}h)</small></div></div>`;
    const themes = Object.entries(fb.themes).map(([t, n]) => `<span class="tag">${escapeHtml(t.replace("_", " "))} · ${n}</span>`).join("");
    $("room-feedback").innerHTML = (themes ? `<div class="tags" style="margin-bottom:10px">${themes}</div>` : "") + (fb.latest.length
      ? fb.latest.map((f) => `<div class="fb-row"><span class="fb-face ${f.sentiment || ""}">${SENT[f.sentiment] || "Neutral"}</span><div><b>${f.rating ? f.rating + "/5" : "—"}</b> ${f.order_id ? `<span class="muted-line">${escapeHtml(f.order_id)}</span>` : ""}<div>${escapeHtml(f.comment || (f.themes || []).join(", ") || "No comment")}</div></div></div>`).join("")
      : `<p class="column-empty">No feedback yet.</p>`);
    $("room-demand").innerHTML = dm.top_unmet.length
      ? dm.top_unmet.map((d) => `<div class="fb-row"><span class="fb-face">${d.count}×</span><div>${escapeHtml(d.request)}</div></div>`).join("")
      : `<p class="column-empty">Every request found a match so far.</p>`;
  }

  $("brief-btn").addEventListener("click", async () => {
    const btn = $("brief-btn");
    btn.disabled = true; btn.textContent = "Thinking…";
    try {
      const { brief } = await staffApi("/api/staff/brief", { method: "POST" });
      $("brief-out").innerHTML = `<div class="muted-line">${brief.by === "gemini" ? "Written by Gemini from live data" : "Template (Gemini unavailable)"}</div>` +
        brief.text.split("\n").filter(Boolean).map((l) => `<p>${escapeHtml(l)}</p>`).join("");
    } catch (err) {
      showError(err.message);
    } finally {
      btn.disabled = false; btn.textContent = "Generate with Gemini";
    }
  });

  // ---------------------------------------------------------------- refresh loop

  async function refreshActive(force) {
    if (!$("pin-gate").hidden) return;
    try {
      if (active === "orders") renderOrders(await staffApi("/api/orders"));
      else if (active === "menu" && force) await loadMenu();
      else if (active === "room") await loadRoom();
      showError("");
      stamp();
    } catch (err) {
      if (err.status !== 401) showError(err.message);
    }
  }

  (async () => {
    try {
      await api("/api/staff/login", { method: "POST", headers: { "X-Staff-Pin": pin } });
      refreshActive(true);
    } catch {
      showPinGate();
    }
  })();
  setInterval(() => refreshActive(false), 4000);
})();

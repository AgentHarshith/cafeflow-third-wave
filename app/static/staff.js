/* CaféFlow AI — staff dashboard. Reads real orders from the backend store. */
(function () {
  const { escapeHtml, inr, api, timeOf } = window.CF;
  const $ = (id) => document.getElementById(id);

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
    const prefTags = [prefs.dietary_preference, prefs.deadline_minutes ? `needs it in ${prefs.deadline_minutes} min` : null]
      .filter(Boolean)
      .map((p) => `<span class="tag">${escapeHtml(p)}</span>`)
      .join("");
    const actions = (ACTIONS[order.status] || [])
      .map(([status, label, cls]) => `<button class="btn btn-sm ${cls}" type="button" data-order="${escapeHtml(order.order_id)}" data-status="${status}">${label}</button>`)
      .join("");
    return `
      <article class="ticket">
        <div class="ticket-top"><span class="ticket-id">${escapeHtml(order.order_id)}</span><span class="muted" style="font-size:12px;color:var(--ink-3)">placed ${timeOf(order.created_at)}</span></div>
        ${prefTags ? `<div class="tags" style="margin-bottom:8px">${prefTags}</div>` : ""}
        <ul class="ticket-lines">${lines}</ul>
        <div class="ticket-meta"><span>Total <b>${inr(order.total_inr)}</b></span><span>Est. ready ${timeOf(order.estimated_ready_at)} · ${dueText}</span></div>
        <div class="ticket-actions">${actions}</div>
      </article>`;
  }

  function render(data) {
    const byStatus = { confirmed: [], preparing: [], ready: [], done: [] };
    for (const o of data.orders) (byStatus[o.status] || byStatus.done).push(o);
    // Oldest first in active columns so staff work in arrival order.
    for (const key of ["confirmed", "preparing", "ready"]) {
      byStatus[key].sort((a, b) => a.created_at.localeCompare(b.created_at));
      $("col-" + key).innerHTML = byStatus[key].length
        ? byStatus[key].map(ticket).join("")
        : `<div class="column-empty">${key === "confirmed" ? "No new orders yet. Place one from the customer app." : "Nothing here."}</div>`;
      $("c-" + key).textContent = byStatus[key].length;
      $("s-" + key).textContent = byStatus[key].length;
    }
    $("s-active").textContent = data.active_count;
    const b = data.queue_backlog_minutes || {};
    $("s-backlog").innerHTML = `${Math.ceil(Math.max(b.bar || 0, b.kitchen || 0))}<small> min · bar ${b.bar || 0} / kitchen ${b.kitchen || 0}</small>`;

    $("c-done").textContent = byStatus.done.length;
    $("history-body").innerHTML = byStatus.done.length
      ? `<table><thead><tr><th>Order</th><th>Items</th><th>Total</th><th>Status</th><th>Updated</th></tr></thead><tbody>${byStatus.done
          .map((o) => `<tr><td class="ticket-id">${escapeHtml(o.order_id)}</td><td>${o.lines.map((l) => `${l.quantity}× ${escapeHtml(l.name)}`).join(", ")}</td><td>${inr(o.total_inr)}</td><td><span class="badge ${o.status === "completed" ? "badge-green" : "badge-red"}">${o.status}</span></td><td>${timeOf(o.updated_at)}</td></tr>`)
          .join("")}</tbody></table>`
      : `<p class="column-empty">No completed orders yet.</p>`;
    $("updated").textContent = "Live · updated " + new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }

  let lastSignature = "";

  async function load() {
    try {
      const data = await api("/api/orders");
      // Only rebuild the board when something changed (or the minute ticks over),
      // so a refresh never swallows a click on an action button.
      const signature = JSON.stringify([data.orders.map((o) => [o.order_id, o.status, o.updated_at]), new Date().getMinutes()]);
      if (signature !== lastSignature) {
        lastSignature = signature;
        render(data);
      } else {
        $("updated").textContent = "Live · updated " + new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
      }
      $("board-error").hidden = true;
    } catch (err) {
      $("board-error").hidden = false;
      $("board-error").textContent = err.message;
    }
  }

  document.querySelector(".board").addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-status]");
    if (!btn) return;
    btn.disabled = true;
    try {
      await api(`/api/orders/${encodeURIComponent(btn.dataset.order)}/status`, { method: "PATCH", body: { status: btn.dataset.status } });
      await load();
    } catch (err) {
      btn.disabled = false;
      $("board-error").hidden = false;
      $("board-error").textContent = err.message;
    }
  });

  load();
  setInterval(load, 4000);
})();

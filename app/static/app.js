/* CaféFlow AI — customer app. All prices, waits and order ids come from the backend. */
(function () {
  const { escapeHtml, inr, api, renderMarkdown, setStatusPill, describeGemini, timeOf } = window.CF;
  const CAFE = window.CAFE || { name: "the café", fictional: true };

  const els = {
    messages: document.getElementById("messages"),
    form: document.getElementById("composer"),
    input: document.getElementById("input"),
    send: document.getElementById("send"),
    status: document.getElementById("ai-status"),
    cart: document.getElementById("cart"),
    cartBody: document.getElementById("cart-body"),
    cartSummary: document.getElementById("cart-summary"),
    reviewBtn: document.getElementById("review-btn"),
    cartFab: document.getElementById("cart-fab"),
    cartFabLabel: document.getElementById("cart-fab-label"),
    cartClose: document.getElementById("cart-close"),
    modal: document.getElementById("modal"),
    modalContent: document.getElementById("modal-content"),
  };

  function browserSessionId() {
    let id = null;
    try { id = sessionStorage.getItem("cf-session"); } catch { /* storage blocked */ }
    if (!id || !/^[A-Za-z0-9_-]{8,64}$/.test(id)) {
      id = (crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2) + Date.now().toString(36)).replace(/[^A-Za-z0-9_-]/g, "");
      try { sessionStorage.setItem("cf-session", id); } catch { /* fine: id lives for this page */ }
    }
    return id;
  }

  const state = {
    sessionId: browserSessionId(),
    history: [], // [{role: "user"|"assistant", text}] — used by the demo-mode fallback
    menu: new Map(), // id -> item
    cart: new Map(), // id -> qty (ids only; names/prices resolved from the server menu)
    quote: null,
    quoteError: null,
    lastPreferences: null,
    busy: false,
  };

  // ---------------------------------------------------------------- status

  async function refreshStatus(verify) {
    try {
      const data = verify ? await api("/api/status/verify", { method: "POST" }) : await api("/api/status");
      const [cls, label, title] = describeGemini(data.gemini);
      setStatusPill(els.status, cls, label, title);
      return data.gemini;
    } catch {
      setStatusPill(els.status, "error", "Status unknown", "Could not load AI status.");
      return null;
    }
  }

  // ---------------------------------------------------------------- chat rendering

  function scrollToBottom() {
    requestAnimationFrame(() => window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" }));
  }

  function addMessage(role, html, extraClass = "") {
    const article = document.createElement("article");
    article.className = `msg ${role} ${extraClass}`.trim();
    article.innerHTML = html;
    els.messages.appendChild(article);
    scrollToBottom();
    return article;
  }

  function formatArgs(args) {
    const entries = Object.entries(args || {});
    if (!entries.length) return "";
    return entries
      .map(([k, v]) => `${escapeHtml(k)}: ${escapeHtml(Array.isArray(v) ? `[${v.join(", ")}]` : typeof v === "string" ? `"${v}"` : v)}`)
      .join(", ");
  }

  function activityHtml(resp) {
    const calls = resp.tool_calls || [];
    const route = resp.agent_route || [];
    if (!calls.length && route.length <= 1) return "";
    const routeHtml = route.length ? `<div class="route">${route.map((a) => `<span class="agent-chip">${escapeHtml(a)}</span>`).join('<span class="arrow">→</span>')}</div>` : "";
    const items = calls
      .map(
        (c) => `<li>${c.agent ? `<b>${escapeHtml(c.agent)}</b> ` : ""}<code>${escapeHtml(c.name)}(${formatArgs(c.arguments)})</code> → <span class="${c.ok ? "ok" : "fail"}">${escapeHtml(c.summary)}</span></li>`
      )
      .join("");
    const who = resp.mode === "gemini" ? "ADK agents" : "Demo mode";
    const summary = calls.length ? `${who} called ${calls.length} backend tool${calls.length > 1 ? "s" : ""}` : "Routed by Barista AI";
    return `<details class="activity" open><summary>${summary}</summary>${routeHtml}${items ? `<ol>${items}</ol>` : ""}</details>`;
  }

  const fillingDots = (score) => {
    const capped = Math.min(score, 5);
    return `<span class="filling" title="Filling score ${score}">${"●".repeat(capped)}<span class="off">${"●".repeat(5 - capped)}</span></span>`;
  };

  const MARKER_CLASS = { veg: "tag-veg", egg: "tag-egg", "non-veg": "tag-nonveg" };

  function itemTags(item) {
    const marker = item.diet_marker || ((item.dietary_tags || []).includes("vegetarian") ? "veg" : "non-veg");
    const tags = [`<span class="tag ${MARKER_CLASS[marker] || ""}">${escapeHtml(marker)}</span>`];
    if ((item.badges || []).includes("must-try")) tags.unshift('<span class="tag tag-must">must try</span>');
    if ((item.serving || []).length) tags.push(`<span class="tag">${item.serving.map(escapeHtml).join(" / ")}</span>`);
    for (const t of item.dietary_tags || []) if (t !== "vegetarian") tags.push(`<span class="tag">${escapeHtml(t)}</span>`);
    return tags.join("");
  }

  function optionCard(combo, index, isAlt) {
    const lines = combo.items
      .map((it) => `<li><span><span class="name">${escapeHtml(it.name)}</span><div class="tags">${itemTags(it)}</div></span><span class="price">${inr(it.price_inr)}${it.price_large_inr ? `<small class="large">L ${inr(it.price_large_inr)}</small>` : ""}</span></li>`)
      .join("");
    const badge = isAlt
      ? '<span class="badge badge-orange">Doesn\'t meet everything</span>'
      : combo.fits_deadline
        ? '<span class="badge badge-green">✓ Meets all constraints</span>'
        : "";
    const unmet = isAlt && combo.unmet_constraints?.length
      ? `<ul class="unmet">${combo.unmet_constraints.map((u) => `<li>${escapeHtml(u)}</li>`).join("")}</ul>`
      : "";
    const left = combo.budget_left_inr != null && combo.budget_left_inr >= 0 ? `<div class="stat"><b>${inr(combo.budget_left_inr)}</b><span>budget left</span></div>` : "";
    return `
      <div class="option ${isAlt ? "alt" : ""}">
        <div class="option-top"><span class="option-rank">${isAlt ? "Alternative" : "Option " + (index + 1)}</span>${badge}</div>
        <ul class="option-items">${lines}</ul>
        ${unmet}
        <div class="option-stats">
          <div class="stat"><b>${inr(combo.total_price_inr)}</b><span>total</span></div>
          <div class="stat"><b>~${combo.estimated_wait_minutes} min</b><span>est. ready</span></div>
          ${left}
        </div>
        <div style="display:flex;justify-content:space-between;align-items:center;gap:8px">
          ${fillingDots(combo.combined_filling_score)}
          <button class="btn btn-sm ${isAlt ? "" : "btn-green"}" type="button" data-add='${escapeHtml(JSON.stringify(combo.item_ids))}'>Add to order</button>
        </div>
      </div>`;
  }

  function menuItemCard(item) {
    return `
      <div class="option">
        <div class="option-top"><span class="option-rank">${escapeHtml(item.category)}</span>${fillingDots(item.filling_score)}</div>
        <ul class="option-items"><li><span><span class="name">${escapeHtml(item.name)}</span><div class="tags">${itemTags(item)}</div></span><span class="price">${inr(item.price_inr)}</span></li></ul>
        <div class="option-stats">
          <div class="stat"><b>${item.preparation_time_minutes} min</b><span>prep time</span></div>
          <div class="stat"><b>${item.sweetness_level ?? 0}/5</b><span>sweetness</span></div>
        </div>
        <button class="btn btn-sm btn-green" type="button" data-add='${escapeHtml(JSON.stringify([item.id]))}'>Add to order</button>
      </div>`;
  }

  function renderAssistant(resp) {
    const meta = resp.mode === "gemini"
      ? `<span class="badge badge-green">Gemini${resp.model ? " · " + escapeHtml(resp.model) : ""}</span>`
      : `<span class="badge badge-orange">Demo mode · rule-based</span>`;
    let html = `<div class="msg-meta">${meta}</div>${activityHtml(resp)}`;
    html += `<div class="bubble">${renderMarkdown(resp.reply)}</div>`;

    if (resp.recommendations?.length) {
      html += `<div class="cards-label">Options from the menu engine</div><div class="cards">${resp.recommendations.map((c, i) => optionCard(c, i, false)).join("")}</div>`;
    }
    if (resp.alternatives?.length) {
      html += `<div class="cards-label">Closest alternatives — not fully compliant</div><div class="cards">${resp.alternatives.map((c, i) => optionCard(c, i, true)).join("")}</div>`;
    }
    if (!resp.recommendations?.length && !resp.alternatives?.length && resp.menu_items?.length) {
      html += `<div class="cards-label">From the menu</div><div class="cards">${resp.menu_items.map(menuItemCard).join("")}</div>`;
    }
    if (resp.order_proposal) {
      const p = resp.order_proposal;
      html += `
        <div class="proposal">
          <h4>Order draft — not placed yet</h4>
          <ul class="modal-lines">${p.lines.map((l) => `<li><span>${l.quantity} × ${escapeHtml(l.name)}</span><span>${inr(l.line_total_inr)}</span></li>`).join("")}</ul>
          <div class="modal-total"><span>Total</span><span>${inr(p.total_inr)}</span></div>
          <p class="modal-sub">Estimated ready in ~${p.estimated_wait_minutes} min (approximate)</p>
          <button class="btn btn-primary btn-block" type="button" data-review='${escapeHtml(JSON.stringify(p.item_ids))}'>Review &amp; confirm</button>
        </div>`;
    } else if (resp.wait_estimate && !resp.recommendations?.length) {
      const w = resp.wait_estimate;
      html += `<div class="wait-card"><h4>Wait estimate: ~${w.estimated_wait_minutes} min</h4><p class="modal-sub" style="margin:0">Includes ${w.active_orders_in_queue} active order(s) in the queue. Approximate.</p></div>`;
    }
    const showedItems = resp.recommendations?.length || resp.alternatives?.length || resp.menu_items?.length;
    if (showedItems && CAFE.allergen_data === "indicative") {
      html += `<div class="msg-meta" style="margin-top:6px">Veg/egg/non-veg markers from a public listing · other dietary &amp; allergen details are indicative — confirm with staff.</div>`;
    }
    if (resp.notice) html += `<div class="msg-meta" style="margin-top:6px">${escapeHtml(resp.notice)}</div>`;
    addMessage("assistant", html);
  }

  // Item ids of the cards now on screen, sent with the next message so the agent can
  // resolve "I'll take option 2" (the server re-derives names from the real menu).
  function shownOptions(resp) {
    return [
      ...(resp.recommendations || []).map((c) => c.item_ids),
      ...(resp.alternatives || []).map((c) => c.item_ids),
      ...(resp.menu_items || []).map((it) => [it.id]),
    ].slice(0, 12);
  }

  function rememberPreferences(resp) {
    const rec = [...(resp.tool_calls || [])].reverse().find((c) => c.name === "recommend_options" && c.ok);
    if (!rec) return;
    const a = rec.arguments || {};
    state.lastPreferences = {
      dietary_preference: a.dietary_preference || null,
      budget_inr: a.budget_inr ?? null,
      deadline_minutes: a.deadline_minutes ?? null,
    };
  }

  // ---------------------------------------------------------------- send

  async function sendMessage(text, extra = {}, displayHtml = null) {
    text = text.trim();
    if (!text || state.busy) return;
    state.busy = true;
    els.send.disabled = true;
    els.input.value = "";
    autoGrow();

    addMessage("user", `<div class="bubble">${displayHtml || escapeHtml(text)}</div>`);
    const typing = addMessage("assistant", `<div class="bubble"><span class="typing"><span class="dots"><i></i><i></i><i></i></span>Checking the menu and kitchen queue…</span></div>`);

    try {
      const resp = await api("/api/chat", { method: "POST", body: { message: text, history: state.history.slice(-20), shown_options: state.shownOptions || [], session_id: state.sessionId, ...extra } });
      typing.remove();
      renderAssistant(resp);
      rememberPreferences(resp);
      if (resp.connect && window.CFConnect) window.CFConnect.handleAction(resp.connect);
      state.history.push({ role: "user", text }, { role: "assistant", text: resp.reply.slice(0, 3500) });
      state.shownOptions = shownOptions(resp);
      if (resp.mode === "gemini") {
        setStatusPill(els.status, "connected", "Gemini connected", `Live Gemini requests succeeding · model ${resp.model}`);
      } else {
        refreshStatus(false);
      }
    } catch (err) {
      typing.remove();
      const msg = addMessage("assistant", `<div class="bubble">${escapeHtml(err.message)} <button class="btn btn-sm" type="button" style="margin-left:6px">Retry</button></div>`, "error");
      msg.querySelector("button").addEventListener("click", () => { msg.remove(); sendMessage(text); });
    } finally {
      state.busy = false;
      els.send.disabled = false;
      els.input.focus();
    }
  }

  // ---------------------------------------------------------------- cart

  function cartIds() {
    const ids = [];
    for (const [id, qty] of state.cart) for (let i = 0; i < qty; i++) ids.push(id);
    return ids;
  }

  function addToCart(ids) {
    for (const id of ids) {
      if (!state.menu.has(id)) continue;
      state.cart.set(id, Math.min((state.cart.get(id) || 0) + 1, 5));
    }
    renderCart();
    refreshQuote();
  }

  function setQty(id, qty) {
    if (qty <= 0) state.cart.delete(id);
    else state.cart.set(id, Math.min(qty, 5));
    renderCart();
    refreshQuote();
  }

  function renderCart() {
    const count = [...state.cart.values()].reduce((a, b) => a + b, 0);
    if (!count) {
      els.cartBody.innerHTML = `<div class="cart-empty">Your order is empty. Ask for a recommendation, then tap <b>Add to order</b>.</div>`;
    } else {
      els.cartBody.innerHTML = [...state.cart.entries()]
        .map(([id, qty]) => {
          const item = state.menu.get(id);
          return `
            <div class="cart-line">
              <div><div class="name">${escapeHtml(item.name)}</div><div class="sub">${inr(item.price_inr)} · ${item.preparation_time_minutes} min prep</div></div>
              <div class="line-total">${inr(item.price_inr * qty)}</div>
              <div class="qty" aria-label="Quantity for ${escapeHtml(item.name)}">
                <button type="button" data-qty="${escapeHtml(id)}" data-delta="-1" aria-label="Decrease">−</button>
                <span>${qty}</span>
                <button type="button" data-qty="${escapeHtml(id)}" data-delta="1" aria-label="Increase">+</button>
              </div>
            </div>`;
        })
        .join("");
    }
    renderSummary();
    els.cartFab.hidden = count === 0;
    document.body.classList.toggle("has-cart", count > 0);
    els.cartFabLabel.textContent = count ? `${count} item${count > 1 ? "s" : ""} · ${state.quote ? inr(state.quote.total_inr) : "…"}` : "Cart";
  }

  function renderSummary() {
    const count = state.cart.size;
    if (!count) {
      els.cartSummary.innerHTML = "";
      els.reviewBtn.disabled = true;
      return;
    }
    if (state.quoteError) {
      els.cartSummary.innerHTML = `<div class="form-error">${escapeHtml(state.quoteError)}</div>`;
      els.reviewBtn.disabled = true;
      return;
    }
    if (!state.quote) {
      els.cartSummary.innerHTML = `<div class="muted">Calculating…</div>`;
      els.reviewBtn.disabled = true;
      return;
    }
    const w = state.quote.wait_estimate;
    els.cartSummary.innerHTML = `
      <div class="row total"><span>Total</span><span>${inr(state.quote.total_inr)}</span></div>
      <div class="row"><span>Estimated ready</span><span>~${w.estimated_wait_minutes} min</span></div>
      <div class="muted">${w.active_orders_in_queue ? `Includes ${w.active_orders_in_queue} order(s) ahead in the queue.` : "No orders ahead of you right now."}</div>`;
    els.reviewBtn.disabled = false;
  }

  let quoteSeq = 0;
  async function refreshQuote() {
    const seq = ++quoteSeq;
    state.quote = null;
    state.quoteError = null;
    renderSummary();
    const ids = cartIds();
    if (!ids.length) return;
    try {
      const q = await api("/api/orders/quote", { method: "POST", body: { item_ids: ids } });
      if (seq !== quoteSeq) return;
      state.quote = q;
    } catch (err) {
      if (seq !== quoteSeq) return;
      state.quoteError = err.message;
    }
    renderCart();
  }

  // ---------------------------------------------------------------- confirmation flow

  function openModal(html) {
    els.modalContent.innerHTML = html;
    els.modal.hidden = false;
    document.body.style.overflow = "hidden";
    els.modalContent.querySelector("[data-autofocus]")?.focus();
  }
  function closeModal() {
    els.modal.hidden = true;
    document.body.style.overflow = "";
  }

  async function openReview() {
    els.cart.classList.remove("open");
    const ids = cartIds();
    if (!ids.length) return;
    openModal(`<h3>Confirm your order</h3><p class="modal-sub">Checking prices and the kitchen queue…</p>`);
    let quote;
    try {
      quote = await api("/api/orders/quote", { method: "POST", body: { item_ids: ids } });
    } catch (err) {
      openModal(`<h3>Can't place this order</h3><p class="form-error">${escapeHtml(err.message)}</p><div class="modal-actions"><button class="btn" data-close>Back</button></div>`);
      return;
    }
    const w = quote.wait_estimate;
    openModal(`
      <h3>Confirm your order</h3>
      <p class="modal-sub">${escapeHtml(CAFE.name)} · demo order</p>
      <ul class="modal-lines">${quote.lines.map((l) => `<li><span>${l.quantity} × ${escapeHtml(l.name)}</span><span>${inr(l.line_total_inr)}</span></li>`).join("")}</ul>
      <div class="modal-total"><span>Total</span><span>${inr(quote.total_inr)}</span></div>
      <p class="modal-sub" style="margin:0">Estimated ready in <b>~${w.estimated_wait_minutes} min</b> (approximate)</p>
      <div class="modal-note">${CAFE.fictional ? "This is a demo with simulated café data." : `Unofficial concept demo: this order is <b>not</b> sent to ${escapeHtml(CAFE.name)}.`} No payment is taken, and the order is kept in demo memory only.</div>
      <div class="modal-actions">
        <button class="btn" type="button" data-close>Keep editing</button>
        <button class="btn btn-primary" type="button" id="confirm-order" data-autofocus>Confirm &amp; place order</button>
      </div>
      <p class="form-error" id="confirm-error" hidden></p>`);
    document.getElementById("confirm-order").addEventListener("click", () => confirmOrder(ids));
  }

  async function confirmOrder(ids) {
    const btn = document.getElementById("confirm-order");
    const errEl = document.getElementById("confirm-error");
    btn.disabled = true;
    btn.textContent = "Placing order…";
    try {
      const body = { item_ids: ids, confirmed: true };
      if (state.lastPreferences) body.customer_preferences = state.lastPreferences;
      const order = await api("/api/orders", { method: "POST", body });
      trackOrder(order);
      state.cart.clear();
      state.quote = null;
      renderCart();
      showSuccess(order);
      addMessage(
        "assistant",
        `<div class="msg-meta"><span class="badge badge-green">Order system</span></div><div class="bubble">✅ Order <b>${escapeHtml(order.order_id)}</b> confirmed by the café backend — ${inr(order.total_inr)}, estimated ready around <b>${timeOf(order.estimated_ready_at)}</b> (~${order.estimated_wait_minutes} min).</div>`,
        "system"
      );
      state.history.push({ role: "assistant", text: `Order ${order.order_id} was placed by the customer (total ₹${order.total_inr}, ~${order.estimated_wait_minutes} min).` });
    } catch (err) {
      btn.disabled = false;
      btn.textContent = "Confirm & place order";
      errEl.hidden = false;
      errEl.textContent = err.message;
    }
  }

  function showSuccess(order) {
    openModal(`
      <div class="success-mark" aria-hidden="true">✓</div>
      <h3>Order placed</h3>
      <p class="modal-sub">Show this order ID at the counter.</p>
      <div class="order-id">${escapeHtml(order.order_id)}</div>
      <dl class="kv">
        <dt>Items</dt><dd>${order.lines.map((l) => `${l.quantity} × ${escapeHtml(l.name)}`).join("<br>")}</dd>
        <dt>Total</dt><dd>${inr(order.total_inr)}</dd>
        <dt>Estimated wait</dt><dd>~${order.estimated_wait_minutes} min (approx.)</dd>
        <dt>Ready around</dt><dd>${timeOf(order.estimated_ready_at)}</dd>
        <dt>Status</dt><dd><span class="badge badge-green">${escapeHtml(order.status)}</span></dd>
      </dl>
      <div class="modal-actions">
        <a class="btn" href="/staff" target="_blank" rel="noopener">View staff dashboard</a>
        <button class="btn btn-primary" type="button" data-close data-autofocus>Done</button>
      </div>`);
  }

  // ---------------------------------------------------------------- events

  function autoGrow() {
    els.input.style.height = "auto";
    els.input.style.height = Math.min(els.input.scrollHeight, 140) + "px";
  }

  els.form.addEventListener("submit", (e) => { e.preventDefault(); sendMessage(els.input.value); });
  els.input.addEventListener("input", autoGrow);
  els.input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendMessage(els.input.value); }
  });
  document.getElementById("quick-prompts").addEventListener("click", (e) => {
    const chip = e.target.closest(".chip");
    if (chip) sendMessage(chip.textContent);
  });

  els.messages.addEventListener("click", (e) => {
    const add = e.target.closest("[data-add]");
    if (add) {
      addToCart(JSON.parse(add.dataset.add));
      add.textContent = "Added ✓";
      setTimeout(() => { add.textContent = "Add to order"; }, 1400);
      return;
    }
    const review = e.target.closest("[data-review]");
    if (review) {
      state.cart.clear();
      addToCart(JSON.parse(review.dataset.review));
      openReview();
    }
  });

  els.cartBody.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-qty]");
    if (btn) setQty(btn.dataset.qty, (state.cart.get(btn.dataset.qty) || 0) + Number(btn.dataset.delta));
  });
  els.reviewBtn.addEventListener("click", openReview);
  els.cartFab.addEventListener("click", () => els.cart.classList.add("open"));
  els.cartClose.addEventListener("click", () => els.cart.classList.remove("open"));
  els.modal.addEventListener("click", (e) => { if (e.target.closest("[data-close]")) closeModal(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !els.modal.hidden) closeModal(); });

  // ---------------------------------------------------------------- order tracking & feedback

  const TRACK_KEY = "cf-orders";
  function loadTracked() {
    try { return JSON.parse(sessionStorage.getItem(TRACK_KEY) || "{}"); } catch { return {}; }
  }
  function saveTracked(t) {
    try { sessionStorage.setItem(TRACK_KEY, JSON.stringify(t)); } catch { /* fine */ }
  }
  state.tracked = loadTracked(); // order_id -> {status, delayedAt, feedbackAsked}

  function trackOrder(order) {
    state.tracked[order.order_id] = { status: order.status, delayedAt: order.delayed_at || null, feedbackAsked: false,
                                      items: order.lines.map((l) => `${l.quantity}× ${l.name}`).join(", ") };
    saveTracked(state.tracked);
  }

  function agentNote(label, html, cls = "") {
    addMessage("assistant", `<div class="msg-meta"><span class="badge ${cls}">${escapeHtml(label)}</span></div><div class="bubble">${html}</div>`, "system");
  }

  async function pollOrders() {
    for (const [id, t] of Object.entries(state.tracked)) {
      if (t.done) continue;
      let data;
      try { data = await api(`/api/orders/${encodeURIComponent(id)}/track`); } catch { continue; }
      const o = data.order;
      if (o.delayed_at && o.delayed_at !== t.delayedAt) {
        t.delayedAt = o.delayed_at;
        agentNote("Wait-time agent", `⏱️ ${escapeHtml(o.delay_message || "Your order is running a little late.")}<br><span class="muted-line">Order ${escapeHtml(id)} · new estimate around <b>${timeOf(o.estimated_ready_at)}</b></span>`, "badge-orange");
      }
      if (o.status !== t.status) {
        t.status = o.status;
        if (o.status === "ready") agentNote("Order update", `🔔 Order <b>${escapeHtml(id)}</b> is <b>ready for pickup</b>!`, "badge-green");
        if (o.status === "cancelled") { agentNote("Order update", `Order ${escapeHtml(id)} was cancelled by the café.`); t.done = true; }
      }
      if (o.status === "completed") {
        if (!t.feedbackAsked && !data.feedback_given && els.modal.hidden) { t.feedbackAsked = true; openFeedback(id, t.items); }
        if (t.feedbackAsked || data.feedback_given) t.done = true;
      }
      saveTracked(state.tracked);
    }
  }

  const FEEDBACK_FACES = [[1, "😞"], [2, "🙁"], [3, "😐"], [4, "🙂"], [5, "😍"]];
  const FEEDBACK_CHIPS = ["Perfect", "Great taste", "Took too long", "Too sweet", "Not hot enough", "Friendly staff", "Wrong item", "Pricey"];

  function openFeedback(orderId, items) {
    openModal(`
      <h3>How was it?</h3>
      <p class="modal-sub">Order ${escapeHtml(orderId)} · ${escapeHtml(items || "")}</p>
      <div class="faces" role="radiogroup" aria-label="Rating">${FEEDBACK_FACES.map(([v, f]) => `<button type="button" class="face" data-rate="${v}" aria-label="${v} out of 5">${f}</button>`).join("")}</div>
      <div class="chips fb-chips">${FEEDBACK_CHIPS.map((c) => `<button type="button" class="chip" data-chip="${escapeHtml(c)}">${escapeHtml(c)}</button>`).join("")}</div>
      <textarea id="fb-comment" class="fb-comment" rows="2" maxlength="600" placeholder="Anything else? (optional)"></textarea>
      <div class="modal-actions">
        <button class="btn" type="button" data-close>Skip</button>
        <button class="btn btn-primary" type="button" id="fb-send" disabled>Send feedback</button>
      </div>`);
    let rating = null;
    const chips = new Set();
    els.modalContent.querySelectorAll(".face").forEach((b) => b.addEventListener("click", () => {
      rating = Number(b.dataset.rate);
      els.modalContent.querySelectorAll(".face").forEach((x) => x.classList.toggle("on", x === b));
      document.getElementById("fb-send").disabled = false;
    }));
    els.modalContent.querySelectorAll("[data-chip]").forEach((b) => b.addEventListener("click", () => {
      const c = b.dataset.chip;
      chips.has(c) ? chips.delete(c) : chips.add(c);
      b.classList.toggle("on", chips.has(c));
    }));
    document.getElementById("fb-send").addEventListener("click", () => {
      const comment = document.getElementById("fb-comment").value.trim();
      closeModal();
      const face = FEEDBACK_FACES.find(([v]) => v === rating)[1];
      const text = `Feedback for order ${orderId}: ${rating}/5${chips.size ? " — " + [...chips].join(", ") : ""}${comment ? `. ${comment}` : ""}`;
      const display = `${face} <b>${rating}/5</b>${chips.size ? " · " + [...chips].map(escapeHtml).join(" · ") : ""}${comment ? `<br>“${escapeHtml(comment)}”` : ""}`;
      sendMessage(text, { feedback_form: { order_id: orderId, rating, chips: [...chips], comment: comment || null } }, display);
    });
  }

  window.CFApp = { openModal, closeModal, addMessage, agentNote, sendMessage, state, isModalOpen: () => !els.modal.hidden };

  // ---------------------------------------------------------------- init

  async function init() {
    renderCart();
    try {
      const menu = await api("/api/menu");
      menu.items.forEach((item) => state.menu.set(item.id, item));
    } catch (err) {
      addMessage("assistant", `<div class="bubble">Couldn't load the menu: ${escapeHtml(err.message)}</div>`, "error");
    }
    const gemini = await refreshStatus(false);
    if (gemini && gemini.state === "unverified") refreshStatus(true);
    setInterval(pollOrders, 5000);
  }

  init();
})();

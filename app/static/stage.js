/* CaféFlow AI — the stage: greeting, the agent constellation, and the collection carousel. */
(function () {
  const { escapeHtml, inr, api } = window.CF;
  const app = window.CFApp;
  const $ = (id) => document.getElementById(id);
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // ---------------------------------------------------------------- greeting

  const hour = new Date().getHours();
  $("greet-word").textContent = hour < 5 ? "Still up" : hour < 12 ? "Good morning" : hour < 17 ? "Good afternoon" : "Good evening";

  // ---------------------------------------------------------------- constellation

  const KEY = { "Barista AI": "router", Discovery: "discovery", Ordering: "ordering", "Wait-time": "wait", Feedback: "feedback", "Connect People": "connect" };
  const NAME = { router: "Barista AI", discovery: "Discovery", ordering: "Ordering", wait: "Wait-time", feedback: "Feedback", connect: "Connect People" };
  const nodes = {};
  document.querySelectorAll(".constellation .node").forEach((n) => (nodes[n.dataset.agent] = n));
  const links = {};
  document.querySelectorAll(".constellation .link").forEach((l) => (links[l.dataset.to] = l));
  const caption = $("const-caption");
  let playId = 0;

  function clear() {
    Object.values(nodes).forEach((n) => n.classList.remove("lit", "listening"));
    Object.values(links).forEach((l) => l.classList.remove("flow"));
  }

  function listen() {
    playId++;
    clear();
    nodes.router.classList.add("listening");
    caption.innerHTML = "<b>Barista AI</b> is listening…";
  }

  async function play(route) {
    const id = ++playId;
    clear();
    const seq = (route || []).map((label) => KEY[label]).filter(Boolean);
    if (!seq.length) seq.push("router");
    for (const key of seq) {
      if (id !== playId) return;
      if (key !== "router" && links[key]) links[key].classList.add("flow");
      nodes[key].classList.add("lit");
      if (!reduced) await sleep(420);
    }
    const names = seq.map((k) => NAME[k]);
    caption.innerHTML = names.length > 1
      ? `<b>${names.map(escapeHtml).join("</b> → <b>")}</b>`
      : `<b>${escapeHtml(names[0])}</b> is with you`;
    await sleep(2400);
    if (id === playId) Object.values(links).forEach((l) => l.classList.remove("flow"));
  }

  window.CFStage = { listen, play };

  // ---------------------------------------------------------------- the collection

  const ICON = {
    cup: '<path class="stroke" d="M28 44h44v12a22 22 0 0 1-22 22h0a22 22 0 0 1-22-22z"/><path class="stroke" d="M72 49h5a8 8 0 0 1 0 16h-6"/><path class="stroke" d="M20 86h60"/><path class="steam" d="M42 36c-5-6 5-10 0-17"/><path class="steam s2" d="M56 36c-5-6 5-10 0-17"/>',
    iced: '<path class="stroke" d="M31 28l7 56h24l7-56z"/><path class="stroke" d="M34 44h32"/><path class="stroke" d="M41 52l8-3 3 8-8 3z"/><path class="stroke" d="M50 64l8 2-2 8-8-2z"/><path class="stroke" d="M57 28l8-16h7"/>',
    matcha: '<path class="stroke" d="M18 50h64a32 28 0 0 1-64 0z"/><path class="stroke" d="M40 80h20"/><path class="stroke" d="M40 44c4-12 16-15 22-16-1 9-9 16-22 16z"/><path class="steam" d="M30 40c-4-6 4-9 0-15"/><path class="steam s2" d="M70 40c-4-6 4-9 0-15"/>',
    bite: '<path class="stroke" d="M16 64c8-26 60-26 68 0"/><path class="stroke" d="M16 64c22 12 46 12 68 0"/><path class="stroke" d="M33 46l6 20M50 41v24M67 46l-6 20"/>',
    sandwich: '<path class="stroke" d="M20 76L50 24l30 52z"/><path class="stroke" d="M29 61c6-4 10 4 16 0s10 4 16 0 8 4 11 0"/><path class="stroke" d="M35 50h30"/>',
    pizza: '<path class="stroke" d="M50 86L21 27c19-8 39-8 58 0z"/><path class="stroke" d="M24 33c17-7 35-7 52 0"/><circle class="stroke" cx="46" cy="45" r="4"/><circle class="stroke" cx="57" cy="58" r="3.5"/><circle class="stroke" cx="49" cy="70" r="3"/>',
    salad: '<path class="stroke" d="M16 52h68a34 28 0 0 1-68 0z"/><path class="stroke" d="M28 50c0-13 11-17 17-19 0 9-5 17-17 19z"/><path class="stroke" d="M52 50c2-11 13-15 21-15-2 9-9 15-21 15z"/><path class="stroke" d="M44 50c2-7 6-10 10-11"/>',
  };
  const PLUS = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>';
  const TICK = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7" stroke="currentColor" stroke-width="2.2" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>';

  function kindOf(item) {
    const cat = item.category.toLowerCase();
    if (item.station === "bar") {
      if (cat.includes("matcha") && (item.serving || []).includes("hot")) return "matcha";
      const serving = item.serving || [];
      return serving.length && !serving.includes("hot") ? "iced" : "cup";
    }
    if (cat.includes("pizza")) return "pizza";
    if (cat.includes("salad")) return "salad";
    if (cat.includes("sandwich") || cat.includes("wrap") || cat.includes("savoury")) return "sandwich";
    return "bite";
  }

  const shortName = (cat) => cat.replace(/^Quick Shareable /, "").replace(/^Handcrafted /, "").replace(/ & .*/, "");

  let menu = [];
  let current = "must";

  function card(item, i) {
    const veg = item.diet_marker === "veg";
    return `
      <article class="coll-card ${item.available ? "" : "sold-out"}" style="--i:${i}">
        ${(item.badges || []).includes("must-try") ? '<span class="must-tag">Must try</span>' : ""}
        <div class="coll-art"><svg viewBox="0 0 100 100" aria-hidden="true">${ICON[kindOf(item)]}</svg></div>
        <h3 class="coll-name">${escapeHtml(item.name)}</h3>
        <div class="coll-meta"><span class="veg ${veg ? "" : "nonveg"}" title="${veg ? "Vegetarian" : "Non-vegetarian"}"></span>${inr(item.price_inr)} · ~${item.preparation_time_minutes} min</div>
        ${item.available
          ? `<button class="coll-add" type="button" data-coll-add="${escapeHtml(item.id)}" aria-label="Add ${escapeHtml(item.name)} to your order">${PLUS}</button>`
          : ""}
      </article>`;
  }

  function render() {
    const items = current === "must"
      ? menu.filter((i) => (i.badges || []).includes("must-try"))
      : menu.filter((i) => i.category === current);
    $("collection").innerHTML = items.map(card).join("");
    $("collection").scrollTo({ left: 0, behavior: reduced ? "auto" : "smooth" });
    document.querySelectorAll("#collection-tabs .pill").forEach((p) => p.classList.toggle("on", p.dataset.cat === current));
  }

  async function loadCollection() {
    let data;
    try { data = await api("/api/menu"); } catch { return; }
    // Signature drinks lead, then food — the way the café's own menu reads.
    const rank = (i) => (i.station === "bar" ? 0 : 1);
    menu = [...data.items].sort((a, b) => rank(a) - rank(b));
    const categories = [...data.categories].sort((a, b) =>
      rank(menu.find((i) => i.category === a)) - rank(menu.find((i) => i.category === b)));
    const hasMust = menu.some((i) => (i.badges || []).includes("must-try"));
    if (!hasMust) current = categories[0];
    $("collection-tabs").innerHTML =
      (hasMust ? '<button class="pill must" type="button" data-cat="must" role="tab">Must try</button>' : "") +
      categories.map((c) => `<button class="pill" type="button" data-cat="${escapeHtml(c)}" role="tab">${escapeHtml(shortName(c))}</button>`).join("");
    render();
  }

  $("collection-tabs").addEventListener("click", (e) => {
    const pill = e.target.closest("[data-cat]");
    if (!pill) return;
    current = pill.dataset.cat;
    render();
  });

  $("collection").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-coll-add]");
    if (!btn) return;
    app.addToCart([btn.dataset.collAdd]);
    btn.classList.add("added");
    btn.innerHTML = TICK;
    const fab = $("cart-fab");
    fab.classList.remove("bump");
    void fab.offsetWidth; // restart the animation
    fab.classList.add("bump");
    setTimeout(() => { btn.classList.remove("added"); btn.innerHTML = PLUS; }, 1300);
  });

  // ---------------------------------------------------------------- order slip scrim

  const cart = $("cart");
  const scrim = $("cart-scrim");
  new MutationObserver(() => { scrim.hidden = !cart.classList.contains("open"); }).observe(cart, { attributes: true, attributeFilter: ["class"] });
  scrim.addEventListener("click", () => cart.classList.remove("open"));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") cart.classList.remove("open"); });

  loadCollection();
})();

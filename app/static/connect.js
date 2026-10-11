/* CaféFlow AI — Connect People (opt-in). Nickname + optional table only; no names, emails or free text. */
(function () {
  const { escapeHtml, api } = window.CF;
  const app = window.CFApp;
  const sessionId = app.state.sessionId;

  const seen = { invites: new Set(), matches: new Set() };
  let latest = null; // last /api/connect/state
  let polling = null;

  const post = (path, body = {}) => api(path, { method: "POST", body: { session_id: sessionId, ...body } });

  // ---------------------------------------------------------------- modals

  function signupModal() {
    app.openModal(`
      <h3>Meet someone here</h3>
      <p class="modal-sub">Opt in to be introduced to kindred spirits in the café over three questions about coffee. Only your nickname and (optional) table are ever shared.</p>
      <label class="field">Nickname <input type="text" id="cn-nick" maxlength="24" placeholder="e.g. Oat Latte Fan" autocomplete="off" data-autofocus></label>
      <label class="field">Table number (optional) <input type="number" id="cn-table" min="1" max="200" placeholder="e.g. 4"></label>
      <label class="check"><input type="checkbox" id="cn-consent"> I agree to show my nickname and table to people I match with. I can stop sharing anytime.</label>
      <p class="form-error" id="cn-error" hidden></p>
      <div class="modal-actions">
        <button class="btn" type="button" data-close>Not now</button>
        <button class="btn btn-primary" type="button" id="cn-signup">Sign up to connect</button>
      </div>`);
    document.getElementById("cn-signup").addEventListener("click", async () => {
      const err = document.getElementById("cn-error");
      const nickname = document.getElementById("cn-nick").value.trim();
      const tableRaw = document.getElementById("cn-table").value.trim();
      if (!document.getElementById("cn-consent").checked) { err.hidden = false; err.textContent = "Please tick the consent box to continue."; return; }
      try {
        await post("/api/connect/profile", { nickname, table: tableRaw ? Number(tableRaw) : null, consent: true });
        app.closeModal();
        startPolling();
        app.agentNote("Connect People", `You're in as <b>${escapeHtml(nickname)}</b>. Tap <b>Connect me with people</b> (or just ask me) whenever you'd like to meet someone here.`);
        renderButton(true);
      } catch (e) {
        err.hidden = false; err.textContent = e.message;
      }
    });
  }

  function panelModal() {
    const s = latest;
    if (!s || !s.signed_up) return signupModal();
    const matches = s.matches.map((m) => `<div class="match-row"><b>${m.percent}% match</b> with ${escapeHtml(m.with_profile.nickname)}${m.with_profile.table ? ` · table ${m.with_profile.table}` : ""}</div>`).join("");
    const mine = s.my_request
      ? `<div class="connect-row"><span>Your request</span><span>${s.my_request.responses} response(s)</span></div>`
      : "";
    app.openModal(`
      <h3>Connect People</h3>
      <div class="connect-card">
        <div class="connect-row"><span>You appear as</span><b>${escapeHtml(s.profile.nickname)}${s.profile.table ? ` · table ${s.profile.table}` : ""}</b></div>
        <div class="connect-row"><span>Others online now</span><b>${s.people_online}</b></div>
        ${mine}
        ${matches || '<p class="modal-sub" style="margin:0">No matches yet.</p>'}
      </div>
      <div class="modal-actions" style="margin-top:14px">
        <button class="btn" type="button" id="cn-leave">Stop sharing</button>
        <button class="btn btn-primary" type="button" id="cn-go">Connect me with people</button>
      </div>`);
    document.getElementById("cn-go").addEventListener("click", () => { app.closeModal(); requestConnect(); });
    document.getElementById("cn-leave").addEventListener("click", async () => {
      await post("/api/connect/leave").catch(() => {});
      latest = null; stopPolling(); renderButton(false); app.closeModal();
      app.agentNote("Connect People", "You've stopped sharing. Your nickname is no longer visible to anyone.");
    });
  }

  function questionsModal(requestId, intro) {
    const questions = (latest && latest.questions) || [];
    const picked = {};
    app.openModal(`
      <h3>Three questions</h3>
      <p class="modal-sub">${intro}</p>
      ${questions.map((q) => `
        <div class="q-block"><h4>${escapeHtml(q.text)}</h4>
          <div class="q-options">${q.options.map((o) => `<button type="button" class="q-opt" data-q="${q.id}" data-o="${o.id}">${escapeHtml(o.label)}</button>`).join("")}</div>
        </div>`).join("")}
      <p class="form-error" id="q-error" hidden></p>
      <div class="modal-actions">
        <button class="btn" type="button" id="q-skip">Not now</button>
        <button class="btn btn-primary" type="button" id="q-send" disabled>Reveal my match</button>
      </div>`);
    const send = document.getElementById("q-send");
    document.querySelectorAll(".q-opt").forEach((b) => b.addEventListener("click", () => {
      picked[b.dataset.q] = b.dataset.o;
      document.querySelectorAll(`.q-opt[data-q="${b.dataset.q}"]`).forEach((x) => x.classList.toggle("on", x === b));
      send.disabled = Object.keys(picked).length < questions.length;
    }));
    document.getElementById("q-skip").addEventListener("click", async () => {
      app.closeModal();
      if (latest && latest.my_request && latest.my_request.request_id === requestId) return;
      await post("/api/connect/decline", { request_id: requestId }).catch(() => {});
    });
    send.addEventListener("click", async () => {
      try {
        await post("/api/connect/answer", { request_id: requestId, answers: picked });
        app.closeModal();
        app.agentNote("Connect People", "Thanks! I'll show your match % here as soon as the other person answers too.");
        poll();
      } catch (e) {
        const err = document.getElementById("q-error"); err.hidden = false; err.textContent = e.message;
      }
    });
  }

  function inviteModal(invite) {
    const who = `${escapeHtml(invite.from.nickname)}${invite.from.table ? ` (table ${invite.from.table})` : ""}`;
    app.openModal(`
      <svg class="two-stars" viewBox="0 0 120 46" aria-hidden="true"><circle cx="10" cy="30" r="4"/><circle cx="110" cy="16" r="4"/><path d="M14 29 Q60 2 106 17"/></svg>
      <h3>${who} would like to connect</h3>
      <p class="modal-sub">Answer three questions about coffee to see how well you match. Only your nickname and table are shared.</p>
      <div class="modal-actions">
        <button class="btn" type="button" id="inv-no">Not now</button>
        <button class="btn btn-primary" type="button" id="inv-yes" data-autofocus>Answer three questions</button>
      </div>`);
    document.getElementById("inv-yes").addEventListener("click", () => questionsModal(invite.request_id, `You and ${who} answer the same questions.`));
    document.getElementById("inv-no").addEventListener("click", async () => {
      app.closeModal();
      await post("/api/connect/decline", { request_id: invite.request_id }).catch(() => {});
    });
  }

  function confetti() {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const layer = document.createElement("div");
    layer.className = "confetti";
    const colors = ["#f3e6d0", "#5b74ff", "#c8336f", "#c9a46a", "#9db0ff"];
    for (let i = 0; i < 70; i++) {
      const p = document.createElement("i");
      p.style.left = Math.random() * 100 + "vw";
      p.style.background = colors[i % colors.length];
      p.style.setProperty("--x", (Math.random() * 30 - 15) + "vw");
      p.style.setProperty("--r", (Math.random() * 900 - 450) + "deg");
      p.style.setProperty("--t", (2 + Math.random() * 1.6) + "s");
      p.style.animationDelay = Math.random() * 0.4 + "s";
      layer.appendChild(p);
    }
    document.body.appendChild(layer);
    setTimeout(() => layer.remove(), 4200);
  }

  function countUp(el, target) {
    const start = performance.now();
    const step = (now) => {
      const t = Math.min(1, (now - start) / 1100);
      el.textContent = Math.round(target * (1 - Math.pow(1 - t, 3)));
      if (t < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }

  function matchModal(m) {
    const who = `${escapeHtml(m.with_profile.nickname)}${m.with_profile.table ? ` · table ${m.with_profile.table}` : ""}`;
    const shared = m.shared.length ? `You both chose ${m.shared.map(escapeHtml).join(" and ").toLowerCase()}.` : "Different tastes — the best conversations start there.";
    app.openModal(`
      <svg class="two-stars" viewBox="0 0 120 46" aria-hidden="true"><circle cx="10" cy="30" r="4"/><circle cx="110" cy="16" r="4"/><path d="M14 29 Q60 2 106 17"/></svg>
      <p class="modal-sub" style="margin:0">A kindred spirit, with</p>
      <h3>${who}</h3>
      <div class="match-big"><span id="match-num">0</span>%</div>
      <p class="modal-sub">${shared}</p>
      <div class="modal-note"><em>${escapeHtml(m.icebreaker)}</em></div>
      <div class="modal-actions"><button class="btn btn-primary" type="button" data-close data-autofocus>Say hello</button></div>`);
    countUp(document.getElementById("match-num"), m.percent);
    confetti();
    app.agentNote("Connect People", `A <b>${m.percent}% match</b> with <b>${who}</b>. ${shared}<br><em>“${escapeHtml(m.icebreaker)}”</em>`);
  }

  // ---------------------------------------------------------------- flow

  async function requestConnect() {
    let r;
    try { r = await post("/api/connect/request"); } catch (e) { app.agentNote("Connect People", escapeHtml(e.message)); return; }
    if (r.needs_signup) return signupModal();
    app.agentNote("Connect People", `I've let <b>${r.people_notified}</b> ${r.people_notified === 1 ? "person" : "people"} here know you'd like to connect. Answer 3 quick questions — your match % appears when someone answers.`);
    await poll();
    if (!r.answered) questionsModal(r.request_id, "Your answers are matched against everyone who responds.");
  }

  async function poll() {
    try { latest = await post("/api/connect/state"); } catch { return; }
    renderButton(latest.signed_up);
    if (!latest.signed_up) { stopPolling(); return; }
    if (app.isModalOpen()) return; // never interrupt the customer; retry on the next poll
    const invite = latest.invites.find((i) => !seen.invites.has(i.request_id));
    if (invite) { seen.invites.add(invite.request_id); return inviteModal(invite); }
    const match = latest.matches.find((m) => !seen.matches.has(m.id));
    if (match) { seen.matches.add(match.id); matchModal(match); }
  }

  function startPolling() { if (!polling) { poll(); polling = setInterval(poll, 3500); } }
  function stopPolling() { clearInterval(polling); polling = null; }

  function renderButton(signedUp) {
    const btn = document.getElementById("connect-btn");
    if (btn) btn.querySelector(".label").textContent = signedUp ? "Connect me with people" : "Meet people here";
    const panel = document.getElementById("connect-panel-btn");
    if (panel) panel.hidden = !signedUp;
  }

  window.CFConnect = {
    handleAction(action) {
      if (action.action === "signup") signupModal();
      if (action.action === "answer") poll().then(() => questionsModal(action.request_id, "Your answers are matched against everyone who responds."));
    },
  };

  document.getElementById("connect-btn").addEventListener("click", async () => {
    await poll();
    if (latest && latest.signed_up) requestConnect(); else signupModal();
  });
  document.getElementById("connect-panel-btn").addEventListener("click", async () => { await poll(); panelModal(); });

  // Resume for returning tabs (state lives on the server, keyed by this browser's session);
  // matches announced earlier are not shown again.
  (async () => {
    try { latest = await post("/api/connect/state"); } catch { return; }
    renderButton(latest.signed_up);
    if (latest.signed_up) { latest.matches.forEach((m) => seen.matches.add(m.id)); startPolling(); }
  })();
})();

// Guest Radar hub. Reads from Supabase when configured, otherwise from data/pilot.json.
(function () {
  const cfg = window.RADAR || {};
  const live = Boolean(cfg.supabaseUrl && cfg.anonKey);
  const $ = (id) => document.getElementById(id);

  const state = { runs: [], run: null, guests: [], feedback: {}, filter: "All" };

  // ---------- data layer ----------
  async function rest(path, opts = {}) {
    const res = await fetch(`${cfg.supabaseUrl}/rest/v1/${path}`, {
      ...opts,
      headers: {
        apikey: cfg.anonKey,
        ...(cfg.anonKey.startsWith("sb_") ? {} : { Authorization: `Bearer ${cfg.anonKey}` }),
        "Content-Type": "application/json",
        ...(opts.headers || {}),
      },
    });
    if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
    return res.status === 204 || res.status === 201 ? null : res.json();
  }

  async function loadRuns() {
    if (!live) {
      const pilot = await (await fetch("data/pilot.json")).json();
      state.pilot = pilot;
      return [pilot.run];
    }
    return rest("runs?select=*&published=eq.true&order=week_of.desc");
  }

  async function loadGuests(run) {
    if (!live) return state.pilot.guests;
    return rest(`guests?select=*&run_id=eq.${run.id}&order=position.asc`);
  }

  async function loadFeedback(guests) {
    const out = {};
    if (!live) {
      guests.forEach((g) => {
        const s = localStorage.getItem(`fb:${g.id}`);
        if (s) out[g.id] = JSON.parse(s);
      });
      return out;
    }
    if (!guests.length) return out;
    const ids = guests.map((g) => g.id).join(",");
    const rows = await rest(`feedback?select=guest_id,verdict,note,created_at&guest_id=in.(${ids})&order=created_at.asc`);
    rows.forEach((r) => (out[r.guest_id] = { verdict: r.verdict, note: r.note }));
    return out;
  }

  async function saveFeedback(guestId, verdict, note) {
    state.feedback[guestId] = { verdict, note };
    if (!live) {
      localStorage.setItem(`fb:${guestId}`, JSON.stringify({ verdict, note }));
      return;
    }
    await rest("feedback", {
      method: "POST",
      headers: { Prefer: "return=minimal" },
      body: JSON.stringify({ guest_id: guestId, verdict, note: note || null }),
    });
  }

  // ---------- helpers ----------
  function ytId(url) {
    if (!url) return null;
    const m = url.match(/(?:youtu\.be\/|v=|embed\/|shorts\/)([\w-]{11})/);
    return m ? m[1] : null;
  }
  function initials(name) {
    return name.replace(/^(Prof\.|Dr\.)\s*/i, "").split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0]).join("").toUpperCase();
  }
  function fmtWeek(d) {
    return new Date(d + "T12:00:00").toLocaleDateString("en-US", { month: "long", day: "numeric", year: "numeric" });
  }
  function fmtTime(s) {
    if (!isFinite(s)) return "0:00";
    const m = Math.floor(s / 60), r = Math.floor(s % 60);
    return `${m}:${String(r).padStart(2, "0")}`;
  }

  // ---------- rendering ----------
  function renderHeader() {
    const r = state.run;
    $("weekLabel").textContent = `Week of ${fmtWeek(r.week_of)}`;
    $("runTitle").textContent = r.title;
    $("runSummary").textContent = r.summary || "";
    document.title = `Guest Radar · ${fmtWeek(r.week_of)}`;

    const audio = $("audio");
    audio.pause();
    $("playIcon").setAttribute("d", "M8 5v14l11-7z");
    $("seek").value = 0;
    if (r.audio_url) {
      audio.src = r.audio_url;
      $("player").classList.remove("disabled");
      $("playBtn").disabled = false;
      $("playerSub").textContent = r.audio_seconds ? `${fmtTime(r.audio_seconds)} · this week's picks` : "This week's picks";
    } else {
      audio.removeAttribute("src");
      $("player").classList.add("disabled");
      $("playBtn").disabled = true;
      $("playerSub").textContent = "Audio briefing arrives with the first automatic run";
    }
  }

  function renderFilters() {
    const cats = ["All", ...new Set(state.guests.map((g) => g.category))];
    const nav = $("filters");
    nav.innerHTML = "";
    cats.forEach((c) => {
      const b = document.createElement("button");
      b.textContent = c;
      b.className = c === state.filter ? "on" : "";
      b.onclick = () => { state.filter = c; renderFilters(); renderCards(); };
      nav.appendChild(b);
    });
  }

  function renderMedia(el, g) {
    const id = ytId(g.video_url);
    el.innerHTML = "";
    if (id) {
      const img = document.createElement("img");
      img.src = `https://i.ytimg.com/vi/${id}/hqdefault.jpg`;
      img.alt = g.video_title || `Video of ${g.name}`;
      const btn = document.createElement("button");
      btn.className = "playover";
      btn.innerHTML = `<span>▶ ${g.video_title ? escapeHtml(g.video_title).slice(0, 70) : "Watch " + escapeHtml(g.name)}</span>`;
      btn.onclick = () => {
        el.innerHTML = `<iframe src="https://www.youtube-nocookie.com/embed/${id}?autoplay=1" allow="autoplay; encrypted-media; picture-in-picture" allowfullscreen title="${escapeHtml(g.name)}"></iframe>`;
      };
      el.append(img, btn);
    } else {
      const d = document.createElement("div");
      d.className = "initials";
      d.textContent = initials(g.name);
      const a = document.createElement("a");
      a.className = "find";
      a.target = "_blank";
      a.rel = "noopener";
      a.href = `https://www.youtube.com/results?search_query=${encodeURIComponent(g.name + " " + (g.org || "") + " interview")}`;
      a.textContent = "▶ Find talks on YouTube";
      el.append(d, a);
    }
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function renderCards() {
    const grid = $("grid");
    grid.innerHTML = "";
    const list = state.guests.filter((g) => state.filter === "All" || g.category === state.filter);
    if (!list.length) {
      grid.innerHTML = `<p class="empty">No suggestions here yet.</p>`;
      return;
    }
    const tpl = $("cardTpl");
    list.forEach((g) => {
      const node = tpl.content.firstElementChild.cloneNode(true);
      renderMedia(node.querySelector(".media"), g);
      node.querySelector(".tag").textContent = g.category;
      node.querySelector(".name").textContent = g.name;
      node.querySelector(".role").textContent = [g.role, g.org].filter(Boolean).join(" · ");
      if (g.previous_note) {
        const p = node.querySelector(".prev");
        p.hidden = false;
        p.textContent = g.previous_note;
      }
      node.querySelector(".why").textContent = g.why_now;
      node.querySelector(".angle").textContent = g.angle;
      const src = node.querySelector(".sources");
      (g.sources || []).forEach((s) => {
        const a = document.createElement("a");
        a.href = s.url; a.target = "_blank"; a.rel = "noopener";
        a.textContent = `↗ ${s.title}`;
        src.appendChild(a);
      });

      const fb = state.feedback[g.id] || {};
      const note = node.querySelector(".note");
      note.value = fb.note || "";
      const saved = node.querySelector(".saved");
      const paint = () => {
        const v = (state.feedback[g.id] || {}).verdict;
        node.className = "card" + (v ? ` v-${v}` : "");
        node.querySelectorAll(".actions button").forEach((b) => b.classList.toggle("on", b.dataset.v === v));
      };
      paint();
      const submit = async (verdict) => {
        try {
          await saveFeedback(g.id, verdict, note.value.trim());
          saved.textContent = "Saved ✓";
        } catch (e) {
          saved.textContent = "Couldn't save — please try again";
          console.error(e);
        }
        paint();
        renderShortlist();
      };
      node.querySelectorAll(".actions button").forEach((b) => (b.onclick = () => submit(b.dataset.v)));
      note.addEventListener("change", () => {
        const v = (state.feedback[g.id] || {}).verdict;
        if (v) submit(v);
        else saved.textContent = "Pick 👍 / 👎 / ✓ to save the note";
      });
      grid.appendChild(node);
    });
  }

  function renderShortlist() {
    const picks = state.guests.filter((g) => (state.feedback[g.id] || {}).verdict === "interested");
    $("shortlistBox").hidden = !picks.length;
    $("shortlist").innerHTML = picks
      .map((g) => `<li><strong>${escapeHtml(g.name)}</strong> — ${escapeHtml([g.role, g.org].filter(Boolean).join(", "))}</li>`)
      .join("");
  }

  // ---------- audio player ----------
  function wirePlayer() {
    const audio = $("audio"), seek = $("seek");
    const speeds = [1, 1.25, 1.5, 0.85];
    let si = 0;
    $("playBtn").onclick = () => (audio.paused ? audio.play() : audio.pause());
    audio.onplay = () => $("playIcon").setAttribute("d", "M6 5h4v14H6zM14 5h4v14h-4z");
    audio.onpause = () => $("playIcon").setAttribute("d", "M8 5v14l11-7z");
    audio.ontimeupdate = () => {
      if (audio.duration) {
        seek.value = (audio.currentTime / audio.duration) * 100;
        $("playerSub").textContent = `${fmtTime(audio.currentTime)} / ${fmtTime(audio.duration)}`;
      }
    };
    seek.oninput = () => { if (audio.duration) audio.currentTime = (seek.value / 100) * audio.duration; };
    $("speedBtn").onclick = () => {
      si = (si + 1) % speeds.length;
      audio.playbackRate = speeds[si];
      $("speedBtn").textContent = `${speeds[si]}×`;
    };
  }

  // ---------- boot ----------
  async function openRun(run) {
    state.run = run;
    state.filter = "All";
    state.guests = await loadGuests(run);
    state.feedback = await loadFeedback(state.guests);
    renderHeader();
    renderFilters();
    renderCards();
    renderShortlist();
    const u = new URL(location.href);
    u.searchParams.set("week", run.id);
    history.replaceState(null, "", u);
  }

  async function boot() {
    wirePlayer();
    try {
      state.runs = await loadRuns();
    } catch (e) {
      $("runTitle").textContent = "Couldn't load the radar right now.";
      console.error(e);
      return;
    }
    if (!state.runs.length) {
      $("runTitle").textContent = "The first weekly radar is on its way.";
      return;
    }
    const sel = $("weekSelect");
    sel.innerHTML = state.runs.map((r) => `<option value="${r.id}">${fmtWeek(r.week_of)}</option>`).join("");
    const wanted = new URL(location.href).searchParams.get("week");
    const first = state.runs.find((r) => String(r.id) === wanted) || state.runs[0];
    sel.value = first.id;
    sel.onchange = () => openRun(state.runs.find((r) => String(r.id) === sel.value));
    await openRun(first);
  }

  boot();
})();

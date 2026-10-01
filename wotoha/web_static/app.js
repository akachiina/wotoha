/* wotoha web front-end (vanilla JS) */

const $ = (sel) => document.querySelector(sel);
const state = {
  view: "search",
  source: "*",
  query: "",
  page: 1,
  lang: "",
  mangaId: null,
  chapters: [],
  reader: { pages: 0, chapter: "", title: "" },
  pollTimer: null,
};

/* ---------- helpers ---------- */

async function api(path, params = {}) {
  const qs = new URLSearchParams({ lang: state.lang, ...params }).toString();
  const res = await fetch(`${path}${qs ? "?" + qs : ""}`);
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

function toast(msg, ms = 2600) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.remove("hidden");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.add("hidden"), ms);
}

function show(view) {
  state.view = view;
  for (const v of document.querySelectorAll(".view")) v.classList.add("hidden");
  $(`#view-${view}`).classList.remove("hidden");
  for (const b of document.querySelectorAll(".nav-btn"))
    b.classList.toggle("active", b.dataset.view === view);
  if (view !== "downloads" && state.pollTimer) {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
  if (view === "downloads") pollJobs();
}

/* ---------- search ---------- */

async function loadSources() {
  const sources = await api("/api/sources");
  const sel = $("#source-select");
  sel.innerHTML =
    `<option value="*">All sources</option>` +
    sources
      .map((s) => `<option value="${s.id}">${s.name}</option>`)
      .join("");
}

async function doSearch(page = 1) {
  state.page = page;
  const q = $("#search-input").value.trim();
  if (!q) return;
  $("#results").innerHTML = "";
  $("#search-meta").textContent = "Searching…";
  try {
    const params = { q, page, source: state.source };
    if (state.lang) params.lang = state.lang;
    const data = await api("/api/search", params);
    $("#search-meta").textContent = `${data.mangaList.length} results (page ${page})`;
    $("#page-label").textContent = `page ${page}`;
    renderResults(data.mangaList);
  } catch (e) {
    $("#search-meta").textContent = `error: ${e.message}`;
  }
}

function coverUrl(m) {
  return m.cover
    ? `/api/cover?url=${encodeURIComponent(m.cover)}&source=${encodeURIComponent(m.sourceId || "")}`
    : "";
}

function renderResults(list) {
  const el = $("#results");
  if (!list.length) {
    el.innerHTML = `<div class="empty"><img class="jelly-big" src="/static/favicon.svg" alt="">no results… the jelly found nothing</div>`;
    return;
  }
  el.innerHTML = list
    .map((m) => {
      const cover = coverUrl(m);
      return `
      <div class="card" data-id="${m.id}" data-title="${escapeAttr(m.title)}">
        ${cover ? `<img loading="lazy" src="${cover}" alt="">` : `<div class="card-cover-empty"></div>`}
        <div class="card-title">${escapeHtml(m.title)}</div>
        <div class="card-source">${m.sourceId || ""}</div>
      </div>`;
    })
    .join("");
  for (const card of el.querySelectorAll(".card")) {
    card.onclick = () => openManga(card.dataset.id, card.dataset.title);
  }
}

function escapeHtml(s) {
  return (s || "").replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
}
function escapeAttr(s) {
  return escapeHtml(s).replace(/`/g, "&#96;");
}

/* ---------- manga details ---------- */

async function openManga(mangaId, titleHint = "") {
  show("manga");
  $("#manga-title").textContent = titleHint || "Loading…";
  $("#manga-desc").textContent = "";
  $("#manga-meta").textContent = "Loading details…";
  $("#chapter-list").innerHTML = "";
  $("#manga-cover").removeAttribute("src");
  state.mangaId = mangaId;
  try {
    const d = await api("/api/manga", { id: mangaId });
    state.chapters = d.chapters || [];
    state.mangaTitle = d.title;
    $("#manga-title").textContent = d.title;
    if (d.cover) $("#manga-cover").src = coverUrl(d);
    const langs = {};
    for (const c of state.chapters) {
      const l = c.language || "?";
      langs[l] = (langs[l] || 0) + 1;
    }
    const langStr = Object.entries(langs)
      .map(([k, v]) => `${k}:${v}`)
      .join(", ");
    $("#manga-meta").innerHTML = [
      d.authors?.length ? `<b>Author:</b> ${escapeHtml(d.authors.join(", "))}` : "",
      d.status ? `<b>Status:</b> ${escapeHtml(d.status)}` : "",
      d.url ? `<a href="${escapeAttr(d.url)}" target="_blank">${escapeHtml(d.url)}</a>` : "",
      `<b>Chapters:</b> ${state.chapters.length}${langStr ? ` (${langStr})` : ""}`,
      d.tags?.length ? `<b>Tags:</b> ${escapeHtml(d.tags.join(", "))}` : "",
    ]
      .filter(Boolean)
      .join("<br>");
    $("#manga-desc").textContent = d.description || "";
    renderChapters();
  } catch (e) {
    $("#manga-meta").textContent = `error: ${e.message}`;
  }
}

function chapterLabel(c, i) {
  const num = c.chapterNumber;
  const base =
    num !== null && num !== undefined
      ? `Ch. ${Number.isInteger(num) ? num : num.toFixed(2)}`
      : c.title || `Chapter ${i + 1}`;
  return base + (num !== null && c.title && c.title !== base ? ` — ${c.title}` : "");
}

function renderChapters() {
  const el = $("#chapter-list");
  el.innerHTML = state.chapters
    .map((c, i) => {
      const date = c.dateUploaded
        ? new Date(c.dateUploaded * 1000).toISOString().slice(0, 10)
        : "";
      return `
      <div class="chapter-row" data-i="${i}">
        <input type="checkbox" class="ch-check" checked>
        <span class="ch-name">${escapeHtml(chapterLabel(c, i))}</span>
        <span class="ch-lang">${c.language || ""}</span>
        <span class="ch-date">${date}</span>
      </div>`;
    })
    .join("");
  for (const row of el.querySelectorAll(".chapter-row")) {
    row.onclick = (ev) => {
      if (ev.target instanceof HTMLInputElement) return;
      openReader(state.chapters[row.dataset.i], +row.dataset.i);
    };
  }
}

function selectedChapters() {
  return [...document.querySelectorAll(".chapter-row")]
    .filter((row) => row.querySelector(".ch-check").checked)
    .map((row) => state.chapters[+row.dataset.i].key);
}

/* ---------- reader ---------- */

async function openReader(chapter, index) {
  show("reader");
  const el = $("#reader-pages");
  el.innerHTML = `<div class="empty"><img class="jelly-big" src="/static/favicon.svg" alt="">loading pages…</div>`;
  $("#reader-title").textContent = chapterLabel(chapter, index);
  try {
    const data = await api("/api/pages", {
      id: state.mangaId,
      chapter: chapter.key,
    });
    const count = data.count;
    state.reader = { pages: count, chapter: chapter.key };
    el.innerHTML = Array.from(
      { length: count },
      (_, n) =>
        `<img loading="lazy" src="/api/page?id=${encodeURIComponent(state.mangaId)}&chapter=${encodeURIComponent(chapter.key)}&n=${n}" data-n="${n}">`
    ).join("");
    for (const img of el.querySelectorAll("img")) {
      img.onload = () => updatePageCounter();
      img.onerror = () => (img.style.minHeight = "40px");
    }
    window.scrollTo(0, 0);
  } catch (e) {
    el.innerHTML = `<div class="empty">error: ${e.message}</div>`;
  }
}

function updatePageCounter() {
  const imgs = [...document.querySelectorAll("#reader-pages img")];
  let loaded = 0;
  for (const img of imgs) if (img.complete && img.naturalWidth > 0) loaded++;
  $("#reader-page").textContent =
    state.reader.pages ? `${loaded}/${state.reader.pages} pages` : "";
}

/* ---------- downloads ---------- */

async function enqueueDownload(chapterKeys) {
  if (!chapterKeys.length) {
    toast("No chapters selected");
    return;
  }
  try {
    const res = await fetch("/api/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        mangaId: state.mangaId,
        chapters: chapterKeys,
        format: $("#dl-format").value,
        mode: $("#dl-mode").value,
        lang: state.lang,
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || res.statusText);
    toast(`Queued ${chapterKeys.length} chapter(s) 🪼`);
    show("downloads");
  } catch (e) {
    toast(`Error: ${e.message}`);
  }
}

function pollJobs() {
  const refresh = async () => {
    try {
      const jobs = await api("/api/jobs");
      renderJobs(jobs);
    } catch { /* server gone; ignore */ }
  };
  refresh();
  if (!state.pollTimer) state.pollTimer = setInterval(refresh, 1200);
}

function renderJobs(jobs) {
  const el = $("#jobs");
  if (!jobs.length) {
    el.innerHTML = `<div class="empty"><img class="jelly-big" src="/static/favicon.svg" alt="">no downloads yet</div>`;
    return;
  }
  el.innerHTML = jobs
    .map(
      (j) => `
    <div class="job">
      <div class="job-top">
        <span class="job-title">${escapeHtml(j.title)}</span>
        <span class="badge ${j.state}">${j.state}</span>
        <span class="muted">${j.format.toUpperCase()} · ${j.mode}</span>
        ${j.state === "running" || j.state === "queued"
          ? `<button class="danger" data-cancel="${j.id}">Cancel</button>`
          : ""}
      </div>
      <div class="progress"><div style="width:${j.total ? (100 * j.done) / j.total : 0}%"></div></div>
      <div class="job-sub">${j.done}/${j.total}${j.current ? ` — ${escapeHtml(j.current)}` : ""}${j.error ? ` — ${escapeHtml(j.error)}` : ""}</div>
    </div>`
    )
    .join("");
  for (const btn of el.querySelectorAll("[data-cancel]")) {
    btn.onclick = async () => {
      await fetch("/api/jobs/cancel", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: +btn.dataset.cancel }),
      });
      refreshJobsIfVisible();
    };
  }
}

function refreshJobsIfVisible() {
  if (state.view === "downloads") pollJobs();
}

/* ---------- theme ---------- */

function initTheme() {
  const saved = localStorage.getItem("wotoha-theme");
  if (saved === "dark") document.body.classList.add("dark");
  $("#theme-toggle").textContent = document.body.classList.contains("dark") ? "☀" : "☾";
  $("#theme-toggle").onclick = () => {
    document.body.classList.toggle("dark");
    const dark = document.body.classList.contains("dark");
    localStorage.setItem("wotoha-theme", dark ? "dark" : "light");
    $("#theme-toggle").textContent = dark ? "☀" : "☾";
  };
}

/* ---------- wiring ---------- */

document.addEventListener("DOMContentLoaded", async () => {
  initTheme();
  await loadSources();
  show("search");

  $("#search-form").onsubmit = (ev) => {
    ev.preventDefault();
    state.source = $("#source-select").value;
    doSearch(1);
  };
  $("#prev-page").onclick = () => state.page > 1 && doSearch(state.page - 1);
  $("#next-page").onclick = () => doSearch(state.page + 1);

  for (const btn of document.querySelectorAll(".nav-btn")) {
    btn.onclick = () => show(btn.dataset.view);
  }
  for (const btn of document.querySelectorAll(".back-btn")) {
    btn.onclick = () => show(btn.dataset.back);
  }

  $("#dl-selected").onclick = () => enqueueDownload(selectedChapters());
  $("#dl-all").onclick = () =>
    enqueueDownload(state.chapters.map((c) => c.key));
});

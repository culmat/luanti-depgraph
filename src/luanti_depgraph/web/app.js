// Luanti dependency explorer: overview graph (sigma.js), per-package trees (Cytoscape),
// details and SQL over the luanti-depgraph SQLite snapshot (sql.js). Data comes from
// `ldg site`; graph and compatibility are precomputed by the same resolver as `ldg resolve`.
import Graph from "graphology";
import Sigma from "sigma";
import forceAtlas2 from "fa2";
import FA2Layout from "fa2-worker";
import cytoscape from "cytoscape";
import cytoscapeDagre from "cytoscape-dagre";

cytoscape.use(cytoscapeDagre);

const KIND = { HARD: 0, OPTIONAL: 1, PROVIDED_BY: 2 };
const STATUS_LABEL = { o: "ok", r: "risky", u: "unsatisfiable" };
const state = {
  db: null, graph: null, sigma: null, compat: null, meta: null, game: "", gameIndex: -1,
  selected: null, hovered: null, neighbors: new Set(), showOptional: false, layout: null,
  suggestions: [], suggestionIndex: -1, searchIndex: [], gameProvides: new Map(),
};

const $ = (sel) => document.querySelector(sel);
const panel = $("#panel");
const statusEl = $("#status");

function css(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function h(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

function q(sql, params = []) {
  const stmt = state.db.prepare(sql);
  stmt.bind(params);
  const rows = [];
  while (stmt.step()) rows.push(stmt.getAsObject());
  stmt.free();
  return rows;
}

function storageGet(key) {
  try { return localStorage.getItem(key); } catch { return null; }
}
function storageSet(key, value) {
  try { localStorage.setItem(key, value); } catch { /* private mode or quota: fine */ }
}

// ------------------------------------------------------------------ loading

async function fetchWithProgress(url, label) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${url}: HTTP ${response.status}`);
  const total = Number(response.headers.get("content-length")) || 0;
  if (!response.body || !total) return response.arrayBuffer();
  const reader = response.body.getReader();
  const parts = [];
  let received = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    parts.push(value);
    received += value.length;
    statusEl.textContent = `Loading ${label}… ${Math.round((100 * received) / total)}% of ${(total / 1e6).toFixed(1)} MB`;
  }
  const buffer = new Uint8Array(received);
  let offset = 0;
  for (const part of parts) { buffer.set(part, offset); offset += part.length; }
  return buffer.buffer;
}

async function load() {
  statusEl.textContent = "Loading…";
  const sqlReady = initSqlJs({ locateFile: (f) => `https://cdn.jsdelivr.net/npm/sql.js@1.14.2/dist/${f}` });
  const [graphData, compat, meta, dbBuf, SQL] = await Promise.all([
    fetch("data/graph.json").then((r) => r.json()),
    fetch("data/compat.json").then((r) => r.json()),
    fetch("data/meta.json").then((r) => r.json()),
    fetchWithProgress("data/luanti-deps.sqlite", "database"),
    sqlReady,
  ]);
  statusEl.textContent = "Preparing graph…";
  state.compat = compat;
  state.meta = meta;
  state.db = new SQL.Database(new Uint8Array(dbBuf));
  buildGraph(graphData);
  buildSearchIndex();
  setupGameSelect();
  setupSigma();
  layout();
  window.addEventListener("hashchange", route);
  route();
}

// ------------------------------------------------------------------ graph

function mulberry32(seed) {
  return () => {
    seed |= 0; seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function layoutKey() {
  return `ldg-layout:${state.meta.built_at}:${state.graph.order}:${state.graph.size}`;
}

function buildGraph(data) {
  // Packages without any dependency edge would only fill the canvas; they stay searchable.
  const degree = new Map();
  data.edges.forEach(([s, t]) => { degree.set(s, (degree.get(s) || 0) + 1); degree.set(t, (degree.get(t) || 0) + 1); });
  const indegree = new Map();
  data.edges.forEach(([, t, kind]) => { if (kind !== KIND.PROVIDED_BY) indegree.set(t, (indegree.get(t) || 0) + 1); });
  const graph = new Graph({ multi: false, type: "directed" });
  const rand = mulberry32(42);
  data.nodes.forEach(([key, label, kind, score, downloads], i) => {
    if (!degree.get(i)) return;
    const size = kind === "api" ? 3 + Math.log2(1 + (indegree.get(i) || 0)) * 1.4 : 1.5 + Math.log10(1 + downloads) * 1.4;
    graph.addNode(key, { label, kind, score, downloads, size, x: rand() * 100, y: rand() * 100 });
  });
  data.edges.forEach(([s, t, kind]) => {
    const source = data.nodes[s][0];
    const target = data.nodes[t][0];
    if (!graph.hasEdge(source, target)) graph.addEdge(source, target, { kind, size: kind === KIND.HARD ? 0.6 : 0.3 });
  });
  state.hiddenCount = data.nodes.length - graph.order;
  state.graph = graph;
}

function nodeColor(key, attrs) {
  if (state.gameIndex >= 0) {
    const game = state.compat.games[state.gameIndex];
    if (attrs.kind === "mod") {
      const code = (state.compat.status[key] || "")[state.gameIndex];
      return code === "o" ? css("--ok") : code === "r" ? css("--risky") : code === "u" ? css("--bad") : css("--na");
    }
    if (attrs.kind === "game") return key === game.key ? css("--accent") : css("--na");
    if (attrs.kind === "api") return gameProvides(game.key).has(attrs.label) ? css("--ok") : css("--bad");
    return css("--na");
  }
  return attrs.kind === "game" ? css("--game") : attrs.kind === "api" ? css("--api") : css("--mod");
}

function setupSigma() {
  const container = $("#graph");
  state.sigma = new Sigma(state.graph, container, {
    renderEdgeLabels: false,
    labelRenderedSizeThreshold: 7,
    labelColor: { color: css("--text") },
    defaultEdgeColor: css("--edge"),
    zIndex: true,
    nodeReducer: (node, attrs) => {
      const res = { ...attrs, color: nodeColor(node, attrs) };
      const focus = state.hovered || state.selected;
      if (focus && node !== focus && !state.neighbors.has(node)) {
        res.color = css("--border");
        res.label = "";
        res.zIndex = 0;
      } else if (focus) {
        res.zIndex = 2;
        res.forceLabel = node === focus || state.neighbors.size < 40;
      }
      if (node === state.selected) res.highlighted = true;
      return res;
    },
    edgeReducer: (edge, attrs) => {
      const res = { ...attrs, color: css("--edge") };
      if (attrs.kind === KIND.OPTIONAL && !state.showOptional) res.hidden = true;
      const focus = state.hovered || state.selected;
      if (focus) {
        const [s, t] = state.graph.extremities(edge);
        if (s === focus || t === focus) {
          res.color = attrs.kind === KIND.HARD ? css("--accent") : css("--muted");
          res.size = 1.2;
          res.zIndex = 2;
        } else {
          res.hidden = true;
        }
      }
      return res;
    },
  });
  state.sigma.on("enterNode", ({ node }) => { state.hovered = node; refreshFocus(); });
  state.sigma.on("leaveNode", () => { state.hovered = null; refreshFocus(); });
  state.sigma.on("clickNode", ({ node }) => { location.hash = nodeHash(node); });
  state.sigma.on("clickStage", () => { if (state.selected) location.hash = "#/"; });
  renderLegend();
}

function refreshFocus() {
  const focus = state.hovered || state.selected;
  state.neighbors = focus ? new Set(state.graph.neighbors(focus)) : new Set();
  state.sigma.refresh({ skipIndexation: true });
}

function nodeHash(key) {
  return key.startsWith("api:") ? `#/m/${encodeURIComponent(key.slice(4))}` : `#/p/${key}`;
}

function layout(force = false) {
  const saved = force ? null : JSON.parse(storageGet(layoutKey()) || "null");
  if (saved) {
    for (const [key, x, y] of saved) {
      if (state.graph.hasNode(key)) state.graph.mergeNodeAttributes(key, { x, y });
    }
    statusEl.textContent = "";
    state.sigma.refresh();
    state.sigma.getCamera().setState({ x: 0.5, y: 0.5, ratio: 1, angle: 0 });
    return;
  }
  if (state.layout) state.layout.kill();
  const settings = { ...forceAtlas2.inferSettings(state.graph), barnesHutOptimize: true, gravity: 0.6,
    scalingRatio: 12, slowDown: 4, adjustSizes: false };
  state.layout = new FA2Layout(state.graph, { settings });
  state.layout.start();
  let remaining = 9;
  statusEl.textContent = `Laying out ${state.graph.order} nodes… ${remaining}s`;
  const timer = setInterval(() => {
    remaining -= 1;
    if (remaining > 0) {
      statusEl.textContent = `Laying out ${state.graph.order} nodes… ${remaining}s`;
      return;
    }
    clearInterval(timer);
    state.layout.stop();
    statusEl.textContent = "";
    const positions = state.graph.mapNodes((key, a) => [key, Math.round(a.x * 100) / 100, Math.round(a.y * 100) / 100]);
    storageSet(layoutKey(), JSON.stringify(positions));
    if (state.selected) focusCamera(state.selected);
    else state.sigma.getCamera().animatedReset({ duration: 400 });
  }, 1000);
}

function focusCamera(node) {
  const display = state.sigma.getNodeDisplayData(node);
  if (display) state.sigma.getCamera().animate({ x: display.x, y: display.y, ratio: 0.35 }, { duration: 500 });
}

function renderLegend() {
  const item = (color, label) => `<div><span class="dot" style="background:${color}"></span>${h(label)}</div>`;
  if (state.gameIndex >= 0) {
    const game = state.compat.games[state.gameIndex];
    $("#legend").innerHTML = `<strong>In ${h(game.title)}</strong>` + item(css("--ok"), "mod works") +
      item(css("--risky"), "installable, uses undeclared mods") + item(css("--bad"), "can't be installed") +
      item(css("--accent"), "the game") + item(css("--na"), "other games / unknown");
  } else {
    $("#legend").innerHTML = item(css("--mod"), "mod / modpack") + item(css("--game"), "game") +
      item(css("--api"), "API only games provide (e.g. default)") +
      `<div class="note">Size: downloads (API: dependers). Arrow: depends on.<br>${state.hiddenCount} packages without dependency links not drawn.</div>`;
  }
}

// ------------------------------------------------------------------ games

function setupGameSelect() {
  const select = $("#game");
  state.compat.games.forEach((g, i) => {
    const opt = document.createElement("option");
    opt.value = String(i);
    opt.textContent = g.title;
    select.append(opt);
  });
  const saved = storageGet("ldg-game");
  if (saved && saved < state.compat.games.length) select.value = saved;
  setGame(select.value);
  select.addEventListener("change", () => { setGame(select.value); storageSet("ldg-game", select.value); route(); });
}

function setGame(value) {
  state.gameIndex = value === "" ? -1 : Number(value);
  state.game = state.gameIndex >= 0 ? state.compat.games[state.gameIndex].key : "";
  if (state.sigma) {
    renderLegend();
    state.sigma.refresh({ skipIndexation: true });
  }
}

function gameProvides(gameKey) {
  if (!state.gameProvides.has(gameKey)) {
    const rows = q("SELECT pr.modname FROM provides pr JOIN package p ON p.id = pr.package_id WHERE p.author || '/' || p.name = ?", [gameKey]);
    state.gameProvides.set(gameKey, new Set(rows.map((r) => r.modname)));
  }
  return state.gameProvides.get(gameKey);
}

// ------------------------------------------------------------------ search

function buildSearchIndex() {
  const entries = [];
  for (const p of q("SELECT author || '/' || name AS key, title, type, score FROM package")) {
    entries.push({ type: p.type, key: p.key, label: p.title, hay: `${p.key} ${p.title}`.toLowerCase(), score: p.score || 0 });
  }
  for (const { modname, n } of q("SELECT modname, count(*) AS n FROM provides GROUP BY modname")) {
    entries.push({ type: "modname", key: modname, label: `${n} provider${n === 1 ? "" : "s"}`, hay: modname, score: 0 });
  }
  state.searchIndex = entries;
  const input = $("#search");
  input.addEventListener("input", () => suggest(input.value));
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const n = state.suggestions.length;
      if (!n) return;
      state.suggestionIndex = (state.suggestionIndex + (e.key === "ArrowDown" ? 1 : -1) + n) % n;
      renderSuggestions();
    } else if (e.key === "Enter") {
      const pick = state.suggestions[Math.max(0, state.suggestionIndex)];
      if (pick) choose(pick);
    } else if (e.key === "Escape") {
      $("#suggestions").hidden = true;
    }
  });
  document.addEventListener("click", (e) => { if (!e.target.closest(".search")) $("#suggestions").hidden = true; });
}

function suggest(text) {
  const t = text.trim().toLowerCase();
  if (t.length < 2) { $("#suggestions").hidden = true; return; }
  const scored = [];
  for (const e of state.searchIndex) {
    const idx = e.hay.indexOf(t);
    if (idx < 0) continue;
    const exact = e.type === "modname" ? e.key === t : e.key.endsWith(`/${t}`);
    scored.push([exact ? 0 : idx === 0 ? 1 : 2, -e.score, e]);
  }
  scored.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  state.suggestions = scored.slice(0, 12).map((s) => s[2]);
  state.suggestionIndex = -1;
  renderSuggestions();
}

function renderSuggestions() {
  const ul = $("#suggestions");
  ul.innerHTML = state.suggestions.map((e, i) =>
    `<li role="option" data-i="${i}" aria-selected="${i === state.suggestionIndex}">
       <span>${e.type === "modname" ? `<code>${h(e.key)}</code>` : h(e.key)}</span>
       <small>${e.type === "modname" ? `mod name · ${h(e.label)}` : `${h(e.type)} · ${h(e.label)}`}</small></li>`).join("");
  ul.hidden = state.suggestions.length === 0;
  ul.querySelectorAll("li").forEach((li) => li.addEventListener("click", () => choose(state.suggestions[li.dataset.i])));
}

function choose(entry) {
  $("#suggestions").hidden = true;
  $("#search").value = "";
  location.hash = entry.type === "modname" ? `#/m/${encodeURIComponent(entry.key)}` : `#/p/${entry.key}`;
}

// ------------------------------------------------------------------ routing

function route() {
  const hash = decodeURIComponent(location.hash.replace(/^#\/?/, ""));
  const [view, ...rest] = hash.split("/");
  let selected = null;
  try {
    if (view === "p" && rest.length >= 2) selected = showPackage(rest.slice(0, 2).join("/"));
    else if (view === "m" && rest[0]) selected = showModname(rest[0]);
    else if (view === "sql") showSql();
    else if (view === "about") showAbout();
    else showHome();
  } catch (err) {
    panel.innerHTML = `<p class="error">${h(err.message)}</p>`;
    console.error(err);
  }
  state.selected = selected && state.graph.hasNode(selected) ? selected : null;
  refreshFocus();
  if (state.selected) focusCamera(state.selected);
}

const link = (key, label) => `<a href="#/p/${h(key)}">${h(label ?? key)}</a>`;
const modLink = (name) => `<a href="#/m/${encodeURIComponent(name)}"><code>${h(name)}</code></a>`;

function bestProvider(name) {
  return q(`SELECT p.author || '/' || p.name AS key, p.name, p.title FROM provides pr JOIN package p ON p.id = pr.package_id
            WHERE pr.modname = ? AND p.type != 'game' ORDER BY (p.name = ?) DESC, p.score DESC LIMIT 1`, [name, name])[0];
}

function hasTable(name) {
  return q("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", [name]).length > 0;
}

// ------------------------------------------------------------------ views

function showHome() {
  const m = state.meta;
  const hubs = q(`SELECT d.modname, count(DISTINCT d.package_id) AS n,
                    (SELECT count(*) FROM provides pr JOIN package g ON g.id = pr.package_id AND g.type = 'game' WHERE pr.modname = d.modname) AS games
                  FROM dep d WHERE d.optional = 0 AND NOT EXISTS (
                    SELECT 1 FROM provides pr JOIN package p ON p.id = pr.package_id AND p.type != 'game' WHERE pr.modname = d.modname)
                  GROUP BY d.modname ORDER BY n DESC LIMIT 12`);
  panel.innerHTML = `
    <h2>Who depends on what, in which game?</h2>
    <p class="sub">${m.packages.toLocaleString()} ContentDB packages (${m.mods} mods, ${m.games} games, ${m.txps} texture packs),
      sources of ${m.corpus_packages} packages analyzed. Snapshot built ${h(m.built_at.slice(0, 10))}.</p>
    <p>In Luanti a dependency names a <em>mod name</em>, not a package. Many packages can provide the same name, and
      games bundle mods, so <code>depends = default</code> works only in games that bundle a <code>default</code> mod.
      Pick a game above to color every mod by whether it can run there.</p>
    <h3>How many mods can run in each game</h3>
    <table><tr><th>game</th><th class="num">ok</th><th class="num">risky</th><th class="num">can't install</th></tr>
      ${gameTotals().map((g) => `<tr><td><a href="#/" data-game="${g.i}">${h(g.title)}</a></td><td class="num">${g.o}</td>
        <td class="num">${g.r}</td><td class="num">${g.u}</td></tr>`).join("")}</table>
    <p class="note">Out of ${Object.keys(state.compat.status).length} mods. Click a game to color the graph for it.</p>
    <h3>Most depended-on mod names that only games provide</h3>
    <table><tr><th>mod name</th><th class="num">hard dependers</th><th class="num">games providing it</th></tr>
      ${hubs.map((r) => `<tr><td>${modLink(r.modname)}</td><td class="num">${r.n}</td><td class="num">${r.games}</td></tr>`).join("")}
    </table>
    <p class="note">Click a node, search above, or try <a href="#/p/mt-mods/pipeworks">mt-mods/pipeworks</a>,
      <a href="#/m/default">default</a> or <a href="#/sql">SQL</a>. Data: <a href="https://content.luanti.org">ContentDB</a>
      via <a href="https://github.com/culmat/luanti-depgraph">luanti-depgraph</a>.</p>`;
  panel.querySelectorAll("[data-game]").forEach((el) => el.addEventListener("click", (e) => {
    e.preventDefault();
    $("#game").value = el.dataset.game;
    $("#game").dispatchEvent(new Event("change"));
  }));
  return null;
}

function gameTotals() {
  return state.compat.games.map((g, i) => {
    const t = { i, title: g.title, o: 0, r: 0, u: 0 };
    for (const codes of Object.values(state.compat.status)) t[codes[i]] += 1;
    return t;
  });
}

function showPackage(key) {
  const [author, name] = key.split("/");
  let pkg = q("SELECT * FROM package WHERE author = ? AND name = ?", [author, name])[0];
  if (!pkg) {
    const alias = q("SELECT p.* FROM package_alias a JOIN package p ON p.id = a.package_id WHERE a.alias = ?", [key])[0];
    if (!alias) throw new Error(`No package ${key}`);
    location.replace(`#/p/${alias.author}/${alias.name}`);
    return null;
  }
  const realKey = `${pkg.author}/${pkg.name}`;
  const provides = q("SELECT modname FROM provides WHERE package_id = ? ORDER BY modname", [pkg.id]).map((r) => r.modname);
  const deps = q(`SELECT d.modname, d.optional,
      (SELECT count(*) FROM provides pr JOIN package p ON p.id = pr.package_id WHERE pr.modname = d.modname AND p.type != 'game') AS mods,
      (SELECT count(*) FROM provides pr JOIN package p ON p.id = pr.package_id WHERE pr.modname = d.modname AND p.type = 'game') AS games
    FROM dep d WHERE d.package_id = ? ORDER BY d.optional, d.modname`, [pkg.id]);
  const usedBy = q(`SELECT DISTINCT p.author || '/' || p.name AS key, p.title, d.optional, d.modname FROM dep d
      JOIN package p ON p.id = d.package_id WHERE d.modname IN (SELECT modname FROM provides WHERE package_id = ?)
      AND p.id != ? ORDER BY d.optional, p.score DESC LIMIT 300`, [pkg.id, pkg.id]);
  const refs = q(`SELECT m.name AS mod, r.modname, r.kind, r.symbol, r.count, r.file, r.line, r.guarded
      FROM ref r JOIN mod m ON m.id = r.mod_id WHERE m.package_id = ? AND r.declared = 0 AND r.transitive = 0
      AND r.modname NOT IN (SELECT name FROM mod WHERE package_id = ?) ORDER BY r.guarded, r.kind, r.count DESC LIMIT 60`,
    [pkg.id, pkg.id]);
  const scanned = q("SELECT count(*) AS n FROM mod WHERE package_id = ?", [pkg.id])[0].n;
  const claims = q(`SELECT g.author || '/' || g.name AS game, g.title, s.supports, s.confidence FROM game_support s
      JOIN package g ON g.id = s.game_id WHERE s.package_id = ? ORDER BY s.confidence DESC, g.score DESC`, [pkg.id]);
  const codes = state.compat.status[realKey] || "";
  const history = hasTable("dep_history") ? q(`SELECT modname, optional, first_seen, last_seen
      FROM dep_history WHERE package = ? ORDER BY last_seen DESC, first_seen DESC, modname LIMIT 60`, [realKey]) : [];
  const historyStart = hasTable("dep_history") ? q("SELECT min(first_seen) AS d FROM dep_history")[0].d : null;
  const latest = state.meta.built_at.slice(0, 10);
  const changes = history.filter((r) => r.first_seen !== historyStart || r.last_seen !== latest);

  const compatRows = state.compat.games.map((g, i) => {
    const code = codes[i];
    const label = code ? STATUS_LABEL[code] : "—";
    return `<tr><td>${link(g.key, g.title)}</td><td><span class="badge ${h(label)}">${h(label)}</span></td></tr>`;
  }).join("");
  const depRows = deps.map((d) => {
    const best = d.mods ? bestProvider(d.modname) : null;
    const where = best ? link(best.key) : d.games ? `<span class="badge bad">only in ${d.games} game${d.games === 1 ? "" : "s"}</span>`
      : `<span class="badge bad">no provider</span>`;
    return `<tr><td>${modLink(d.modname)}</td><td>${d.optional ? "optional" : "<strong>hard</strong>"}</td>
      <td>${where}</td><td class="num">${d.mods}/${d.games}</td></tr>`;
  }).join("");

  panel.innerHTML = `
    <h2>${h(pkg.title || pkg.name)}</h2>
    <p class="sub"><code>${h(realKey)}</code> · ${h(pkg.type)} · ${Number(pkg.downloads || 0).toLocaleString()} downloads</p>
    <p>${h(pkg.short_description || "")}</p>
    <div class="badges">
      ${pkg.license ? `<span class="badge">code ${h(pkg.license)}</span>` : ""}
      ${pkg.media_license ? `<span class="badge">media ${h(pkg.media_license)}</span>` : ""}
      ${pkg.min_engine ? `<span class="badge">Luanti ≥ ${h(pkg.min_engine)}</span>` : ""}
      ${pkg.dev_state ? `<span class="badge">${h(pkg.dev_state.toLowerCase().replaceAll("_", " "))}</span>` : ""}
      ${pkg.supports_all_games ? `<span class="badge ok">declares all games</span>` : ""}
    </div>
    <p><a href="https://content.luanti.org/packages/${h(realKey)}/" target="_blank" rel="noopener">ContentDB</a>
      ${pkg.repo ? ` · <a href="${h(pkg.repo)}" target="_blank" rel="noopener">source</a>` : ""}</p>
    <div class="tabs" role="tablist">
      <button role="tab" aria-selected="true" data-tab="details">Details</button>
      ${pkg.type !== "game" ? `<button role="tab" aria-selected="false" data-tab="tree">Dependency tree</button>` : ""}
    </div>
    <div data-panel="details">
      ${provides.length ? `<h3>Provides (${provides.length})</h3><p>${provides.slice(0, 80).map(modLink).join(" ")}${provides.length > 80 ? " …" : ""}</p>` : ""}
      ${pkg.type === "mod" ? `<h3>Works in</h3><table>${compatRows}</table>
        <p class="note">ok: installable, no undeclared use of missing mods · risky: installable, but the code calls APIs of mods the game lacks without declaring them${scanned ? "" : " (sources not analyzed for this package)"} · computed like <code>ldg compat</code>.</p>` : ""}
      ${deps.length ? `<h3>Dependencies</h3><table><tr><th>mod name</th><th>kind</th><th>provided by</th><th class="num">mods/games</th></tr>${depRows}</table>` : "<h3>Dependencies</h3><p>None.</p>"}
      ${refs.length ? `<h3>Undeclared use of other mods</h3><table><tr><th>uses</th><th>how</th><th>where</th></tr>
        ${refs.map((r) => `<tr><td>${modLink(r.modname)}${r.guarded ? ' <span class="badge">guarded</span>' : ""}</td>
          <td><code>${h(r.symbol)}</code> ×${r.count}</td><td class="mono">${h(r.file)}:${r.line}</td></tr>`).join("")}</table>
        <p class="note">From static analysis (<code>ldg lint</code>); verify before reporting upstream.</p>` : ""}
      ${claims.length ? `<h3>ContentDB game support</h3><p>${claims.slice(0, 40).map((c) =>
        `<span class="badge ${c.supports ? "ok" : "bad"}" title="confidence ${c.confidence}">${h(c.title)}</span>`).join(" ")}</p>
        <p class="note">confidence 1 = detected by ContentDB, 10 = declared in mod.conf, 11 = set on the website.</p>` : ""}
      ${historyStart ? `<h3>Dependency changes since ${h(historyStart)}</h3>${changes.length ? `<table><tr><th>mod name</th><th>kind</th><th>from → to</th></tr>
        ${changes.map((r) => `<tr><td>${modLink(r.modname)}</td><td>${r.optional ? "optional" : "hard"}</td>
          <td>${r.first_seen === historyStart ? "before" : h(r.first_seen)} → ${r.last_seen === latest ? "now" : h(r.last_seen)}</td></tr>`).join("")}</table>`
        : `<p class="note">No changes recorded.</p>`}` : ""}
      <h3>Used by (${usedBy.length}${usedBy.length === 300 ? "+" : ""})</h3>
      <p>${usedBy.length ? usedBy.slice(0, 60).map((u) => link(u.key, u.title) + (u.optional ? "<sup>opt</sup>" : "")).join(", ") : "Nobody."}</p>
    </div>
    <div data-panel="tree" hidden>
      <p class="note">Hard dependencies, resolved through the best-scored standalone provider${state.game ? `; green = provided by ${h(state.compat.games[state.gameIndex].title)}` : "; pick a game above to see what it provides"}.</p>
      <div id="tree"></div>
    </div>`;
  panel.querySelectorAll(".tabs button").forEach((btn) => btn.addEventListener("click", () => {
    panel.querySelectorAll(".tabs button").forEach((b) => b.setAttribute("aria-selected", String(b === btn)));
    panel.querySelectorAll("[data-panel]").forEach((p) => { p.hidden = p.dataset.panel !== btn.dataset.tab; });
    if (btn.dataset.tab === "tree") drawTree(pkg);
  }));
  return realKey;
}

function drawTree(pkg) {
  const provided = state.game ? gameProvides(state.game) : new Set();
  const elements = [];
  const seen = new Set();
  const rootKey = `${pkg.author}/${pkg.name}`;
  elements.push({ data: { id: rootKey, label: pkg.name, kind: "root" } });
  seen.add(rootKey);
  const walk = (packageId, parent, depth) => {
    const own = new Set(q("SELECT modname FROM provides WHERE package_id = ?", [packageId]).map((r) => r.modname));
    const names = q("SELECT modname FROM dep WHERE package_id = ? AND optional = 0 ORDER BY modname", [packageId]).map((r) => r.modname);
    for (const name of names) {
      if (own.has(name)) continue;
      if (provided.has(name)) {
        const id = `game:${name}`;
        if (!seen.has(id)) { seen.add(id); elements.push({ data: { id, label: `${name} (game)`, kind: "game" } }); }
        elements.push({ data: { id: `${parent}->${id}`, source: parent, target: id } });
        continue;
      }
      const best = bestProvider(name);
      if (!best) {
        const games = q(`SELECT count(*) AS n FROM provides pr JOIN package p ON p.id = pr.package_id WHERE pr.modname = ? AND p.type = 'game'`, [name])[0].n;
        const id = `missing:${name}`;
        if (!seen.has(id)) { seen.add(id); elements.push({ data: { id, label: games ? `${name}: only in ${games} games` : `${name}: no provider`, kind: "missing" } }); }
        elements.push({ data: { id: `${parent}->${id}`, source: parent, target: id } });
        continue;
      }
      const isNew = !seen.has(best.key);
      if (isNew) { seen.add(best.key); elements.push({ data: { id: best.key, label: name === best.name ? name : `${name} ← ${best.name}`, kind: "mod" } }); }
      elements.push({ data: { id: `${parent}->${best.key}`, source: parent, target: best.key } });
      if (isNew && depth < 6) {
        const [a, n] = best.key.split("/");
        const row = q("SELECT id FROM package WHERE author = ? AND name = ?", [a, n])[0];
        if (row) walk(row.id, best.key, depth + 1);
      }
    }
  };
  walk(pkg.id, rootKey, 1);
  const unique = [...new Map(elements.map((e) => [e.data.id, e])).values()];
  const cy = cytoscape({
    container: $("#tree"),
    elements: unique,
    layout: { name: "dagre", rankDir: "LR", nodeSep: 12, rankSep: 50 },
    style: [
      { selector: "node", style: { label: "data(label)", "font-size": 10, color: css("--text"), "text-valign": "center",
        "text-halign": "right", "text-margin-x": 4, width: 10, height: 10, "background-color": css("--mod") } },
      { selector: "node[kind = 'root']", style: { "background-color": css("--accent"), width: 14, height: 14 } },
      { selector: "node[kind = 'game']", style: { "background-color": css("--ok") } },
      { selector: "node[kind = 'missing']", style: { "background-color": css("--bad") } },
      { selector: "edge", style: { width: 1, "line-color": css("--border"), "target-arrow-color": css("--border"),
        "target-arrow-shape": "triangle", "curve-style": "bezier" } },
    ],
  });
  cy.on("tap", "node", (e) => {
    const id = e.target.id();
    if (id.includes("/") && !id.includes(":")) location.hash = `#/p/${id}`;
    else if (id.includes(":")) location.hash = `#/m/${encodeURIComponent(id.split(":")[1])}`;
  });
}

function showModname(name) {
  const providers = q(`SELECT p.author || '/' || p.name AS key, p.title, p.type, p.score FROM provides pr
      JOIN package p ON p.id = pr.package_id WHERE pr.modname = ? ORDER BY p.type = 'game', p.name != ?, p.score DESC`, [name, name]);
  const dependers = q(`SELECT p.author || '/' || p.name AS key, p.title, d.optional FROM dep d JOIN package p ON p.id = d.package_id
      WHERE d.modname = ? ORDER BY d.optional, p.score DESC`, [name]);
  const refs = q(`SELECT count(DISTINCT m.package_id) AS n FROM ref r JOIN mod m ON m.id = r.mod_id
      WHERE r.modname = ? AND r.declared = 0 AND r.transitive = 0`, [name])[0].n;
  const mods = providers.filter((p) => p.type !== "game");
  const games = providers.filter((p) => p.type === "game");
  const hard = dependers.filter((d) => !d.optional);
  panel.innerHTML = `
    <h2><code>${h(name)}</code></h2>
    <p class="sub">mod name · ${providers.length} provider${providers.length === 1 ? "" : "s"} · ${hard.length} hard / ${dependers.length - hard.length} optional dependers</p>
    ${mods.length === 0 && games.length ? `<p><strong>Only games provide this mod name.</strong> A mod that hard-depends on
      <code>${h(name)}</code> runs only in these ${games.length} game${games.length === 1 ? "" : "s"}; it can't be installed elsewhere.</p>` : ""}
    ${mods.length === 0 && !games.length ? `<p><strong>No approved ContentDB package provides this name.</strong></p>` : ""}
    ${mods.length ? `<h3>Standalone providers (${mods.length})</h3><p>${mods.map((p, i) => link(p.key, p.title) + (i === 0 ? " <span class=\"badge ok\">preferred</span>" : "")).join(", ")}</p>` : ""}
    ${games.length ? `<h3>Games that bundle it (${games.length})</h3><p>${games.map((p) => link(p.key, p.title)).join(", ")}</p>` : ""}
    ${refs ? `<p class="note">Used without being declared by ${refs} analyzed package${refs === 1 ? "" : "s"}
      (<a href="#/sql" data-sql="undeclared:${h(name)}">show</a>).</p>` : ""}
    <h3>Hard dependers (${hard.length})</h3><p>${hard.slice(0, 120).map((d) => link(d.key, d.title)).join(", ") || "None."}${hard.length > 120 ? " …" : ""}</p>
    <h3>Optional dependers (${dependers.length - hard.length})</h3><p>${dependers.filter((d) => d.optional).slice(0, 80).map((d) => link(d.key, d.title)).join(", ") || "None."}</p>`;
  panel.querySelectorAll("[data-sql]").forEach((a) => a.addEventListener("click", () => {
    state.pendingSql = `SELECT package, mod, kind, symbol, count, file, line, guarded\nFROM v_undeclared_refs\nWHERE modname = '${name.replaceAll("'", "''")}'\nORDER BY guarded, count DESC;`;
  }));
  return state.graph.hasNode(`api:${name}`) ? `api:${name}` : mods[0]?.key ?? null;
}

const EXAMPLES = {
  "Undeclared API use": `SELECT package, mod, modname, symbol, count, file, line
FROM v_undeclared_refs
WHERE kind = 'global' AND guarded = 0 AND same_package = 0 AND requires = ''
ORDER BY count DESC LIMIT 50;`,
  "Game-only mod names": `SELECT d.modname, count(DISTINCT d.package_id) AS hard_dependers
FROM dep d
WHERE d.optional = 0 AND NOT EXISTS (
  SELECT 1 FROM provides pr JOIN package p ON p.id = pr.package_id
  WHERE pr.modname = d.modname AND p.type != 'game')
GROUP BY d.modname ORDER BY hard_dependers DESC LIMIT 30;`,
  "Most-used libraries": `SELECT p.author || '/' || p.name AS package, count(DISTINCT d.package_id) AS dependers
FROM dep d JOIN provides pr ON pr.modname = d.modname
JOIN package p ON p.id = pr.package_id AND p.type = 'mod' AND p.name = d.modname
WHERE d.optional = 0
GROUP BY p.id ORDER BY dependers DESC LIMIT 30;`,
  "Licenses": `SELECT license, count(*) AS n FROM package
WHERE type = 'mod' GROUP BY license ORDER BY n DESC LIMIT 20;`,
  "Transitive deps": `SELECT modname, depth FROM v_transitive_deps
WHERE root = 'mt-mods/technic_plus' ORDER BY depth, modname;`,
};

function showSql() {
  const initial = state.pendingSql || EXAMPLES["Undeclared API use"];
  state.pendingSql = null;
  const tables = q("SELECT name, type FROM sqlite_master WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' ORDER BY type, name");
  panel.innerHTML = `
    <h2>SQL</h2>
    <p class="sub">The whole database runs in your browser (sql.js). Tables: ${tables.map((t) => `<code>${h(t.name)}</code>`).join(" ")}</p>
    <div class="examples">${Object.keys(EXAMPLES).map((k) => `<button type="button" data-example="${h(k)}">${h(k)}</button>`).join("")}</div>
    <textarea id="sql" spellcheck="false" aria-label="SQL query">${h(initial)}</textarea>
    <p><button id="run" type="button">Run (Ctrl+Enter)</button> <span class="note" id="sql-info"></span></p>
    <div class="results" id="results"></div>`;
  const run = () => {
    const info = $("#sql-info");
    const out = $("#results");
    try {
      const started = performance.now();
      const res = state.db.exec($("#sql").value);
      const last = res[res.length - 1];
      if (!last) { out.innerHTML = ""; info.textContent = "No rows."; return; }
      const rows = last.values.slice(0, 500);
      info.textContent = `${last.values.length} row(s) in ${Math.round(performance.now() - started)} ms${last.values.length > 500 ? ", showing 500" : ""}`;
      out.innerHTML = `<table><tr>${last.columns.map((c) => `<th>${h(c)}</th>`).join("")}</tr>${rows.map((r) =>
        `<tr>${r.map((v) => `<td>${typeof v === "string" && /^[\w.-]+\/[\w.-]+$/.test(v) && state.graph.hasNode(v) ? link(v) : h(v)}</td>`).join("")}</tr>`).join("")}</table>`;
    } catch (err) {
      out.innerHTML = `<p class="error">${h(err.message)}</p>`;
      info.textContent = "";
    }
  };
  $("#run").addEventListener("click", run);
  $("#sql").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); run(); } });
  panel.querySelectorAll("[data-example]").forEach((b) => b.addEventListener("click", () => { $("#sql").value = EXAMPLES[b.dataset.example]; run(); }));
  run();
}

function showAbout() {
  const m = state.meta;
  panel.innerHTML = `
    <h2>About</h2>
    <p>This explorer shows the dependency graph of <a href="https://www.luanti.org">Luanti</a> (formerly Minetest) content
      on <a href="https://content.luanti.org">ContentDB</a>, combined with static analysis of package sources. It's built by
      <a href="https://github.com/culmat/luanti-depgraph">luanti-depgraph</a> (<code>ldg site</code>), part of
      <a href="https://github.com/culmat/avanti-luanti">avanti-luanti</a>.</p>
    <h3>How to read it</h3>
    <ul>
      <li>Arrows point from a package to what it depends on. If a standalone mod provides the mod name, the arrow goes
        to the best-scored one. If only games provide it, the arrow goes to a purple <em>API</em> node (like
        <a href="#/m/default">default</a>), which links to the games that bundle it.</li>
      <li>"Works in" uses the same rules as the engine and the main menu: game mods satisfy their names, other hard
        dependencies must be installable from ContentDB, and games never satisfy dependencies. Like ContentDB, the
        Minetest Game copies bundled by a few games are ignored, and providers' declared games are respected.</li>
      <li>"Risky" means installable, but the code uses mods missing in that game without declaring them. The source
        analysis covers the ${m.corpus_packages} best-scored packages.</li>
    </ul>
    <h3>Snapshot</h3>
    <dl class="kv">${Object.entries(m).map(([k, v]) => `<dt>${h(k)}</dt><dd>${h(v)}</dd>`).join("")}</dl>
    <p class="note">Download the database: <a href="data/luanti-deps.sqlite">luanti-deps.sqlite</a> ·
      CLI and MCP server: <code>uvx luanti-depgraph</code> · Developed with AI assistance (Claude), reviewed by a human.
      Content data © its respective authors.</p>`;
}

// ------------------------------------------------------------------ boot

$("#show-optional").addEventListener("change", (e) => { state.showOptional = e.target.checked; state.sigma?.refresh({ skipIndexation: true }); });
$("#relayout").addEventListener("click", () => layout(true));
$("#fit").addEventListener("click", () => state.sigma?.getCamera().animatedReset({ duration: 400 }));
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
  state.sigma?.setSetting("labelColor", { color: css("--text") });
  renderLegend();
  state.sigma?.refresh({ skipIndexation: true });
});
load().catch((err) => {
  statusEl.textContent = "";
  panel.innerHTML = `<p class="error">Failed to load data: ${h(err.message)}</p>`;
  console.error(err);
});

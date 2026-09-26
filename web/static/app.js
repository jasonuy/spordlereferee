const views = {
  favorites: document.getElementById("view-favorites"),
  schedule: document.getElementById("view-schedule"),
  refs: document.getElementById("view-refs"),
  standings: document.getElementById("view-standings"),
  team: document.getElementById("view-team"),
  leaders: document.getElementById("view-leaders"),
  player: document.getElementById("view-player"),
  search: document.getElementById("view-search"),
  game: document.getElementById("view-game"),
};

const pageTitle = document.getElementById("page-title");
const form = document.getElementById("filters");
const gamesEl = document.getElementById("games");
const statusEl = document.getElementById("status");
const pagerEl = document.getElementById("pager");
const pageSizeEl = document.getElementById("page-size");

const fields = {
  date_mode: form.elements.date_mode,
  day: form.elements.day,
  season_id: form.elements.season_id,
  office_id: form.elements.office_id,
  division: form.elements.division,
  gender: form.elements.gender,
  type: form.elements.type,
  schedule_id: form.elements.schedule_id,
  group_id: form.elements.group_id,
};

const standingsForm = document.getElementById("standings-filters");
const leadersForm = document.getElementById("leaders-filters");
const searchForm = document.getElementById("search-form");
const teamForm = document.getElementById("team-filters");
let currentTeamId = null;

let page = 1;
let pageSize = Number(pageSizeEl?.value) || 25;
let loading = false;
let filterData = null;
let standingsSchedulesCache = [];
let suppressStandingsEvents = false;
const sortState = new WeakMap();

const FAVORITES_KEY = "pcaha.favorites.v1";
const LEGACY_FOLLOWS_KEY = "pcaha.followedTeams.v1";

function normalizeFavorite(raw) {
  if (!raw || typeof raw !== "object") return null;
  if (raw.type === "player" || raw.participantId != null) {
    const participantId = Number(raw.participantId);
    if (!Number.isFinite(participantId)) return null;
    return {
      type: "player",
      participantId,
      seasonId: String(raw.seasonId || ""),
      scheduleId:
        raw.scheduleId != null && raw.scheduleId !== "" ? Number(raw.scheduleId) : null,
      teamId: raw.teamId != null && raw.teamId !== "" ? Number(raw.teamId) : null,
      name: raw.name || "",
    };
  }
  const teamId = Number(raw.teamId);
  if (!Number.isFinite(teamId)) return null;
  return {
    type: "team",
    teamId,
    seasonId: String(raw.seasonId || ""),
    scheduleId:
      raw.scheduleId != null && raw.scheduleId !== "" ? Number(raw.scheduleId) : null,
    name: raw.name || "",
    logoUrl: raw.logoUrl || null,
  };
}

function getFavorites() {
  try {
    const raw = JSON.parse(localStorage.getItem(FAVORITES_KEY) || "null");
    if (Array.isArray(raw)) {
      return raw.map(normalizeFavorite).filter(Boolean);
    }
  } catch {
    /* fall through to legacy */
  }
  try {
    const legacy = JSON.parse(localStorage.getItem(LEGACY_FOLLOWS_KEY) || "[]");
    if (!Array.isArray(legacy) || !legacy.length) return [];
    const migrated = legacy
      .map((f) => normalizeFavorite({ ...f, type: "team" }))
      .filter(Boolean);
    setFavorites(migrated);
    return migrated;
  } catch {
    return [];
  }
}

function setFavorites(list) {
  localStorage.setItem(FAVORITES_KEY, JSON.stringify(list));
}

function favoriteKey(entry) {
  const f = normalizeFavorite(entry);
  if (!f) return "";
  if (f.type === "player") {
    return `player|${f.seasonId}|${f.participantId}`;
  }
  return `team|${f.seasonId}|${f.scheduleId ?? ""}|${f.teamId}`;
}

function isFavorite(entry) {
  const key = favoriteKey(entry);
  return key ? getFavorites().some((f) => favoriteKey(f) === key) : false;
}

function toggleFavorite(entry) {
  const normalized = normalizeFavorite(entry);
  if (!normalized) return false;
  const list = getFavorites();
  const key = favoriteKey(normalized);
  const idx = list.findIndex((f) => favoriteKey(f) === key);
  if (idx >= 0) {
    list.splice(idx, 1);
    setFavorites(list);
    return false;
  }
  list.push(normalized);
  setFavorites(list);
  return true;
}

function favoriteButtonHtml(entry, { size = "" } = {}) {
  const f = normalizeFavorite(entry);
  if (!f) return "";
  const on = isFavorite(f);
  const label = f.name || (f.type === "player" ? "player" : "team");
  const verb = on ? "Remove from favorites" : "Add to favorites";
  if (f.type === "player") {
    return `<button
      type="button"
      class="follow-btn favorite-btn ${on ? "on" : ""} ${size}"
      data-fav-type="player"
      data-fav-participant="${escapeHtml(f.participantId)}"
      data-fav-season="${escapeHtml(f.seasonId)}"
      data-fav-schedule="${escapeHtml(f.scheduleId ?? "")}"
      data-fav-team="${escapeHtml(f.teamId ?? "")}"
      data-fav-name="${escapeHtml(f.name || "")}"
      aria-pressed="${on ? "true" : "false"}"
      aria-label="${verb}: ${escapeHtml(label)}"
      title="${verb}"
    >★</button>`;
  }
  return `<button
    type="button"
    class="follow-btn favorite-btn ${on ? "on" : ""} ${size}"
    data-fav-type="team"
    data-fav-team="${escapeHtml(f.teamId)}"
    data-fav-season="${escapeHtml(f.seasonId)}"
    data-fav-schedule="${escapeHtml(f.scheduleId ?? "")}"
    data-fav-name="${escapeHtml(f.name || "")}"
    data-fav-logo="${escapeHtml(f.logoUrl || "")}"
    aria-pressed="${on ? "true" : "false"}"
    aria-label="${verb}: ${escapeHtml(label)}"
    title="${verb}"
  >★</button>`;
}

/** @deprecated Use favoriteButtonHtml — kept as alias for call sites during rename. */
function followButtonHtml(opts) {
  return favoriteButtonHtml(
    {
      type: "team",
      teamId: opts.teamId,
      seasonId: opts.seasonId,
      scheduleId: opts.scheduleId,
      name: opts.name,
      logoUrl: opts.logoUrl,
    },
    { size: opts.size || "" },
  );
}

function fillSelect(select, items, { value, label, blank }) {
  if (!select) return;
  const current = select.value;
  select.innerHTML = "";
  if (blank) {
    select.append(new Option(blank, ""));
  }
  for (const item of items) {
    select.append(new Option(label(item), value(item)));
  }
  if ([...select.options].some((o) => o.value === current)) {
    select.value = current;
  }
}

async function getJson(url) {
  const resp = await fetch(url);
  if (!resp.ok) {
    throw new Error(await resp.text());
  }
  return resp.json();
}

function query(params) {
  const qs = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === "" || value == null) continue;
    if (Array.isArray(value)) {
      for (const item of value) {
        if (item !== "" && item != null) qs.append(key, item);
      }
    } else {
      qs.set(key, value);
    }
  }
  return qs.toString();
}

function selectedDivisionName() {
  const option = fields.division.selectedOptions[0];
  return option && option.value ? option.dataset.name || option.textContent : "";
}

function titleCase(name) {
  return String(name || "")
    .toLowerCase()
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function teamLogo(url, name, { size = "md" } = {}) {
  const initial = escapeHtml(((name || "?").trim().charAt(0) || "?").toUpperCase());
  if (!url) {
    return `<span class="team-logo team-logo-${size} team-logo-fallback" aria-hidden="true">${initial}</span>`;
  }
  return `<img class="team-logo team-logo-${size}" src="${escapeHtml(url)}" alt="" loading="lazy" decoding="async" onerror="this.outerHTML='<span class=\\'team-logo team-logo-${size} team-logo-fallback\\' aria-hidden=\\'true\\'>${initial}</span>'" />`;
}

function teamLinkLabel(name, url, href, { size = "md" } = {}) {
  const inner = `${teamLogo(url, name, { size })}<span class="team-name">${escapeHtml(name || "TBD")}</span>`;
  if (!href) return `<span class="team-with-logo">${inner}</span>`;
  return `<a class="team-with-logo" href="${href}">${inner}</a>`;
}

function parseHash() {
  const raw = (location.hash || "#/favorites").replace(/^#\/?/, "");
  const [path, qs] = raw.split("?");
  const parts = path.split("/").filter(Boolean);
  const params = Object.fromEntries(new URLSearchParams(qs || ""));
  return { parts, params };
}

function setHash(path, params = {}) {
  const qs = query(params);
  location.hash = `#/${path}${qs ? `?${qs}` : ""}`;
}

function showView(name, title) {
  for (const [key, el] of Object.entries(views)) {
    if (el) el.hidden = key !== name;
  }
  pageTitle.textContent = title;
  const standingsFamily = ["standings", "team", "leaders", "player", "search", "game"];
  let topTab = "schedule";
  if (name === "favorites") topTab = "favorites";
  else if (name === "refs") topTab = "refs";
  else if (standingsFamily.includes(name)) topTab = "standings";
  document.querySelectorAll(".main-nav a").forEach((a) => {
    a.classList.toggle("active", a.dataset.nav === topTab);
  });
  document.querySelectorAll(".sub-nav a").forEach((a) => {
    const sub = a.dataset.subnav;
    const active =
      (sub === "standings" && (name === "standings" || name === "team" || name === "game")) ||
      (sub === "leaders" && name === "leaders") ||
      (sub === "search" && (name === "search" || name === "player"));
    a.classList.toggle("active", active);
  });
}

function num(value) {
  if (value == null || value === "") return "—";
  if (typeof value === "number") {
    return Number.isInteger(value) ? String(value) : value.toFixed(2).replace(/\.00$/, "");
  }
  return String(value);
}

/** Natural sort for group labels: Tier 1/2/3, Flight 1…, and colour names A–Z. */
function compareGroupNames(a, b) {
  return String(a || "").localeCompare(String(b || ""), undefined, {
    numeric: true,
    sensitivity: "base",
  });
}

function sortableTable(container, columns, rows, { rowClass, onRowClick, primaryKey } = {}) {
  const state = sortState.get(container) || { key: columns[0]?.key, dir: "asc" };
  sortState.set(container, state);

  const sorted = [...rows].sort((a, b) => {
    if (primaryKey) {
      const pa = a[primaryKey];
      const pb = b[primaryKey];
      const primaryCmp =
        primaryKey === "groupName"
          ? compareGroupNames(pa, pb)
          : String(pa ?? "").localeCompare(String(pb ?? ""), undefined, {
              numeric: true,
              sensitivity: "base",
            });
      if (primaryCmp !== 0) return primaryCmp;
      if (state.key === primaryKey) return 0;
    }
    const av = a[state.key];
    const bv = b[state.key];
    if (av == null && bv == null) return 0;
    if (av == null) return 1;
    if (bv == null) return -1;
    if (typeof av === "number" && typeof bv === "number") {
      return state.dir === "asc" ? av - bv : bv - av;
    }
    return state.dir === "asc"
      ? String(av).localeCompare(String(bv), undefined, { numeric: true, sensitivity: "base" })
      : String(bv).localeCompare(String(av), undefined, { numeric: true, sensitivity: "base" });
  });

  const head = columns
    .map((c) => {
      const arrow = state.key === c.key && c.key !== "follow" && c.key !== "favorite" ? (state.dir === "asc" ? " ↑" : " ↓") : "";
      const cls = [c.numeric ? "num" : "", c.key === "follow" || c.key === "favorite" ? "follow-cell" : ""].filter(Boolean).join(" ");
      const title = c.title ? ` title="${escapeHtml(c.title)}"` : "";
      return `<th data-key="${escapeHtml(c.key)}" class="${cls}"${title}>${escapeHtml(c.label)}${arrow}</th>`;
    })
    .join("");

  const body = sorted
    .map((row, idx) => {
      const cells = columns
        .map((c) => {
          const raw = c.render ? c.render(row) : escapeHtml(num(row[c.key]));
          const cls = [c.numeric ? "num" : "", c.key === "follow" || c.key === "favorite" ? "follow-cell" : ""].filter(Boolean).join(" ");
          return `<td class="${cls}">${raw}</td>`;
        })
        .join("");
      const cls = rowClass ? rowClass(row, idx) : "";
      const clickable = onRowClick ? "clickable" : "";
      return `<tr class="${cls} ${clickable}" data-idx="${idx}">${cells}</tr>`;
    })
    .join("");

  // Always render a real table (horizontally scrollable on narrow screens).
  // Stacked mobile cards were too tall for multi-column standings/stats.
  container.innerHTML = `
    <div class="data-table">
      <table>
        <thead><tr>${head}</tr></thead>
        <tbody>${body || `<tr><td colspan="${columns.length}">No rows</td></tr>`}</tbody>
      </table>
    </div>
  `;

  container.querySelectorAll("th[data-key]").forEach((th) => {
    th.addEventListener("click", () => {
      const key = th.dataset.key;
      if (!key || key === "follow" || key === "favorite") return;
      if (state.key === key) state.dir = state.dir === "asc" ? "desc" : "asc";
      else {
        state.key = key;
        state.dir = columns.find((c) => c.key === key)?.numeric ? "desc" : "asc";
      }
      sortableTable(container, columns, rows, { rowClass, onRowClick, primaryKey });
    });
  });

  if (onRowClick) {
    container.querySelectorAll("[data-idx]").forEach((el) => {
      el.addEventListener("click", () => onRowClick(sorted[Number(el.dataset.idx)]));
    });
  }
}

async function loadFilters() {
  filterData = await getJson("/api/filters");
  const seasons = filterData.seasons;

  fields.day.value = filterData.defaultDate;
  fillSelect(fields.season_id, seasons, { value: (s) => s, label: (s) => s });
  fields.season_id.value = filterData.defaultSeason;
  fillSelect(fields.office_id, filterData.offices, {
    blank: "All associations",
    value: (o) => o.id,
    label: (o) => o.name,
  });
  fillSelect(fields.division, filterData.divisions, {
    blank: "All divisions",
    value: (d) => d.id,
    label: (d) => d.name,
  });
  for (const d of filterData.divisions) {
    const opt = [...fields.division.options].find((o) => o.value === String(d.id));
    if (opt) opt.dataset.name = d.name;
  }
  fillSelect(fields.gender, filterData.genders, {
    blank: "All genders",
    value: (g) => g,
    label: (g) => g,
  });
  fillSelect(fields.type, filterData.scheduleTypes, {
    blank: "All types",
    value: (t) => t,
    label: (t) => t,
  });

  // Search can browse any Spordle season; standings/leaders seasons come from SQLite.
  fillSelect(searchForm.elements.season_id, seasons, { value: (s) => s, label: (s) => s });
  searchForm.elements.season_id.value = filterData.defaultSeason;
}

async function loadSchedules() {
  const params = query({
    season_id: fields.season_id.value,
    office_id: fields.office_id.value,
    division_id: fields.division.value,
    gender: fields.gender.value,
    type: fields.type.value,
  });
  const rows = await getJson(`/api/schedules?${params}`);
  fillSelect(fields.schedule_id, rows, {
    blank: "All schedules",
    value: (s) => s.id,
    label: (s) => `${s.type ? `${s.type} · ` : ""}${s.name}`,
  });
  for (const s of rows) {
    const opt = [...fields.schedule_id.options].find((o) => o.value === String(s.id));
    if (opt && s.officeId) opt.dataset.officeId = s.officeId;
  }
  return rows;
}

async function loadGroups() {
  const officeId = fields.office_id.value || fields.schedule_id.selectedOptions[0]?.dataset.officeId;
  const rows = officeId
    ? await getJson(`/api/groups?${query({ office_id: officeId, type: fields.type.value })}`)
    : [];
  fillSelect(fields.group_id, rows, {
    blank: "All groups",
    value: (g) => g.id,
    label: (g) => g.name,
  });
}

function officialChips(officials) {
  if (!officials.length) {
    return `<span class="chip none">No officials assigned</span>`;
  }
  return officials
    .map(
      (o) =>
        `<span class="chip"><span class="pos">${escapeHtml(o.position)}</span> ${escapeHtml(titleCase(o.name))}${
          o.status ? ` · ${escapeHtml(o.status)}` : ""
        }</span>`,
    )
    .join("");
}

function gameField(game, snake, camel) {
  return game[snake] ?? game[camel];
}

/** Stacked away/home scoreboard — clearer than "2 – 0". */
function stackedScoreboard(game, { highlightTeamId = null, showNames = true, seasonId = null } = {}) {
  const awayName = gameField(game, "away_name", "away") || "Away";
  const homeName = gameField(game, "home_name", "home") || "Home";
  const awayScore = gameField(game, "away_score", "awayScore");
  const homeScore = gameField(game, "home_score", "homeScore");
  const awayId = gameField(game, "away_team_id", "awayTeamId");
  const homeId = gameField(game, "home_team_id", "homeTeamId");
  const awayLogo = gameField(game, "awayLogoUrl", "away_logo_url");
  const homeLogo = gameField(game, "homeLogoUrl", "home_logo_url");
  const season =
    seasonId ||
    gameField(game, "season_id", "seasonId") ||
    filterData?.defaultSeason ||
    "2026-27";
  const hasScore = awayScore != null && homeScore != null;
  const hid = highlightTeamId != null ? Number(highlightTeamId) : null;

  const row = (name, logo, side, score, teamId) => {
    const self = hid != null && Number(teamId) === hid;
    const href =
      teamId != null
        ? `#/team/${teamId}?season_id=${encodeURIComponent(season)}`
        : null;
    const label = showNames
      ? teamLinkLabel(name, logo, href, { size: "sm" })
      : `<span class="team-with-logo">${teamLogo(logo, name, { size: "sm" })}</span>`;
    return `
      <div class="scoreboard-row${self ? " is-self" : ""}">
        <div class="scoreboard-team">${label}</div>
        <span class="scoreboard-ha">${side}</span>
        <span class="scoreboard-pts">${hasScore ? escapeHtml(String(score)) : "—"}</span>
      </div>
    `;
  };

  return `
    <div class="scoreboard${showNames ? "" : " scoreboard-compact"}" aria-label="Score">
      ${row(awayName, awayLogo, "away", awayScore, awayId)}
      ${row(homeName, homeLogo, "home", homeScore, homeId)}
    </div>
  `;
}

function scoreline(game) {
  if (game.homeScore == null || game.awayScore == null) return "";
  return stackedScoreboard(game, { showNames: false });
}

function pageNumberItems(current, totalPages) {
  const cur = Math.max(1, Math.min(current, totalPages));
  if (totalPages <= 7) {
    return Array.from({ length: totalPages }, (_, i) => i + 1);
  }
  const keep = new Set([1, totalPages, cur]);
  for (let n = cur - 1; n <= cur + 1; n += 1) {
    if (n >= 1 && n <= totalPages) keep.add(n);
  }
  if (cur <= 3) [2, 3, 4, 5].forEach((n) => keep.add(n));
  if (cur >= totalPages - 2) {
    [totalPages - 4, totalPages - 3, totalPages - 2, totalPages - 1].forEach((n) => {
      if (n >= 1) keep.add(n);
    });
  }
  const nums = [...keep].sort((a, b) => a - b);
  const items = [];
  for (let i = 0; i < nums.length; i += 1) {
    if (i > 0 && nums[i] - nums[i - 1] > 1) items.push("…");
    items.push(nums[i]);
  }
  return items;
}

function renderPager(totalPages, currentPage) {
  const pages = Math.max(0, Number(totalPages) || 0);
  const cur = Math.max(1, Math.min(Number(currentPage) || 1, Math.max(pages, 1)));

  if (pages < 1) {
    pagerEl.hidden = false;
    pagerEl.innerHTML = `
      <button type="button" disabled>Prev</button>
      <button type="button" class="current" disabled>1</button>
      <button type="button" disabled>Next</button>
    `;
    return;
  }

  if (pages === 1) {
    pagerEl.hidden = true;
    pagerEl.innerHTML = "";
    return;
  }

  const numbers = pageNumberItems(cur, pages)
    .map((item) => {
      if (item === "…") {
        return `<span class="pager-ellipsis" aria-hidden="true">…</span>`;
      }
      const active = item === cur;
      return `<button type="button" data-page="${item}" class="${
        active ? "current" : ""
      }" ${active ? "disabled aria-current=\"page\"" : ""}>${item}</button>`;
    })
    .join("");

  pagerEl.hidden = false;
  pagerEl.innerHTML = `
    <button type="button" data-page="${cur - 1}" ${cur <= 1 ? "disabled" : ""}>Prev</button>
    ${numbers}
    <button type="button" data-page="${cur + 1}" ${cur >= pages ? "disabled" : ""}>Next</button>
  `;
}

function renderGames(payload) {
  const total = Number(payload.total) || 0;
  const pages = total > 0 ? Number(payload.pages) || 1 : 0;
  if (page > pages && pages > 0) page = pages;

  if (!payload.games.length) {
    gamesEl.innerHTML = "";
    statusEl.textContent = "No games match these filters.";
    renderPager(0, 1);
    return;
  }
  statusEl.textContent = `${total} game${total === 1 ? "" : "s"}${
    payload.fromDay ? ` from ${payload.fromDay}` : ""
  }`;
  gamesEl.innerHTML = payload.games
    .map((game) => {
      const when = [game.date, game.startTime && game.endTime ? `${game.startTime} – ${game.endTime}` : game.startTime]
        .filter(Boolean)
        .join(" · ");
      const league = [game.scheduleType, game.schedule, game.group].filter(Boolean).join(" · ");
      return `
        <article class="game">
          <div>
            <div class="meta">${escapeHtml(game.number)} · ${escapeHtml(when)}${league ? ` · ${escapeHtml(league)}` : ""}</div>
            <div class="title matchup">
              ${teamLinkLabel(
                game.away,
                game.awayLogoUrl,
                `#/team/${game.awayTeamId}?season_id=${encodeURIComponent(fields.season_id.value)}`,
              )}
              <span class="at">@</span>
              ${teamLinkLabel(
                game.home,
                game.homeLogoUrl,
                `#/team/${game.homeTeamId}?season_id=${encodeURIComponent(fields.season_id.value)}`,
              )}
            </div>
            <div class="venue">${escapeHtml([game.venue, game.city].filter(Boolean).join(" · "))}</div>
          </div>
          <div class="side">
            ${scoreline(game)}
            <a class="sheet" href="#/game/${game.id}">Recap</a>
            <a class="sheet ghost" href="${escapeHtml(game.scoresheetUrl)}" target="_blank" rel="noreferrer">PDF</a>
          </div>
          <div class="officials">${officialChips(game.officials)}</div>
        </article>
      `;
    })
    .join("");

  renderPager(pages, page);
}

async function loadGames() {
  if (loading) return;
  if (!fields.day.value) {
    fields.day.value = todayIso();
  }
  loading = true;
  const upcoming = fields.date_mode?.value === "upcoming";
  statusEl.textContent = upcoming ? "Loading upcoming games…" : "Loading games…";
  try {
    const params = query({
      ...(upcoming ? { from_day: fields.day.value } : { day: fields.day.value }),
      season_id: fields.season_id.value,
      office_id: fields.office_id.value,
      division: selectedDivisionName(),
      gender: fields.gender.value,
      schedule_id: fields.schedule_id.value,
      group_id: fields.group_id.value,
      page,
      page_size: pageSize,
    });
    renderGames(await getJson(`/api/games?${params}`));
  } catch (err) {
    statusEl.textContent = `Could not load games: ${err.message}`;
    gamesEl.innerHTML = "";
    renderPager(0, 1);
  } finally {
    loading = false;
  }
}

function standingsTypeSelect(form) {
  return form.querySelector('select[name="type"]');
}

function standingsGenderLabel(value) {
  return value;
}

function scheduleLabel(s) {
  const cat = s.category ? String(s.category) : "";
  const bits = [
    s.division,
    s.gender ? standingsGenderLabel(s.gender) : null,
    s.type,
    cat && cat !== s.name ? cat : null,
    s.name,
  ].filter(Boolean);
  return bits.join(" · ");
}

function preferredScheduleId(schedules) {
  if (!schedules.length) return "";
  const scored = schedules.map((s) => {
    const name = String(s.name || "").toLowerCase();
    let score = (s.groups || []).length;
    if (/u18a/.test(name) && /pre-?season/.test(name)) score += 100;
    else if (/u18/.test(name) && /pre-?season/.test(name)) score += 80;
    else if (/pre-?season/.test(name)) score += 20;
    if (s.division === "U18") score += 10;
    return { id: s.id, score };
  });
  scored.sort((a, b) => b.score - a.score);
  return String(scored[0].id);
}

function renderStandingsScheduleList() {
  const list = document.getElementById("standings-schedule-list");
  const picker = document.getElementById("standings-league-picker");
  if (!list) return;
  const selected = standingsForm.elements.schedule_id.value;
  if (!standingsSchedulesCache.length) {
    list.innerHTML = "";
    if (picker) picker.hidden = true;
    return;
  }
  if (picker) picker.hidden = false;
  list.innerHTML = standingsSchedulesCache
    .map((s) => {
      const active = String(s.id) === String(selected) ? " active" : "";
      return `<button type="button" data-schedule-id="${escapeHtml(s.id)}" class="${active.trim()}">${escapeHtml(scheduleLabel(s))}</button>`;
    })
    .join("");
}

function standingsHashParams() {
  return {
    season_id: standingsForm.elements.season_id.value,
    gender: standingsForm.elements.gender?.value || "",
    division: standingsForm.elements.division.value,
    type: standingsTypeSelect(standingsForm)?.value || "",
    schedule_id: standingsForm.elements.schedule_id.value,
    group_id: standingsForm.elements.group_id.value,
  };
}

async function loadStandingsSchedules({ preserveSchedule = true, autoSelect = true } = {}) {
  const seasonEl = standingsForm.elements.season_id;
  const genderEl = standingsForm.elements.gender;
  const divisionEl = standingsForm.elements.division;
  const typeEl = standingsTypeSelect(standingsForm);
  let season = seasonEl?.value || "";
  const gender = genderEl?.value || "";
  const division = divisionEl?.value || "";
  const type = typeEl?.value || "";
  const prevSchedule = preserveSchedule ? standingsForm.elements.schedule_id.value : "";
  const prevGroup = preserveSchedule ? standingsForm.elements.group_id.value : "";

  // Resolve season against SQLite first so we never recurse on a Spordle-only year.
  let data = await getJson(
    `/api/standings/schedules?${query({
      season_id: season || undefined,
      gender,
      division,
      type,
    })}`,
  );
  const seasons = data.seasons || [];
  const resolved =
    (season && seasons.includes(season) && season) ||
    data.defaultSeason ||
    seasons[0] ||
    "2026-27";
  if (resolved !== (data.seasonId || "") && !gender && !division && !type) {
    data = await getJson(
      `/api/standings/schedules?${query({
        season_id: resolved,
        gender,
        division,
        type,
      })}`,
    );
  }

  suppressStandingsEvents = true;
  try {
    fillSelect(seasonEl, data.seasons || seasons, { value: (s) => s, label: (s) => s });
    if (seasonEl) seasonEl.value = resolved;

    const genders = data.genders || [];
    fillSelect(genderEl, genders, {
      blank: "All genders",
      value: (g) => g,
      label: (g) => standingsGenderLabel(g),
    });
    if (gender && genders.includes(gender) && genderEl) {
      genderEl.value = gender;
    }

    fillSelect(divisionEl, data.divisions || [], {
      blank: "All ages",
      value: (d) => d,
      label: (d) => d,
    });
    if (division && (data.divisions || []).includes(division) && divisionEl) {
      divisionEl.value = division;
    }
    fillSelect(typeEl, data.types || [], {
      blank: "All types",
      value: (t) => t,
      label: (t) => t,
    });
    if (type && (data.types || []).includes(type) && typeEl) {
      typeEl.value = type;
    }

    standingsSchedulesCache = data.schedules || [];
    fillSelect(standingsForm.elements.schedule_id, standingsSchedulesCache, {
      blank: standingsSchedulesCache.length ? "Select a schedule" : "No schedules for this season",
      value: (s) => String(s.id),
      label: (s) => scheduleLabel(s),
    });
    const want =
      (prevSchedule &&
        [...standingsForm.elements.schedule_id.options].some((o) => o.value === String(prevSchedule)) &&
        String(prevSchedule)) ||
      (autoSelect ? preferredScheduleId(standingsSchedulesCache) : "");
    standingsForm.elements.schedule_id.value = want;
    updateStandingsGroups();
    if (prevGroup && [...standingsForm.elements.group_id.options].some((o) => o.value === String(prevGroup))) {
      standingsForm.elements.group_id.value = prevGroup;
    }
    renderStandingsScheduleList();
  } finally {
    suppressStandingsEvents = false;
  }
}

function updateStandingsGroups() {
  const sid = Number(standingsForm.elements.schedule_id.value);
  const sched = standingsSchedulesCache.find((s) => s.id === sid);
  const groups = [...(sched?.groups || [])].sort((a, b) => compareGroupNames(a.name, b.name));
  let blank = "All groups";
  if (!sid) blank = "Select a schedule first";
  else if (!groups.length) blank = "No groups";
  fillSelect(standingsForm.elements.group_id, groups, {
    blank,
    value: (g) => g.id,
    label: (g) => g.name,
  });
}

async function loadStandings() {
  const status = document.getElementById("standings-status");
  const table = document.getElementById("standings-table");
  const scheduleId = standingsForm.elements.schedule_id.value;
  if (!scheduleId) {
    const n = standingsSchedulesCache.length;
    status.textContent = n
      ? `Pick a schedule below to load standings (${n} available).`
      : "No standings for this season yet. Run ingest or pick another season.";
    table.innerHTML = "";
    renderStandingsScheduleList();
    return;
  }
  status.textContent = "Loading standings…";
  try {
    const params = query({
      season_id: standingsForm.elements.season_id.value,
      schedule_id: scheduleId,
      group_id: standingsForm.elements.group_id.value,
    });
    const data = await getJson(`/api/standings?${params}`);
    if (!data.standings.length) {
      status.textContent = "No standings for this league yet. Run ingest or pick another schedule.";
      table.innerHTML = "";
      return;
    }
    status.textContent = `${data.schedule?.name || "Standings"} · ${data.standings.length} teams`;
    const season = standingsForm.elements.season_id.value;
    const allGroups = !standingsForm.elements.group_id.value;
    const groupNames = new Set(
      data.standings.map((r) => r.groupName).filter((n) => n != null && String(n).trim() !== ""),
    );
    const showGroupCol = allGroups && groupNames.size > 1;
    const cols = [
      {
        key: "follow",
        label: "",
        render: (r) =>
          followButtonHtml({
            teamId: r.team_id,
            seasonId: season,
            scheduleId: r.schedule_id,
            name: r.team_name,
            logoUrl: r.logoUrl,
          }),
      },
      {
        key: "team_name",
        label: "Team",
        render: (r) =>
          teamLinkLabel(
            r.team_name,
            r.logoUrl,
            `#/team/${r.team_id}?season_id=${encodeURIComponent(season)}&schedule_id=${r.schedule_id}`,
          ),
      },
      ...(showGroupCol
        ? [{ key: "groupName", label: "Group", render: (r) => escapeHtml(r.groupName || "—") }]
        : []),
      { key: "gp", label: "GP", numeric: true },
      { key: "w", label: "W", numeric: true },
      { key: "l", label: "L", numeric: true },
      { key: "t", label: "T", numeric: true },
      {
        key: "sportsmanship",
        label: "SP",
        numeric: true,
        title: "Sportsmanship points",
      },
      { key: "pts", label: "PTS", numeric: true },
      { key: "gf", label: "GF", numeric: true },
      { key: "ga", label: "GA", numeric: true },
      { key: "gd", label: "GD", numeric: true },
      { key: "pim", label: "PIM", numeric: true },
    ];
    const state = sortState.get(table) || { key: "pts", dir: "desc" };
    state.key = "pts";
    state.dir = "desc";
    sortState.set(table, state);
    sortableTable(table, cols, data.standings, {
      primaryKey: showGroupCol ? "groupName" : undefined,
    });
  } catch (err) {
    status.textContent = `Could not load standings: ${err.message}`;
    table.innerHTML = "";
  }
}

async function loadTeam(teamId, params) {
  showView("team", "Team");
  currentTeamId = teamId;
  const status = document.getElementById("team-status");
  status.textContent = "Loading team…";
  try {
    const season = params.season_id || filterData?.defaultSeason || "2026-27";
    const wantAll = params.scope === "all";
    const qs = query({
      season_id: season,
      schedule_id: wantAll ? "" : params.schedule_id,
      type: wantAll ? "" : params.type,
      scope: wantAll ? "all" : "",
    });
    const data = await getJson(`/api/teams/${teamId}/roster?${qs}`);
    const team = data.team;
    pageTitle.textContent = team.name || `Team ${teamId}`;
    status.textContent = "";

    // Sync URL to the resolved default (League / incoming schedule) once, so
    // dropdowns reflect what's shown; user can still switch to All to compare.
    if (
      !wantAll &&
      !params.schedule_id &&
      !params.type &&
      data.scheduleId != null
    ) {
      setHash(`team/${teamId}`, {
        season_id: season,
        schedule_id: data.scheduleId,
        type: data.type || undefined,
      });
      return;
    }

    const schedules = data.schedules || [];
    const types = data.types || [];
    const selectedType = wantAll ? "" : params.type || data.type || "";
    const selectedScheduleId = wantAll
      ? ""
      : params.schedule_id != null && params.schedule_id !== ""
        ? String(params.schedule_id)
        : data.scheduleId != null
          ? String(data.scheduleId)
          : "";

    fillSelect(teamForm.elements.type, types, {
      blank: "All types",
      value: (t) => t,
      label: (t) => t,
    });
    teamForm.elements.type.value =
      selectedType && types.includes(selectedType) ? selectedType : "";

    const typeFilter = teamForm.elements.type.value;
    const scheduleOptions = typeFilter
      ? schedules.filter((s) => s.type === typeFilter)
      : schedules;
    fillSelect(teamForm.elements.schedule_id, scheduleOptions, {
      blank: typeFilter ? `All ${typeFilter} schedules` : "All schedules",
      value: (s) => s.id,
      label: (s) => `${s.type || "Schedule"} · ${s.name}`,
    });
    if (
      selectedScheduleId &&
      [...teamForm.elements.schedule_id.options].some((o) => o.value === selectedScheduleId)
    ) {
      teamForm.elements.schedule_id.value = selectedScheduleId;
    } else {
      teamForm.elements.schedule_id.value = "";
    }

    const st = (data.standings || [])[0];
    const scheduleId = wantAll ? "" : data.scheduleId ?? params.schedule_id ?? "";
    const scheduleLabel = wantAll
      ? "All schedules"
      : data.schedule
        ? `${data.schedule.type || ""} · ${data.schedule.name}`.replace(/^\s·\s/, "").trim()
        : selectedType
          ? `All ${selectedType}`
          : "All schedules";
    const record = st
      ? `${st.gp} GP · ${st.pts} PTS · ${st.w}-${st.l}-${st.t}${st.sportsmanship != null ? ` · ${st.sportsmanship} SP` : ""}`
      : "";
    document.getElementById("team-header").innerHTML = `
      <p class="meta">
        <a href="#/standings">← Standings</a>
        · ${escapeHtml(scheduleLabel)}
        ${record ? ` · ${escapeHtml(record)}` : ""}
      </p>
      <div class="team-heading-row">
        ${followButtonHtml({
          teamId,
          seasonId: data.seasonId || season,
          scheduleId,
          name: team.name,
          logoUrl: team.logo_url || team.logoUrl,
          size: "lg",
        })}
        <h2 class="team-heading">${teamLogo(team.logo_url || team.logoUrl, team.name, { size: "lg" })}<span>${escapeHtml(team.name || "")}</span></h2>
      </div>
    `;

    const skatersEl = document.getElementById("team-skaters");
    const skaterCols = [
      {
        key: "favorite",
        label: "",
        render: (r) =>
          favoriteButtonHtml({
            type: "player",
            participantId: r.participant_id,
            seasonId: data.seasonId || season,
            scheduleId: r.schedule_id || scheduleId,
            teamId,
            name: r.player_name,
          }),
      },
      { key: "number", label: "#", render: (r) => escapeHtml(r.number_display || r.number || "—") },
      {
        key: "player_name",
        label: "Name",
        render: (r) =>
          `<a href="#/player/${r.participant_id}?season_id=${encodeURIComponent(data.seasonId || season)}&schedule_id=${r.schedule_id || scheduleId}">${escapeHtml(titleCase(r.player_name))}</a>${
            r.is_affiliate ? ' <span class="tag">AP</span>' : ""
          }`,
      },
      { key: "positions", label: "Pos" },
      { key: "gp", label: "GP", numeric: true },
      { key: "g", label: "G", numeric: true },
      { key: "a", label: "A", numeric: true },
      { key: "p", label: "P", numeric: true },
      { key: "pim", label: "PIM", numeric: true },
      { key: "ppg", label: "PPG", numeric: true },
      { key: "shg", label: "SHG", numeric: true },
      { key: "gwg", label: "GWG", numeric: true },
    ];
    sortState.set(skatersEl, { key: "p", dir: "desc" });

    const goaliesEl = document.getElementById("team-goalies");
    const goalieCols = [
      {
        key: "favorite",
        label: "",
        render: (r) =>
          favoriteButtonHtml({
            type: "player",
            participantId: r.participant_id,
            seasonId: data.seasonId || season,
            scheduleId: r.schedule_id || scheduleId,
            teamId,
            name: r.player_name,
          }),
      },
      { key: "number", label: "#", render: (r) => escapeHtml(r.number_display || r.number || "—") },
      {
        key: "player_name",
        label: "Name",
        render: (r) =>
          `<a href="#/player/${r.participant_id}?season_id=${encodeURIComponent(data.seasonId || season)}">${escapeHtml(titleCase(r.player_name))}</a>`,
      },
      { key: "goalie_gp", label: "GP", numeric: true },
      { key: "goalie_w", label: "W", numeric: true },
      { key: "goalie_l", label: "L", numeric: true },
      { key: "goalie_t", label: "T", numeric: true },
      { key: "ga", label: "GA", numeric: true },
      { key: "gaa", label: "GAA", numeric: true },
    ];
    sortState.set(goaliesEl, { key: "goalie_w", dir: "desc" });

    if (!(data.skaters || []).length && !(data.goalies || []).length) {
      const note =
        (data.games || []).length > 0
          ? `<p class="muted">Roster stats aren’t loaded for this season yet — standings/games are in, player sheets are still downloading.</p>`
          : `<p class="muted">No roster data yet.</p>`;
      skatersEl.innerHTML = note;
      goaliesEl.innerHTML = "";
    } else {
      sortableTable(skatersEl, skaterCols, data.skaters);
      sortableTable(goaliesEl, goalieCols, data.goalies);
    }

    document.getElementById("team-games").innerHTML = data.games
      .map((g) => renderCompactGameCard(g, { highlightTeamId: teamId, seasonId: data.seasonId || season }))
      .join("") || `<p class="muted">No approved games yet.</p>`;

    await loadTeamFutureGames(teamId, {
      seasonId: data.seasonId || season,
      scheduleId: wantAll ? "" : data.scheduleId || params.schedule_id || "",
      teamName: team.name,
    });
  } catch (err) {
    status.textContent = `Could not load team: ${err.message}`;
  }
}

async function loadLeadersSchedules() {
  const seasonEl = leadersForm.elements.season_id;
  let season = seasonEl.value;
  const division = leadersForm.elements.division?.value || "";
  const data = await getJson(
    `/api/standings/schedules?${query({ season_id: season || undefined, division })}`,
  );
  const seasons = data.seasons || [];
  fillSelect(seasonEl, seasons, { value: (s) => s, label: (s) => s });
  if (season && seasons.includes(season)) {
    seasonEl.value = season;
  } else if (data.defaultSeason && seasons.includes(data.defaultSeason)) {
    seasonEl.value = data.defaultSeason;
    season = data.defaultSeason;
  } else if (seasons.length) {
    seasonEl.value = seasons[0];
    season = seasons[0];
  }
  if (season && season !== (data.seasonId || "") && !division) {
    return loadLeadersSchedules();
  }
  fillSelect(leadersForm.elements.division, data.divisions || [], {
    blank: "All ages",
    value: (d) => d,
    label: (d) => d,
  });
  if (division) leadersForm.elements.division.value = division;
  fillSelect(leadersForm.elements.schedule_id, data.schedules || [], {
    blank: "All schedules with stats",
    value: (s) => s.id,
    label: (s) => `${s.division || ""} ${s.type ? `· ${s.type}` : ""} · ${s.name}`.trim(),
  });
}

async function loadLeaders() {
  const status = document.getElementById("leaders-status");
  const root = document.getElementById("leaders");
  status.textContent = "Loading leaders…";
  try {
    const params = query({
      season_id: leadersForm.elements.season_id.value,
      schedule_id: leadersForm.elements.schedule_id.value,
      limit: 15,
    });
    const data = await getJson(`/api/leaders?${params}`);
    status.textContent = "";
    const season = leadersForm.elements.season_id.value;

    function board(title, rows, cols) {
      const wrap = document.createElement("div");
      wrap.className = "leader-board";
      wrap.innerHTML = `<h2 class="section-title">${escapeHtml(title)}</h2><div class="table-wrap"></div>`;
      sortableTable(wrap.querySelector(".table-wrap"), cols, rows);
      return wrap;
    }

    const favCol = {
      key: "favorite",
      label: "",
      render: (r) =>
        favoriteButtonHtml({
          type: "player",
          participantId: r.participant_id,
          seasonId: season,
          scheduleId: r.schedule_id,
          teamId: r.team_id,
          name: r.player_name,
        }),
    };
    const nameCol = {
      key: "player_name",
      label: "Player",
      render: (r) =>
        `<a href="#/player/${r.participant_id}?season_id=${encodeURIComponent(season)}&schedule_id=${r.schedule_id}">${escapeHtml(titleCase(r.player_name))}</a>`,
    };
    const teamCol = {
      key: "team_name",
      label: "Team",
      render: (r) =>
        r.team_id
          ? teamLinkLabel(
              r.team_name,
              r.logoUrl,
              `#/team/${r.team_id}?season_id=${encodeURIComponent(season)}&schedule_id=${r.schedule_id}`,
              { size: "sm" },
            )
          : "",
    };

    root.innerHTML = "";
    root.append(
      board("Points", data.points, [favCol, nameCol, teamCol, { key: "gp", label: "GP", numeric: true }, { key: "g", label: "G", numeric: true }, { key: "a", label: "A", numeric: true }, { key: "p", label: "P", numeric: true }]),
      board("Goals", data.goals, [favCol, nameCol, teamCol, { key: "g", label: "G", numeric: true }, { key: "p", label: "P", numeric: true }]),
      board("Assists", data.assists, [favCol, nameCol, teamCol, { key: "a", label: "A", numeric: true }, { key: "p", label: "P", numeric: true }]),
      board("Penalty minutes", data.pim, [favCol, nameCol, teamCol, { key: "pim", label: "PIM", numeric: true }]),
      board("Goalie wins", data.goalies, [favCol, nameCol, teamCol, { key: "goalie_gp", label: "GP", numeric: true }, { key: "goalie_w", label: "W", numeric: true }, { key: "goalie_l", label: "L", numeric: true }, { key: "gaa", label: "GAA", numeric: true }]),
      board("Goals against avg (min 2 GP)", data.gaa, [favCol, nameCol, teamCol, { key: "goalie_gp", label: "GP", numeric: true }, { key: "ga", label: "GA", numeric: true }, { key: "gaa", label: "GAA", numeric: true }]),
    );
  } catch (err) {
    status.textContent = `Could not load leaders: ${err.message}`;
    root.innerHTML = "";
  }
}

async function loadPlayer(participantId, params) {
  showView("player", "Player");
  const status = document.getElementById("player-status");
  status.textContent = "Loading player…";
  try {
    const qs = query({
      season_id: params.season_id || filterData?.defaultSeason || "2026-27",
      schedule_id: params.schedule_id,
    });
    const data = await getJson(`/api/players/${participantId}?${qs}`);
    pageTitle.textContent = data.player.displayName || data.player.full_name;
    status.textContent = "";
    const seasonId = data.seasonId || params.season_id || "2026-27";
    document.getElementById("player-header").innerHTML = `
      <p class="meta"><a href="#/search">← Search</a> · Point streak: ${data.pointStreak} game${data.pointStreak === 1 ? "" : "s"}</p>
      <div class="team-heading-row">
        ${favoriteButtonHtml(
          {
            type: "player",
            participantId,
            seasonId,
            scheduleId: params.schedule_id || null,
            teamId: (data.stats || [])[0]?.team_id || null,
            name: data.player.displayName || data.player.full_name,
          },
          { size: "lg" },
        )}
        <h2>${escapeHtml(data.player.displayName || data.player.full_name)}</h2>
      </div>
    `;
    document.getElementById("player-splits").innerHTML = `
      <div class="split"><span class="card-label">Home</span> ${data.splits.home.gp} GP · ${data.splits.home.g}-${data.splits.home.a}-${data.splits.home.p} · ${data.splits.home.pim} PIM</div>
      <div class="split"><span class="card-label">Away</span> ${data.splits.away.gp} GP · ${data.splits.away.g}-${data.splits.away.a}-${data.splits.away.p} · ${data.splits.away.pim} PIM</div>
    `;
    sortableTable(
      document.getElementById("player-stats"),
      [
        {
          key: "team_name",
          label: "Team",
          render: (r) =>
            r.team_id
              ? teamLinkLabel(
                  r.team_name,
                  r.logoUrl,
                  `#/team/${r.team_id}?season_id=${encodeURIComponent(data.seasonId)}&schedule_id=${r.schedule_id}`,
                  { size: "sm" },
                )
              : "",
        },
        { key: "gp", label: "GP", numeric: true },
        { key: "g", label: "G", numeric: true },
        { key: "a", label: "A", numeric: true },
        { key: "p", label: "P", numeric: true },
        { key: "pim", label: "PIM", numeric: true },
        { key: "ppg", label: "PPG", numeric: true },
        { key: "shg", label: "SHG", numeric: true },
        { key: "gwg", label: "GWG", numeric: true },
      ],
      data.stats,
    );
    sortableTable(
      document.getElementById("player-log"),
      [
        { key: "date", label: "Date" },
        {
          key: "matchup",
          label: "Game",
          render: (r) => {
            const oppName = r.isHome ? r.away_name : r.home_name;
            const oppLogo = r.isHome ? r.awayLogoUrl : r.homeLogoUrl;
            const prefix = r.isHome ? "vs" : "@";
            return `<a class="team-with-logo" href="#/game/${r.game_id}"><span class="vs-prefix">${prefix}</span>${teamLogo(oppLogo, oppName, { size: "sm" })}<span class="team-name">${escapeHtml(oppName || "TBD")}</span></a>`;
          },
        },
        { key: "g", label: "G", numeric: true },
        { key: "a", label: "A", numeric: true },
        { key: "p", label: "P", numeric: true },
        { key: "pim", label: "PIM", numeric: true },
        {
          key: "scoresheetUrl",
          label: "Sheet",
          render: (r) => `<a href="${escapeHtml(r.scoresheetUrl)}" target="_blank" rel="noreferrer">PDF</a>`,
        },
      ],
      data.gameLog.map((g) => ({ ...g, matchup: g.isHome ? g.away_name : g.home_name })),
    );
  } catch (err) {
    status.textContent = `Could not load player: ${err.message}`;
  }
}

async function loadSearch(params = {}) {
  showView("search", "Search");
  // Prefer SQLite seasons so Search works without Spordle filters.
  if (!searchForm.elements.season_id.options.length) {
    try {
      const meta = await getJson("/api/standings/schedules");
      const seasons = meta.seasons?.length ? meta.seasons : ["2026-27"];
      fillSelect(searchForm.elements.season_id, seasons, { value: (s) => s, label: (s) => s });
      searchForm.elements.season_id.value = meta.defaultSeason || seasons[0];
    } catch {
      fillSelect(searchForm.elements.season_id, ["2026-27"], { value: (s) => s, label: (s) => s });
      searchForm.elements.season_id.value = "2026-27";
    }
  }
  if (params.q) searchForm.elements.q.value = params.q;
  if (params.season_id) searchForm.elements.season_id.value = params.season_id;
  const q = searchForm.elements.q.value.trim();
  const status = document.getElementById("search-status");
  const root = document.getElementById("search-results");
  if (q.length < 2) {
    status.textContent = "Type at least 2 characters.";
    root.innerHTML = "";
    return;
  }
  status.textContent = "Searching…";
  try {
    const season = searchForm.elements.season_id.value || "2026-27";
    const data = await getJson(
      `/api/search?${query({ q, season_id: season })}`,
    );
    status.textContent = `${data.players.length} players · ${data.teams.length} teams`;
    root.innerHTML = `
      <div>
        <h2 class="section-title">Players</h2>
        <div class="result-list">
          ${
            data.players
              .map(
                (p) => `
              <div class="result result-with-fav">
                ${favoriteButtonHtml({
                  type: "player",
                  participantId: p.participant_id,
                  seasonId: season,
                  scheduleId: p.schedule_id,
                  teamId: p.team_id,
                  name: p.displayName || p.full_name,
                })}
                <div class="result-body">
                  <a href="#/player/${p.participant_id}?season_id=${encodeURIComponent(season)}${p.schedule_id ? `&schedule_id=${p.schedule_id}` : ""}">
                    <strong>${escapeHtml(p.displayName || titleCase(p.full_name))}</strong>
                  </a>
                  <div class="result-meta">
                    ${
                      p.team_id
                        ? teamLinkLabel(
                            p.team_name,
                            p.logoUrl,
                            `#/team/${p.team_id}?season_id=${encodeURIComponent(season)}${p.schedule_id ? `&schedule_id=${p.schedule_id}` : ""}`,
                            { size: "sm" },
                          )
                        : `<span class="muted">No team</span>`
                    }
                    ${p.p != null ? `<span class="muted">· ${p.p} pts</span>` : ""}
                  </div>
                </div>
              </div>`,
              )
              .join("") || `<p class="muted">No players</p>`
          }
        </div>
      </div>
      <div>
        <h2 class="section-title">Teams</h2>
        <div class="result-list">
          ${
            data.teams
              .map(
                (t) => `
              <div class="result result-with-fav">
                ${favoriteButtonHtml({
                  type: "team",
                  teamId: t.id,
                  seasonId: season,
                  scheduleId: t.schedule_id,
                  name: t.name,
                  logoUrl: t.logoUrl,
                })}
                <a href="#/team/${t.id}?season_id=${encodeURIComponent(season)}${t.schedule_id ? `&schedule_id=${t.schedule_id}` : ""}">
                  <strong class="team-with-logo">${teamLogo(t.logoUrl, t.name, { size: "sm" })}<span class="team-name">${escapeHtml(t.name)}</span></strong>
                  <span>${escapeHtml(t.schedule_name || "")}${t.pts != null ? ` · ${t.pts} pts` : ""}</span>
                </a>
              </div>`,
              )
              .join("") || `<p class="muted">No teams</p>`
          }
        </div>
      </div>
    `;
  } catch (err) {
    status.textContent = `Search failed: ${err.message}`;
    root.innerHTML = "";
  }
}

async function loadGame(gameId) {
  showView("game", "Game recap");
  const status = document.getElementById("game-status");
  status.textContent = "Loading recap…";
  try {
    const data = await getJson(`/api/games/${gameId}/recap`);
    const g = data.game;
    pageTitle.textContent = g.number || `Game ${gameId}`;
    status.textContent = "";
    document.getElementById("game-header").innerHTML = `
      <p class="meta">${escapeHtml(g.date || "")} · ${escapeHtml(g.schedule_name || "")}${g.group_name ? ` · ${escapeHtml(g.group_name)}` : ""}</p>
      <h2 class="matchup-score">
        ${teamLinkLabel(g.away_name || "Away", g.awayLogoUrl, `#/team/${g.away_team_id}?season_id=${encodeURIComponent(g.season_id || "2026-27")}`, { size: "lg" })}
        <span class="score-mid">${g.away_score ?? "—"} – ${g.home_score ?? "—"}</span>
        ${teamLinkLabel(g.home_name || "Home", g.homeLogoUrl, `#/team/${g.home_team_id}?season_id=${encodeURIComponent(g.season_id || "2026-27")}`, { size: "lg" })}
      </h2>
      <p><a class="sheet" href="${escapeHtml(g.scoresheetUrl)}" target="_blank" rel="noreferrer">Scoresheet PDF</a></p>
    `;

    sortableTable(
      document.getElementById("game-goals"),
      [
        { key: "period", label: "Per" },
        {
          key: "clock",
          label: "Time",
          render: (r) =>
            r.minutes != null ? `${r.minutes}:${String(r.seconds ?? 0).padStart(2, "0")}` : "—",
        },
        {
          key: "scorerDisplay",
          label: "Goal",
          render: (r) => {
            const assists = (r.assists || []).map((a) => a.displayName).filter(Boolean).join(", ");
            const flags = [
              r.is_powerplay ? "PP" : "",
              r.is_shorthanded ? "SH" : "",
              r.is_empty_net ? "EN" : "",
              r.is_game_winner ? "GWG" : "",
            ]
              .filter(Boolean)
              .join(" ");
            return `${escapeHtml(r.scorerDisplay || "—")}${assists ? ` (${escapeHtml(assists)})` : ""}${flags ? ` <span class="tag">${escapeHtml(flags)}</span>` : ""}`;
          },
        },
        {
          key: "team_id",
          label: "Team",
          render: (r) => {
            const isHome = r.team_id === g.home_team_id;
            return teamLinkLabel(
              isHome ? g.home_name : g.away_name,
              isHome ? g.homeLogoUrl : g.awayLogoUrl,
              `#/team/${r.team_id}?season_id=${encodeURIComponent(g.season_id || "2026-27")}`,
              { size: "sm" },
            );
          },
        },
      ],
      data.goals,
    );

    sortableTable(
      document.getElementById("game-penalties"),
      [
        { key: "period", label: "Per" },
        {
          key: "clock",
          label: "Time",
          render: (r) =>
            r.minutes != null ? `${r.minutes}:${String(r.seconds ?? 0).padStart(2, "0")}` : "—",
        },
        { key: "displayName", label: "Player", render: (r) => escapeHtml(r.displayName || "—") },
        { key: "infraction", label: "Infraction" },
        { key: "duration", label: "Type" },
        { key: "pim", label: "PIM", numeric: true },
      ],
      data.penalties,
    );

    const lineups = document.getElementById("game-lineups");
    lineups.innerHTML = ["away", "home"]
      .map((side) => {
        const title = side === "home" ? g.home_name : g.away_name;
        const logo = side === "home" ? g.homeLogoUrl : g.awayLogoUrl;
        const teamId = side === "home" ? g.home_team_id : g.away_team_id;
        const members = data.lineups[side] || [];
        return `
          <div>
            <h3 class="section-title">${teamLinkLabel(
              title || side,
              logo,
              teamId
                ? `#/team/${teamId}?season_id=${encodeURIComponent(g.season_id || "2026-27")}`
                : null,
              { size: "md" },
            )} <span class="scoreboard-ha">${side}</span></h3>
            <div class="table-wrap" data-side="${side}"></div>
          </div>
        `;
      })
      .join("");
    for (const side of ["away", "home"]) {
      sortableTable(
        lineups.querySelector(`[data-side="${side}"]`),
        [
          { key: "number", label: "#", numeric: true },
          {
            key: "displayName",
            label: "Name",
            render: (r) =>
              `<a href="#/player/${r.participant_id}?season_id=${encodeURIComponent(g.season_id || "2026-27")}">${escapeHtml(r.displayName || "")}</a>${
                r.is_affiliate ? ' <span class="tag">AP</span>' : ""
              }`,
          },
          { key: "positions", label: "Pos" },
        ],
        data.lineups[side] || [],
      );
    }
  } catch (err) {
    status.textContent = `Could not load recap: ${err.message}`;
  }
}

function isStandingsFamily(view) {
  return ["standings", "team", "leaders", "player", "search", "game"].includes(view);
}

function todayIso() {
  const d = new Date();
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function isoToMmDdYyyy(iso) {
  const m = String(iso || "").match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (!m) return "";
  return `${m[2]}/${m[3]}/${m[1]}`;
}

function csvEscape(value) {
  const s = String(value ?? "");
  if (/[",\n\r]/.test(s)) return `"${s.replaceAll('"', '""')}"`;
  return s;
}

/** TeamSnap team schedule import columns (instructional first column omitted). */
const TEAMSNAP_HEADERS = [
  "Date",
  "Time",
  "Duration (HH:MM)",
  "Arrival Time (Minutes)",
  "Name",
  "Opponent Name",
  "Opponent Contact Name",
  "Opponent Contact Phone Number",
  "Opponent Contact E-mail Address",
  "Location Name",
  "Location Address",
  "Location Details",
  "Location URL",
  "Home or Away",
  "Uniform",
  "Extra Label",
  "Notes",
];

function gameToTeamsnapRow(game, teamId) {
  const tid = Number(teamId);
  const isHome = Number(game.homeTeamId) === tid;
  const opponent = isHome ? game.away : game.home;
  const notes = [game.scheduleType, game.schedule, game.group, game.number]
    .filter(Boolean)
    .join(" · ");
  return [
    isoToMmDdYyyy(game.date),
    game.startTime || "",
    game.duration || "1:15",
    "30",
    "",
    opponent || "",
    "",
    "",
    "",
    game.venueName || game.venue || "",
    game.venueAddress || "",
    game.locationDetails || "",
    "",
    isHome ? "h" : "a",
    "",
    game.number || "",
    notes,
  ];
}

function downloadTeamsnapCsv(games, teamId, teamName) {
  const rows = (games || []).map((g) => gameToTeamsnapRow(g, teamId));
  const lines = [
    TEAMSNAP_HEADERS.map(csvEscape).join(","),
    ...rows.map((r) => r.map(csvEscape).join(",")),
  ];
  const blob = new Blob([lines.join("\r\n") + "\r\n"], {
    type: "text/csv;charset=utf-8",
  });
  const slug = String(teamName || "team")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "")
    .slice(0, 40);
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `${slug || "team"}-teamsnap-schedule.csv`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

async function fetchAllPublicGames(baseParams) {
  const pageSize = 100;
  let pageNum = 1;
  let pages = 1;
  const all = [];
  while (pageNum <= pages) {
    const data = await getJson(
      `/api/games?${query({ ...baseParams, page: pageNum, page_size: pageSize })}`,
    );
    all.push(...(data.games || []));
    pages = data.pages || 1;
    pageNum += 1;
    if (pageNum > 20) break; // safety: max 2000 games
  }
  return all;
}

function renderCompactGameCard(g, { highlightTeamId, seasonId } = {}) {
  const when = [g.date, g.startTime && g.endTime ? `${g.startTime} – ${g.endTime}` : g.startTime]
    .filter(Boolean)
    .join(" · ");
  const league = [g.scheduleType || g.schedule_type, g.schedule || g.schedule_name, g.group || g.group_name]
    .filter(Boolean)
    .join(" · ");
  const venue = [g.venue, g.city].filter(Boolean).join(" · ");
  const scoreboardGame = {
    ...g,
    away_name: g.away_name || g.away,
    home_name: g.home_name || g.home,
    away_score: g.away_score ?? g.awayScore,
    home_score: g.home_score ?? g.homeScore,
    away_team_id: g.away_team_id ?? g.awayTeamId,
    home_team_id: g.home_team_id ?? g.homeTeamId,
    awayLogoUrl: g.awayLogoUrl,
    homeLogoUrl: g.homeLogoUrl,
  };
  return `
    <article class="game compact">
      <div>
        <div class="meta">${escapeHtml(when)}${g.number ? ` · ${escapeHtml(g.number)}` : ""}${
          league ? ` · ${escapeHtml(league)}` : ""
        }</div>
        ${stackedScoreboard(scoreboardGame, {
          highlightTeamId,
          seasonId,
        })}
        ${venue ? `<div class="venue">${escapeHtml(venue)}</div>` : ""}
      </div>
      <div class="side">
        <a class="sheet" href="#/game/${g.id}">Recap</a>
        <a class="sheet ghost" href="${escapeHtml(g.scoresheetUrl)}" target="_blank" rel="noreferrer">PDF</a>
      </div>
    </article>
  `;
}

let teamFutureGamesCache = [];
let teamFutureExportCtx = null;

async function loadTeamFutureGames(teamId, { seasonId, scheduleId, teamName } = {}) {
  const listEl = document.getElementById("team-future-games");
  const status = document.getElementById("team-future-status");
  const exportBtn = document.getElementById("team-teamsnap-export");
  if (!listEl) return;
  status.textContent = "Loading future games…";
  exportBtn.hidden = true;
  teamFutureGamesCache = [];
  teamFutureExportCtx = { teamId, teamName };
  try {
    const games = await fetchAllPublicGames({
      from_day: todayIso(),
      team_id: teamId,
      season_id: seasonId,
      schedule_id: scheduleId || undefined,
    });
    teamFutureGamesCache = games;
    if (!games.length) {
      status.textContent = "";
      listEl.innerHTML = `<p class="muted">No upcoming games found for this filter.</p>`;
      return;
    }
    status.textContent = `${games.length} upcoming game${games.length === 1 ? "" : "s"}`;
    listEl.innerHTML = games
      .map((g) => renderCompactGameCard(g, { highlightTeamId: teamId, seasonId }))
      .join("");
    exportBtn.hidden = false;
  } catch (err) {
    status.textContent = `Could not load future games: ${err.message}`;
    listEl.innerHTML = "";
  }
}

function pickLastAndNextGame(games) {
  const today = todayIso();
  const sorted = [...(games || [])].sort((a, b) => {
    const d = String(a.date).localeCompare(String(b.date));
    return d !== 0 ? d : Number(a.id) - Number(b.id);
  });
  const pastOrToday = sorted.filter((g) => String(g.date) <= today);
  const future = sorted.filter((g) => String(g.date) > today);
  const last = pastOrToday.length ? pastOrToday[pastOrToday.length - 1] : null;
  let next = future.length ? future[0] : null;
  if (!next && last && String(last.date) === today) next = last;
  return { last, next };
}

function formatFollowedGame(g, teamId) {
  if (!g) return `<div class="muted">—</div>`;
  const hasScore = g.home_score != null && g.away_score != null;
  return `
    <div class="meta">${escapeHtml(g.date)}${g.number ? ` · ${escapeHtml(g.number)}` : ""}</div>
    ${stackedScoreboard(g, { highlightTeamId: teamId })}
    ${hasScore ? "" : `<div class="muted">Scheduled</div>`}
    <a href="#/game/${g.id}">Recap</a>
  `;
}

async function loadFavorites() {
  showView("favorites", "Favorites");
  const status = document.getElementById("favorites-status");
  const list = document.getElementById("favorites-list");
  const favorites = getFavorites();
  const teams = favorites.filter((f) => f.type === "team");
  const players = favorites.filter((f) => f.type === "player");
  if (!favorites.length) {
    status.textContent = "";
    list.innerHTML = `
      <div class="favorites-empty">
        <h2>Save your favorites</h2>
        <p>Tap the star on any team or player. Favorites stay on this device — no account needed.</p>
        <div class="favorites-empty-actions">
          <a class="btn-link" href="#/standings">Browse standings</a>
          <a class="btn-link secondary" href="#/search">Search players</a>
        </div>
      </div>
    `;
    return;
  }
  status.textContent = `Loading ${favorites.length} favorite${favorites.length === 1 ? "" : "s"}…`;
  list.innerHTML = "";

  const teamCards = await Promise.all(
    teams.map(async (f) => {
      try {
        const qs = query({
          season_id: f.seasonId,
          schedule_id: f.scheduleId,
        });
        const data = await getJson(`/api/teams/${f.teamId}/roster?${qs}`);
        const team = data.team || { id: f.teamId, name: f.name, logo_url: f.logoUrl };
        const st = (data.standings || [])[0];
        const { last, next } = pickLastAndNextGame(data.games || []);
        const teamHref = `#/team/${f.teamId}?${query({
          season_id: f.seasonId,
          schedule_id: f.scheduleId,
        })}`;
        const standingsHref = `#/standings?${query({
          season_id: f.seasonId,
          schedule_id: f.scheduleId,
        })}`;
        return `
          <article class="favorite-card">
            <div class="favorite-card-top">
              <div>
                ${teamLinkLabel(team.name || f.name, team.logo_url || team.logoUrl || f.logoUrl, teamHref, { size: "lg" })}
                <div class="favorite-meta">
                  Team · ${escapeHtml(f.seasonId)}${st ? ` · ${st.gp} GP · ${st.pts} PTS · ${st.w}-${st.l}-${st.t}` : ""}
                </div>
              </div>
              ${favoriteButtonHtml({ ...f, name: team.name || f.name, logoUrl: team.logo_url || team.logoUrl || f.logoUrl }, { size: "lg" })}
            </div>
            <div class="favorite-games">
              <div class="favorite-game">
                <div class="label">Last game</div>
                ${formatFollowedGame(last, f.teamId)}
              </div>
              <div class="favorite-game">
                <div class="label">Next / today</div>
                ${formatFollowedGame(next, f.teamId)}
              </div>
            </div>
            <div class="favorite-actions">
              <a href="${teamHref}">Roster &amp; stats</a>
              <a href="${standingsHref}">League table</a>
            </div>
          </article>
        `;
      } catch (err) {
        return `
          <article class="favorite-card">
            <div class="favorite-card-top">
              <div>
                <strong>${escapeHtml(f.name || `Team ${f.teamId}`)}</strong>
                <div class="favorite-meta">Team · ${escapeHtml(f.seasonId)} · could not load (${escapeHtml(err.message)})</div>
              </div>
              ${favoriteButtonHtml(f, { size: "lg" })}
            </div>
          </article>
        `;
      }
    }),
  );

  const playerCards = await Promise.all(
    players.map(async (f) => {
      try {
        const qs = query({
          season_id: f.seasonId,
          schedule_id: f.scheduleId,
        });
        const data = await getJson(`/api/players/${f.participantId}?${qs}`);
        const name = data.player.displayName || data.player.full_name || f.name;
        const st = (data.stats || [])[0];
        const totals = (data.stats || []).reduce(
          (acc, r) => {
            acc.gp += r.gp || 0;
            acc.g += r.g || 0;
            acc.a += r.a || 0;
            acc.p += r.p || 0;
            acc.pim += r.pim || 0;
            return acc;
          },
          { gp: 0, g: 0, a: 0, p: 0, pim: 0 },
        );
        const playerHref = `#/player/${f.participantId}?${query({
          season_id: f.seasonId,
          schedule_id: f.scheduleId,
        })}`;
        const teamHref = st
          ? `#/team/${st.team_id}?${query({
              season_id: f.seasonId,
              schedule_id: st.schedule_id,
            })}`
          : "";
        return `
          <article class="favorite-card">
            <div class="favorite-card-top">
              <div>
                <a class="favorite-player-name" href="${playerHref}">${escapeHtml(titleCase(name))}</a>
                <div class="favorite-meta">
                  Player · ${escapeHtml(f.seasonId)}
                  ${totals.gp ? ` · ${totals.gp} GP · ${totals.g}-${totals.a}-${totals.p} · ${totals.pim} PIM` : ""}
                  ${data.pointStreak ? ` · ${data.pointStreak}-game point streak` : ""}
                </div>
                ${st?.team_name
                  ? `<div class="favorite-meta">${teamLinkLabel(
                      st.team_name,
                      st.logoUrl,
                      teamHref ||
                        `#/team/${st.team_id}?season_id=${encodeURIComponent(f.seasonId)}${
                          st.schedule_id ? `&schedule_id=${st.schedule_id}` : ""
                        }`,
                      { size: "sm" },
                    )}</div>`
                  : ""}
              </div>
              ${favoriteButtonHtml({ ...f, name }, { size: "lg" })}
            </div>
            <div class="favorite-actions">
              <a href="${playerHref}">Player page</a>
              ${teamHref ? `<a href="${teamHref}">Team</a>` : ""}
            </div>
          </article>
        `;
      } catch (err) {
        return `
          <article class="favorite-card">
            <div class="favorite-card-top">
              <div>
                <strong>${escapeHtml(titleCase(f.name) || `Player ${f.participantId}`)}</strong>
                <div class="favorite-meta">Player · ${escapeHtml(f.seasonId)} · could not load (${escapeHtml(err.message)})</div>
              </div>
              ${favoriteButtonHtml(f, { size: "lg" })}
            </div>
          </article>
        `;
      }
    }),
  );

  const sections = [];
  if (teamCards.length) {
    sections.push(`<h2 class="favorites-section-title">Teams</h2><div class="favorites-grid">${teamCards.join("")}</div>`);
  }
  if (playerCards.length) {
    sections.push(`<h2 class="favorites-section-title">Players</h2><div class="favorites-grid">${playerCards.join("")}</div>`);
  }
  const bits = [];
  if (teams.length) bits.push(`${teams.length} team${teams.length === 1 ? "" : "s"}`);
  if (players.length) bits.push(`${players.length} player${players.length === 1 ? "" : "s"}`);
  status.textContent = bits.join(" · ");
  list.innerHTML = sections.join("");
}

async function ensureScheduleFilters() {
  if (filterData) return;
  await loadFilters();
  await loadSchedules();
  await loadGroups();
}

async function route() {
  const { parts, params } = parseHash();
  const view = parts[0] || "favorites";

  if (view === "favorites" || view === "my-teams" || view === "home" || view === "following") {
    await loadFavorites();
    return;
  }
  if (view === "standings") {
    showView("standings", "Standings");
    const status = document.getElementById("standings-status");
    if (status) status.textContent = "Loading standings…";
    suppressStandingsEvents = true;
    try {
      if (params.season_id) standingsForm.elements.season_id.value = params.season_id;
      if (params.gender && standingsForm.elements.gender) {
        standingsForm.elements.gender.value = params.gender;
      }
      if (params.division) standingsForm.elements.division.value = params.division;
      if (params.type) {
        const typeEl = standingsTypeSelect(standingsForm);
        if (typeEl) typeEl.value = params.type;
      }
    } finally {
      suppressStandingsEvents = false;
    }
    await loadStandingsSchedules({
      preserveSchedule: Boolean(params.schedule_id),
      autoSelect: !params.schedule_id,
    });
    if (params.schedule_id) {
      suppressStandingsEvents = true;
      try {
        standingsForm.elements.schedule_id.value = String(params.schedule_id);
        updateStandingsGroups();
        if (params.group_id) standingsForm.elements.group_id.value = String(params.group_id);
      } finally {
        suppressStandingsEvents = false;
      }
      renderStandingsScheduleList();
    }
    await loadStandings();
    return;
  }
  if (view === "team" && parts[1]) {
    await loadTeam(parts[1], params);
    return;
  }
  if (view === "leaders") {
    showView("leaders", "Leaders");
    if (params.season_id) leadersForm.elements.season_id.value = params.season_id;
    if (params.division) leadersForm.elements.division.value = params.division;
    await loadLeadersSchedules();
    if (params.schedule_id) leadersForm.elements.schedule_id.value = params.schedule_id;
    await loadLeaders();
    return;
  }
  if (view === "player" && parts[1]) {
    await loadPlayer(parts[1], params);
    return;
  }
  if (view === "search") {
    await loadSearch(params);
    return;
  }
  if (view === "game" && parts[1]) {
    await loadGame(parts[1]);
    return;
  }
  if (view === "refs") {
    await loadRefs();
    return;
  }

  showView("schedule", "Schedule");
  await ensureScheduleFilters();
  await loadGames();
}

form.addEventListener("change", async (event) => {
  page = 1;
  const name = event.target.name;
  try {
    await ensureScheduleFilters();
  } catch (err) {
    statusEl.textContent = `Could not load schedule filters: ${err.message}`;
    return;
  }
  if (["season_id", "office_id", "division", "gender", "type"].includes(name)) {
    await loadSchedules();
    await loadGroups();
  } else if (name === "schedule_id") {
    await loadGroups();
  }
  await loadGames();
});

pagerEl.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-page]");
  if (!button || button.disabled) return;
  const next = Number(button.dataset.page);
  if (!Number.isFinite(next) || next < 1 || next === page) return;
  page = next;
  await loadGames();
  pagerEl.scrollIntoView({ block: "nearest", behavior: "smooth" });
});

pageSizeEl?.addEventListener("change", async () => {
  pageSize = Number(pageSizeEl.value) || 25;
  page = 1;
  await loadGames();
});

document.getElementById("team-teamsnap-export")?.addEventListener("click", () => {
  if (!teamFutureGamesCache.length || !teamFutureExportCtx) return;
  downloadTeamsnapCsv(
    teamFutureGamesCache,
    teamFutureExportCtx.teamId,
    teamFutureExportCtx.teamName,
  );
});

standingsForm.addEventListener("change", async (event) => {
  if (suppressStandingsEvents) return;
  const name = event.target.name;
  if (["season_id", "gender", "division", "type"].includes(name)) {
    await loadStandingsSchedules({
      preserveSchedule: name !== "season_id" && name !== "gender",
      autoSelect: true,
    });
  } else if (name === "schedule_id") {
    updateStandingsGroups();
    renderStandingsScheduleList();
  }
  setHash("standings", standingsHashParams());
});

document.getElementById("standings-schedule-list")?.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-schedule-id]");
  if (!button) return;
  const sid = button.dataset.scheduleId;
  if (![...standingsForm.elements.schedule_id.options].some((o) => o.value === sid)) return;
  standingsForm.elements.schedule_id.value = sid;
  updateStandingsGroups();
  renderStandingsScheduleList();
  const next = `#/standings?${query(standingsHashParams())}`;
  if (location.hash === next) {
    await loadStandings();
  } else {
    location.hash = next;
  }
});

leadersForm.addEventListener("change", async (event) => {
  if (["season_id", "division"].includes(event.target.name)) await loadLeadersSchedules();
  setHash("leaders", {
    season_id: leadersForm.elements.season_id.value,
    division: leadersForm.elements.division.value,
    schedule_id: leadersForm.elements.schedule_id.value,
  });
});

teamForm.addEventListener("change", (event) => {
  if (!currentTeamId) return;
  const { params } = parseHash();
  const next = {
    season_id: params.season_id || filterData?.defaultSeason || "2026-27",
  };
  const type = teamForm.elements.type.value;
  const scheduleId = teamForm.elements.schedule_id.value;
  if (event.target.name === "type") {
    if (type) next.type = type;
    else next.scope = "all";
  } else if (scheduleId) {
    if (type) next.type = type;
    next.schedule_id = scheduleId;
  } else if (type) {
    next.type = type;
  } else {
    next.scope = "all";
  }
  setHash(`team/${currentTeamId}`, next);
});

searchForm.addEventListener("submit", (event) => {
  event.preventDefault();
  setHash("search", {
    q: searchForm.elements.q.value.trim(),
    season_id: searchForm.elements.season_id.value,
  });
});

window.addEventListener("hashchange", () => {
  route().catch((err) => {
    console.error(err);
  });
});

document.addEventListener("click", (event) => {
  const btn = event.target.closest("button.favorite-btn, button.follow-btn");
  if (!btn) return;
  event.preventDefault();
  event.stopPropagation();
  const type = btn.dataset.favType || "team";
  const entry =
    type === "player"
      ? {
          type: "player",
          participantId: btn.dataset.favParticipant,
          seasonId: btn.dataset.favSeason,
          scheduleId: btn.dataset.favSchedule || null,
          teamId: btn.dataset.favTeam || null,
          name: btn.dataset.favName || "",
        }
      : {
          type: "team",
          teamId: btn.dataset.favTeam || btn.dataset.followTeam,
          seasonId: btn.dataset.favSeason || btn.dataset.followSeason,
          scheduleId: btn.dataset.favSchedule || btn.dataset.followSchedule || null,
          name: btn.dataset.favName || btn.dataset.followName || "",
          logoUrl: btn.dataset.favLogo || btn.dataset.followLogo || null,
        };
  const on = toggleFavorite(entry);
  btn.classList.toggle("on", on);
  btn.setAttribute("aria-pressed", on ? "true" : "false");
  const label = entry.name || (type === "player" ? "player" : "team");
  const verb = on ? "Remove from favorites" : "Add to favorites";
  btn.setAttribute("aria-label", `${verb}: ${label}`);
  btn.title = verb;
  const { parts } = parseHash();
  const view = parts[0] || "favorites";
  if (view === "favorites" || view === "my-teams" || view === "home" || view === "following") {
    loadFavorites().catch(console.error);
  }
});

const REFS_AGE_GROUPS = ["U7", "U9", "U11", "U13", "U15", "U18", "U21"];

function refsAgeGroupOptions() {
  const fromApi = filterData?.divisions || [];
  const byName = new Map(
    fromApi.map((d) => [String(d.name || d).toUpperCase(), d.name || d]),
  );
  // Fixed PCAHA/ice list — ignore Spordle extras like U4, Junior, Other.
  return REFS_AGE_GROUPS.map((name) => {
    const match = byName.get(name);
    if (match && typeof match === "object") return match;
    return { id: name, name };
  });
}
const refsAuthEl = document.getElementById("refs-auth");
const refsAppEl = document.getElementById("refs-app");
const refsGamesEl = document.getElementById("refs-games");
const refsStatusEl = document.getElementById("refs-status");
const refsPagerEl = document.getElementById("refs-pager");
const refsForms = {
  open: document.getElementById("refs-filters-open"),
  requested: document.getElementById("refs-filters-requested"),
  assigned: document.getElementById("refs-filters-assigned"),
};

let refsMe = null;
let refsView = "open"; // open | requested | assigned
let refsPage = 1;
let refsLoading = false;
let refsSuppressEvents = false;
let refsLoadSeq = 0;
let refsFiltersReady = false;

function refsActiveForm() {
  return refsForms[refsView] || refsForms.open;
}

function showRefsFilterForm() {
  for (const [key, form] of Object.entries(refsForms)) {
    if (!form) continue;
    const active = key === refsView;
    form.hidden = !active;
    form.style.display = active ? "" : "none";
  }
}

function beginRefsLoad() {
  refsLoadSeq += 1;
  refsLoading = true;
  return refsLoadSeq;
}

function isRefsLoadCurrent(seq) {
  return seq === refsLoadSeq;
}

function getRefsPrefs() {
  try {
    return JSON.parse(localStorage.getItem(REFS_PREFS_KEY) || "{}") || {};
  } catch {
    return {};
  }
}

function setRefsPrefs(patch) {
  const next = { ...getRefsPrefs(), ...patch };
  localStorage.setItem(REFS_PREFS_KEY, JSON.stringify(next));
  return next;
}

async function refsApi(path, { method = "GET", body } = {}) {
  const opts = {
    method,
    credentials: "same-origin",
    headers: { Accept: "application/json" },
  };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const resp = await fetch(`/api/refs${path}`, opts);
  const text = await resp.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = { detail: text };
  }
  if (!resp.ok) {
    const detail = data?.detail;
    const msg =
      typeof detail === "string"
        ? detail
        : Array.isArray(detail)
          ? detail.map((d) => d.msg || JSON.stringify(d)).join("; ")
          : resp.statusText;
    const err = new Error(msg || `HTTP ${resp.status}`);
    err.status = resp.status;
    throw err;
  }
  return data;
}

function renderRefsAuth() {
  if (!refsAuthEl) return;
  if (refsMe?.signedIn) {
    const identities = refsMe.identities || [];
    refsAuthEl.innerHTML = `
      <div class="refs-session">
        <div>
          <strong>${escapeHtml(refsMe.displayName || refsMe.username)}</strong>
          <span class="muted"> · signed in · password not stored</span>
          ${
            (refsMe.identities || []).find((i) => String(i.id) === String(refsMe.identityId))
              ?.tenant
              ? `<div class="muted">Active: ${escapeHtml(
                  (refsMe.identities || []).find((i) => String(i.id) === String(refsMe.identityId))
                    ?.tenant || "",
                )}</div>`
              : ""
          }
          ${
            refsMe.grades && Object.keys(refsMe.grades).length
              ? `<div class="muted">Grades: ${escapeHtml(
                  Object.entries(refsMe.grades)
                    .map(([k, v]) => `${k} ${v}`)
                    .join(" · "),
                )}</div>`
              : ""
          }
        </div>
        <div class="refs-session-actions">
          ${
            identities.length > 1
              ? `<label class="refs-identity">Identity
                  <select id="refs-identity">
                    ${identities
                      .map(
                        (i) =>
                          `<option value="${escapeHtml(i.id)}" ${
                            String(i.id) === String(refsMe.identityId) ? "selected" : ""
                          }>${escapeHtml(i.name || i.id)}${
                            i.tenant ? ` (${escapeHtml(i.tenant)})` : ""
                          }</option>`,
                      )
                      .join("")}
                  </select>
                </label>`
              : ""
          }
          <button type="button" id="refs-logout" class="btn-export">Sign out</button>
        </div>
      </div>
    `;
    refsAppEl.hidden = false;
  } else {
    refsAppEl.hidden = true;
    refsAuthEl.innerHTML = `
      <div class="refs-login-card">
        <h2>Sign in with Spordle</h2>
        <p class="muted">
          Your password is sent once to Spordle to get a session token.
          We never save your password — only a short-lived session cookie on this device.
        </p>
        <form id="refs-login-form" class="refs-login-form">
          <label>
            Email / username
            <input name="username" type="text" autocomplete="username" required />
          </label>
          <label>
            Password
            <input name="password" type="password" autocomplete="current-password" required />
          </label>
          <button type="submit" class="sheet">Sign in</button>
          <p id="refs-login-error" class="status" hidden></p>
        </form>
      </div>
    `;
  }
}

async function refreshRefsMe() {
  refsMe = await refsApi("/me");
  renderRefsAuth();
  return refsMe;
}

function applyRefsPrefsToForm() {
  const prefs = getRefsPrefs();
  const open = refsForms.open;
  if (open) {
    if (prefs.home && open.elements.home) open.elements.home.value = prefs.home;
    if (prefs.max_distance_km != null && open.elements.max_distance_km) {
      open.elements.max_distance_km.value = prefs.max_distance_km;
    }
    if (prefs.position && open.elements.position) open.elements.position.value = prefs.position;
    if (prefs.crew && open.elements.crew) open.elements.crew.value = prefs.crew;
    if (prefs.gender != null && open.elements.gender) open.elements.gender.value = prefs.gender;
    if (Array.isArray(prefs.divisions) && open.elements.division) {
      const allowed = new Set(REFS_AGE_GROUPS);
      const wanted = new Set(
        prefs.divisions.map(String).filter((name) => allowed.has(name)),
      );
      [...open.elements.division.options].forEach((opt) => {
        opt.selected = wanted.has(opt.value);
      });
    }
  }
  if (refsForms.assigned?.elements.when && prefs.assignedWhen) {
    refsForms.assigned.elements.when.value = prefs.assignedWhen;
  }
  if (refsForms.assigned?.elements.position && prefs.assignedPosition) {
    refsForms.assigned.elements.position.value = prefs.assignedPosition;
  }
  if (refsForms.requested?.elements.position && prefs.requestedPosition) {
    refsForms.requested.elements.position.value = prefs.requestedPosition;
  }
}

function saveRefsPrefsFromForm() {
  const open = refsForms.open;
  const divisions = [...(open?.elements.division?.selectedOptions || [])].map((o) => o.value);
  setRefsPrefs({
    home: open?.elements.home?.value?.trim() || "",
    max_distance_km: open?.elements.max_distance_km?.value || "",
    position: open?.elements.position?.value || "any",
    crew: open?.elements.crew?.value || "open",
    gender: open?.elements.gender?.value || "",
    divisions,
    assignedWhen: refsForms.assigned?.elements.when?.value || "upcoming",
    assignedPosition: refsForms.assigned?.elements.position?.value || "any",
    requestedPosition: refsForms.requested?.elements.position?.value || "any",
  });
}

function selectedRefsDivisions() {
  return [...(refsForms.open?.elements.division?.selectedOptions || [])]
    .map((o) => o.value)
    .filter(Boolean);
}

function seedRefsSeasonSelects() {
  const seasons = filterData?.seasons || ["2026-27", "2025-26"];
  const defaultSeason = filterData?.defaultSeason || seasons[0];
  for (const form of Object.values(refsForms)) {
    if (!form?.elements.season_id) continue;
    const prev = form.elements.season_id.value;
    fillSelect(form.elements.season_id, seasons, { value: (s) => s, label: (s) => s });
    form.elements.season_id.value =
      prev && seasons.includes(prev) ? prev : defaultSeason;
  }
}

function formatMoney(amount) {
  if (amount == null || Number.isNaN(Number(amount))) return "—";
  return `$${Number(amount).toFixed(2)}`;
}

function renderRefsPaySummary(pay, { seasonStart } = {}) {
  const el = document.getElementById("refs-pay-summary");
  if (!el) return;
  if (!pay) {
    el.hidden = true;
    el.innerHTML = "";
    return;
  }
  el.hidden = false;
  el.innerHTML = `
    <div class="refs-pay-card">
      <span class="refs-pay-label">Earned so far</span>
      <strong>${formatMoney(pay.earned)}</strong>
    </div>
    <div class="refs-pay-card">
      <span class="refs-pay-label">Upcoming</span>
      <strong>${formatMoney(pay.future)}</strong>
    </div>
    <div class="refs-pay-card total">
      <span class="refs-pay-label">Season total${seasonStart ? ` · from ${escapeHtml(seasonStart)}` : ""}</span>
      <strong>${formatMoney(pay.total)}</strong>
    </div>
  `;
}

function refsOfficialChips(game) {
  const bits = [];
  if (game.pay != null) {
    bits.push(`<span class="chip pay">${escapeHtml(formatMoney(game.pay))}</span>`);
  }
  if (game.assignmentPosition) {
    bits.push(`<span class="chip mine">${escapeHtml(game.assignmentPosition)}</span>`);
  }
  if (game.needsReferee) bits.push(`<span class="chip need">Needs referee</span>`);
  if (game.needsLinesperson) bits.push(`<span class="chip need">Needs lines</span>`);
  if (game.unassigned != null) bits.push(`<span class="chip">${game.unassigned} open</span>`);
  if (game.myRequested) bits.push(`<span class="chip mine">You requested</span>`);
  if (game.myAssigned && !game.assignmentPosition) {
    bits.push(`<span class="chip mine">You’re assigned</span>`);
  }
  if (game.conflicts?.length) {
    bits.push(`<span class="chip warn">Conflicts with ${game.conflicts.length} of yours</span>`);
  }
  if (game.distanceKm != null) bits.push(`<span class="chip">${game.distanceKm} km</span>`);
  (game.crew || []).forEach((c) => {
    bits.push(
      `<span class="chip"><span class="pos">${escapeHtml(c.position)}</span> ${escapeHtml(c.name)}</span>`,
    );
  });
  return bits.join("") || `<span class="chip none">No crew info</span>`;
}

function refsActionButtons(game) {
  if (game.myAssigned) {
    return `<span class="muted">Assigned</span>`;
  }
  if (game.myRequested) {
    return `<button type="button" class="sheet ghost" data-refs-unrequest="${game.id}">Unrequest</button>`;
  }
  const btns = [];
  if (game.needsReferee || refsView !== "open") {
    btns.push(
      `<button type="button" class="sheet" data-refs-request="${game.id}" data-position="Referee">Request referee</button>`,
    );
  }
  if (game.needsLinesperson || refsView !== "open") {
    btns.push(
      `<button type="button" class="sheet ghost" data-refs-request="${game.id}" data-position="Linesperson">Request lines</button>`,
    );
  }
  if (!btns.length && game.open) {
    btns.push(
      `<button type="button" class="sheet" data-refs-request="${game.id}" data-position="Referee">Request referee</button>`,
      `<button type="button" class="sheet ghost" data-refs-request="${game.id}" data-position="Linesperson">Request lines</button>`,
    );
  }
  return btns.join("");
}

function renderRefsGameCard(game) {
  const when = [game.date, game.startTime && game.endTime ? `${game.startTime} – ${game.endTime}` : game.startTime]
    .filter(Boolean)
    .join(" · ");
  const league = [game.division, game.category, game.group].filter(Boolean).join(" · ");
  return `
    <article class="game">
      <div>
        <div class="meta">${escapeHtml(game.number || "")} · ${escapeHtml(when)}${
          league ? ` · ${escapeHtml(league)}` : ""
        }</div>
        <div class="title matchup">
          ${teamLinkLabel(game.away, game.awayLogoUrl, game.awayTeamId ? `#/team/${game.awayTeamId}` : null)}
          <span class="at">@</span>
          ${teamLinkLabel(game.home, game.homeLogoUrl, game.homeTeamId ? `#/team/${game.homeTeamId}` : null)}
        </div>
        <div class="venue">${escapeHtml([game.venue, game.venueAddress].filter(Boolean).join(" · "))}</div>
      </div>
      <div class="side">
        ${refsActionButtons(game)}
        <a class="sheet ghost" href="#/game/${game.id}">Recap</a>
      </div>
      <div class="officials">${refsOfficialChips(game)}</div>
    </article>
  `;
}

function renderRefsPager(pages, current) {
  if (!refsPagerEl) return;
  if (pages <= 1) {
    refsPagerEl.hidden = true;
    refsPagerEl.innerHTML = "";
    return;
  }
  const numbers = pageNumberItems(current, pages)
    .map((item) => {
      if (item === "…") return `<span class="pager-ellipsis">…</span>`;
      const active = item === current;
      return `<button type="button" data-refs-page="${item}" class="${active ? "current" : ""}" ${
        active ? "disabled" : ""
      }>${item}</button>`;
    })
    .join("");
  refsPagerEl.hidden = false;
  refsPagerEl.innerHTML = `
    <button type="button" data-refs-page="${current - 1}" ${current <= 1 ? "disabled" : ""}>Prev</button>
    ${numbers}
    <button type="button" data-refs-page="${current + 1}" ${current >= pages ? "disabled" : ""}>Next</button>
  `;
}

async function loadRefsOpenGames(seq = refsLoadSeq) {
  const f = refsForms.open.elements;
  const upcoming = f.date_mode.value === "upcoming";
  if (!f.day.value) f.day.value = todayIso();
  const data = await refsApi(
    `/games?${query({
      season_id: f.season_id.value,
      ...(upcoming ? { from_day: f.day.value } : { day: f.day.value }),
      division: selectedRefsDivisions(),
      gender: f.gender.value || undefined,
      crew: f.crew.value,
      position: f.position.value,
      my_status: "available",
      home: f.home.value.trim() || undefined,
      max_distance_km: f.max_distance_km.value || undefined,
      page: refsPage,
      page_size: 25,
    })}`,
  );
  if (!isRefsLoadCurrent(seq) || refsView !== "open") return;
  refsGamesEl.innerHTML =
    data.games.map(renderRefsGameCard).join("") ||
    `<p class="muted">No games match these filters.</p>`;
  refsStatusEl.textContent = `${data.returned} shown · ${data.total} in Spordle window (page ${data.page})`;
  renderRefsPaySummary(null);
  renderRefsPager(data.pages, data.page);
}

async function loadRefsMine(kind, seq = refsLoadSeq) {
  const form = kind === "assigned" ? refsForms.assigned : refsForms.requested;
  const season = form?.elements.season_id?.value || "2026-27";
  const position = form?.elements.position?.value || "any";
  const when = kind === "assigned" ? form?.elements.when?.value || "all" : undefined;
  if (isRefsLoadCurrent(seq)) {
    refsStatusEl.textContent =
      kind === "requested" ? "Loading your requests…" : "Loading your assignments…";
    if (kind === "assigned") renderRefsPaySummary(null);
  }
  const data = await refsApi(
    `/mine?${query({ season_id: season, kind, when, position })}`,
  );
  if (!isRefsLoadCurrent(seq) || refsView !== kind) return;
  refsGamesEl.innerHTML =
    data.games.map(renderRefsGameCard).join("") ||
    `<p class="muted">${
      kind === "requested"
        ? "No pending requests found."
        : "No ice hockey assignments for this filter."
    }</p>`;
  if (kind === "assigned") {
    const start = data.seasonStart || "";
    const whenLabel =
      data.when === "upcoming" ? "upcoming" : data.when === "completed" ? "completed" : "";
    refsStatusEl.textContent = `${data.games.length} ice hockey game${
      data.games.length === 1 ? "" : "s"
    }${whenLabel ? ` · ${whenLabel}` : ""}${start ? ` · season from ${start}` : ""}`;
    renderRefsPaySummary(data.pay, { seasonStart: data.seasonStart });
  } else {
    refsStatusEl.textContent = `${data.games.length} game${data.games.length === 1 ? "" : "s"}`;
    renderRefsPaySummary(null);
  }
  renderRefsPager(0, 1);
}

async function ensureRefsFiltersSeeded() {
  if (refsFiltersReady) return;
  try {
    await ensureScheduleFilters();
  } catch {
    /* optional */
  }
  refsSuppressEvents = true;
  try {
    seedRefsSeasonSelects();
    const open = refsForms.open;
    if (open?.elements.division) {
      fillSelect(open.elements.division, refsAgeGroupOptions(), {
        value: (d) => d.name,
        label: (d) => d.name,
      });
    }
    if (open && !open.elements.day.value) open.elements.day.value = todayIso();
    applyRefsPrefsToForm();
    refsFiltersReady = true;
  } finally {
    // Let any sync change events flush while still suppressed.
    await Promise.resolve();
    refsSuppressEvents = false;
  }
}

async function loadRefs() {
  showView("refs", "Refs");
  const seq = beginRefsLoad();
  showRefsFilterForm();
  document.querySelectorAll("[data-refs-view]").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.refsView === refsView);
  });
  refsStatusEl.textContent = "Checking Spordle session…";
  try {
    await refreshRefsMe();
    if (!isRefsLoadCurrent(seq)) return;
  } catch (err) {
    if (!isRefsLoadCurrent(seq)) return;
    refsStatusEl.textContent = `Could not check session: ${err.message}`;
    refsLoading = false;
    return;
  }
  if (!refsMe?.signedIn) {
    if (!isRefsLoadCurrent(seq)) return;
    refsStatusEl.textContent = "";
    refsGamesEl.innerHTML = "";
    renderRefsPaySummary(null);
    refsLoading = false;
    return;
  }

  try {
    await ensureRefsFiltersSeeded();
    if (!isRefsLoadCurrent(seq)) return;

    if (refsView === "open") await loadRefsOpenGames(seq);
    else if (refsView === "requested") await loadRefsMine("requested", seq);
    else await loadRefsMine("assigned", seq);
  } catch (err) {
    if (!isRefsLoadCurrent(seq)) return;
    if (err.status === 401) {
      refsMe = { signedIn: false };
      renderRefsAuth();
      refsStatusEl.textContent = "Session expired — sign in again.";
    } else {
      refsStatusEl.textContent = `Could not load: ${err.message}`;
      refsGamesEl.innerHTML = "";
    }
  } finally {
    if (isRefsLoadCurrent(seq)) refsLoading = false;
  }
}

refsAuthEl?.addEventListener("submit", async (event) => {
  if (event.target?.id !== "refs-login-form") return;
  event.preventDefault();
  const form = event.target;
  const errEl = document.getElementById("refs-login-error");
  const btn = form.querySelector('button[type="submit"]');
  errEl.hidden = true;
  btn.disabled = true;
  const username = form.elements.username.value.trim();
  const password = form.elements.password.value;
  try {
    refsMe = await refsApi("/login", { method: "POST", body: { username, password } });
    form.elements.password.value = "";
    renderRefsAuth();
    await loadRefs();
  } catch (err) {
    errEl.hidden = false;
    errEl.textContent = err.message;
    form.elements.password.value = "";
  } finally {
    btn.disabled = false;
  }
});

refsAuthEl?.addEventListener("click", async (event) => {
  if (event.target?.id === "refs-logout") {
    await refsApi("/logout", { method: "POST" });
    refsMe = { signedIn: false };
    renderRefsAuth();
    refsGamesEl.innerHTML = "";
    refsStatusEl.textContent = "";
  }
});

refsAuthEl?.addEventListener("change", async (event) => {
  if (event.target?.id !== "refs-identity") return;
  try {
    refsMe = await refsApi("/identity", {
      method: "POST",
      body: { identity_id: event.target.value },
    });
    renderRefsAuth();
    await loadRefs();
  } catch (err) {
    refsStatusEl.textContent = err.message;
  }
});

document.querySelectorAll("[data-refs-view]").forEach((btn) => {
  btn.addEventListener("click", async (event) => {
    event.preventDefault();
    const next = btn.dataset.refsView;
    if (!next || !refsForms[next]) return;
    refsView = next;
    refsPage = 1;
    document.querySelectorAll("[data-refs-view]").forEach((b) => {
      b.classList.toggle("active", b === btn);
    });
    showRefsFilterForm();
    await loadRefs();
  });
});

for (const form of Object.values(refsForms)) {
  form?.addEventListener("change", async () => {
    if (refsSuppressEvents || refsLoading || !refsMe?.signedIn) return;
    // Ignore changes on hidden forms (e.g. division fill while on assignments)
    if (form.hidden) return;
    saveRefsPrefsFromForm();
    refsPage = 1;
    await loadRefs();
  });
}

refsPagerEl?.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-refs-page]");
  if (!button || button.disabled) return;
  if (refsView !== "open") return;
  refsPage = Number(button.dataset.refsPage);
  const seq = beginRefsLoad();
  try {
    await loadRefsOpenGames(seq);
  } finally {
    if (isRefsLoadCurrent(seq)) refsLoading = false;
  }
});

refsGamesEl?.addEventListener("click", async (event) => {
  const req = event.target.closest("[data-refs-request]");
  const un = event.target.closest("[data-refs-unrequest]");
  try {
    if (req) {
      req.disabled = true;
      await refsApi(`/games/${req.dataset.refsRequest}/request`, {
        method: "POST",
        body: { position: req.dataset.position },
      });
      await loadRefs();
    } else if (un) {
      un.disabled = true;
      await refsApi(`/games/${un.dataset.refsUnrequest}/unrequest`, { method: "POST" });
      await loadRefs();
    }
  } catch (err) {
    refsStatusEl.textContent = err.message;
    if (req) req.disabled = false;
    if (un) un.disabled = false;
  }
});

(async function init() {
  // Standings/leaders/search/team/player/game are SQLite-only — never wait on Spordle.
  // Schedule tab loads Spordle filters lazily via ensureScheduleFilters().
  try {
    await route();
  } catch (err) {
    console.error(err);
    const { parts } = parseHash();
    const view = parts[0] || "favorites";
    if (view === "favorites" || view === "my-teams") {
      const status = document.getElementById("favorites-status");
      if (status) status.textContent = `Could not load Favorites: ${err.message}`;
    } else if (isStandingsFamily(view)) {
      const standingsStatus = document.getElementById("standings-status");
      if (standingsStatus) standingsStatus.textContent = `Could not load standings: ${err.message}`;
    } else {
      statusEl.textContent = `Could not load schedule: ${err.message}`;
    }
  }
})();

const views = {
  schedule: document.getElementById("view-schedule"),
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

const fields = {
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

let page = 1;
let loading = false;
let filterData = null;
let standingsSchedulesCache = [];
const sortState = new WeakMap();

function fillSelect(select, items, { value, label, blank }) {
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
    if (value !== "" && value != null) qs.set(key, value);
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

function parseHash() {
  const raw = (location.hash || "#/schedule").replace(/^#\/?/, "");
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
    el.hidden = key !== name;
  }
  pageTitle.textContent = title;
  const standingsFamily = ["standings", "team", "leaders", "player", "search", "game"];
  const topTab = standingsFamily.includes(name) ? "standings" : "schedule";
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

function sortableTable(container, columns, rows, { rowClass, onRowClick } = {}) {
  const state = sortState.get(container) || { key: columns[0]?.key, dir: "asc" };
  sortState.set(container, state);

  const sorted = [...rows].sort((a, b) => {
    const av = a[state.key];
    const bv = b[state.key];
    if (av == null && bv == null) return 0;
    if (av == null) return 1;
    if (bv == null) return -1;
    if (typeof av === "number" && typeof bv === "number") {
      return state.dir === "asc" ? av - bv : bv - av;
    }
    return state.dir === "asc"
      ? String(av).localeCompare(String(bv), undefined, { sensitivity: "base" })
      : String(bv).localeCompare(String(av), undefined, { sensitivity: "base" });
  });

  const head = columns
    .map((c) => {
      const arrow = state.key === c.key ? (state.dir === "asc" ? " ↑" : " ↓") : "";
      return `<th data-key="${escapeHtml(c.key)}" class="${c.numeric ? "num" : ""}">${escapeHtml(c.label)}${arrow}</th>`;
    })
    .join("");

  const body = sorted
    .map((row, idx) => {
      const cells = columns
        .map((c) => {
          const raw = c.render ? c.render(row) : escapeHtml(num(row[c.key]));
          return `<td class="${c.numeric ? "num" : ""}">${raw}</td>`;
        })
        .join("");
      const cls = rowClass ? rowClass(row, idx) : "";
      const clickable = onRowClick ? "clickable" : "";
      return `<tr class="${cls} ${clickable}" data-idx="${idx}">${cells}</tr>`;
    })
    .join("");

  container.innerHTML = `
    <div class="desktop-table">
      <table>
        <thead><tr>${head}</tr></thead>
        <tbody>${body || `<tr><td colspan="${columns.length}">No rows</td></tr>`}</tbody>
      </table>
    </div>
    <div class="mobile-cards">
      ${sorted
        .map((row, idx) => {
          const lines = columns
            .map((c) => {
              const raw = c.render ? c.render(row) : escapeHtml(num(row[c.key]));
              return `<div><span class="card-label">${escapeHtml(c.label)}</span> ${raw}</div>`;
            })
            .join("");
          return `<article class="stat-card ${onRowClick ? "clickable" : ""}" data-idx="${idx}">${lines}</article>`;
        })
        .join("") || `<p class="muted">No rows</p>`}
    </div>
  `;

  container.querySelectorAll("th[data-key]").forEach((th) => {
    th.addEventListener("click", () => {
      const key = th.dataset.key;
      if (state.key === key) state.dir = state.dir === "asc" ? "desc" : "asc";
      else {
        state.key = key;
        state.dir = columns.find((c) => c.key === key)?.numeric ? "desc" : "asc";
      }
      sortableTable(container, columns, rows, { rowClass, onRowClick });
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

  for (const f of [standingsForm, leadersForm, searchForm]) {
    fillSelect(f.elements.season_id, seasons, { value: (s) => s, label: (s) => s });
    f.elements.season_id.value = filterData.defaultSeason;
  }
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

function scoreline(game) {
  if (game.homeScore == null || game.awayScore == null) return "";
  return `<div class="score">${game.awayScore} – ${game.homeScore}</div>`;
}

function renderGames(payload) {
  if (!payload.games.length) {
    gamesEl.innerHTML = "";
    statusEl.textContent = "No games match these filters.";
    pagerEl.hidden = true;
    return;
  }
  statusEl.textContent = `${payload.total} game${payload.total === 1 ? "" : "s"}`;
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
            <div class="title">
              <a href="#/team/${game.awayTeamId}?season_id=${encodeURIComponent(fields.season_id.value)}">${escapeHtml(game.away)}</a>
              @
              <a href="#/team/${game.homeTeamId}?season_id=${encodeURIComponent(fields.season_id.value)}">${escapeHtml(game.home)}</a>
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

  if (payload.pages > 1) {
    pagerEl.hidden = false;
    pagerEl.innerHTML = `
      <button data-page="${page - 1}" ${page <= 1 ? "disabled" : ""}>Prev</button>
      <button class="current" disabled>${page} / ${payload.pages}</button>
      <button data-page="${page + 1}" ${page >= payload.pages ? "disabled" : ""}>Next</button>
    `;
  } else {
    pagerEl.hidden = true;
  }
}

async function loadGames() {
  if (loading) return;
  if (!fields.day.value) {
    fields.day.value = new Date().toISOString().slice(0, 10);
  }
  loading = true;
  statusEl.textContent = "Loading games…";
  try {
    const params = query({
      day: fields.day.value,
      office_id: fields.office_id.value,
      division: selectedDivisionName(),
      gender: fields.gender.value,
      schedule_id: fields.schedule_id.value,
      group_id: fields.group_id.value,
      page,
    });
    renderGames(await getJson(`/api/games?${params}`));
  } catch (err) {
    statusEl.textContent = `Could not load games: ${err.message}`;
    gamesEl.innerHTML = "";
  } finally {
    loading = false;
  }
}

async function loadStandingsSchedules({ preserveSchedule = true } = {}) {
  const season = standingsForm.elements.season_id.value;
  const division = standingsForm.elements.division?.value || "";
  const type = standingsForm.elements.type?.value || "";
  const prevSchedule = preserveSchedule ? standingsForm.elements.schedule_id.value : "";
  const prevGroup = preserveSchedule ? standingsForm.elements.group_id.value : "";
  const data = await getJson(
    `/api/standings/schedules?${query({
      season_id: season,
      division,
      type,
    })}`,
  );
  const divisions = data.divisions || [];
  const types = data.types || [];
  fillSelect(standingsForm.elements.division, divisions, {
    blank: "All ages",
    value: (d) => d,
    label: (d) => d,
  });
  if (division && divisions.includes(division)) {
    standingsForm.elements.division.value = division;
  }
  fillSelect(standingsForm.elements.type, types, {
    blank: "All types",
    value: (t) => t,
    label: (t) => t,
  });
  if (type && types.includes(type)) {
    standingsForm.elements.type.value = type;
  }

  standingsSchedulesCache = data.schedules || [];
  fillSelect(standingsForm.elements.schedule_id, standingsSchedulesCache, {
    blank: standingsSchedulesCache.length ? "Select a schedule" : "No schedules match",
    value: (s) => s.id,
    label: (s) => {
      const cat = s.category ? String(s.category) : "";
      const bits = [s.division, s.type, cat && cat !== s.name ? cat : null, s.name].filter(Boolean);
      return bits.join(" · ");
    },
  });
  if (prevSchedule && [...standingsForm.elements.schedule_id.options].some((o) => o.value === prevSchedule)) {
    standingsForm.elements.schedule_id.value = prevSchedule;
  } else {
    standingsForm.elements.schedule_id.value = "";
  }
  updateStandingsGroups();
  if (prevGroup && [...standingsForm.elements.group_id.options].some((o) => o.value === prevGroup)) {
    standingsForm.elements.group_id.value = prevGroup;
  }
}

function updateStandingsGroups() {
  const sid = Number(standingsForm.elements.schedule_id.value);
  const sched = standingsSchedulesCache.find((s) => s.id === sid);
  const groups = sched?.groups || [];
  fillSelect(standingsForm.elements.group_id, groups, {
    blank: groups.length ? "All groups" : "No groups",
    value: (g) => g.id,
    label: (g) => g.name,
  });
}

async function loadStandings() {
  const status = document.getElementById("standings-status");
  const table = document.getElementById("standings-table");
  const scheduleId = standingsForm.elements.schedule_id.value;
  if (!scheduleId) {
    status.textContent = "Pick a schedule to load standings.";
    table.innerHTML = "";
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
    sortableTable(
      table,
      [
        {
          key: "team_name",
          label: "Team",
          render: (r) =>
            `<a href="#/team/${r.team_id}?season_id=${encodeURIComponent(season)}&schedule_id=${r.schedule_id}">${escapeHtml(r.team_name)}</a>`,
        },
        { key: "gp", label: "GP", numeric: true },
        { key: "w", label: "W", numeric: true },
        { key: "l", label: "L", numeric: true },
        { key: "t", label: "T", numeric: true },
        { key: "otl", label: "OTL", numeric: true },
        { key: "pts", label: "PTS", numeric: true },
        { key: "gf", label: "GF", numeric: true },
        { key: "ga", label: "GA", numeric: true },
        { key: "gd", label: "GD", numeric: true },
        { key: "pim", label: "PIM", numeric: true },
        { key: "sportsmanship", label: "FP", numeric: true },
      ],
      data.standings,
    );
    // Default sort by points desc
    const state = sortState.get(table);
    if (state) {
      state.key = "pts";
      state.dir = "desc";
      sortableTable(
        table,
        [
          {
            key: "team_name",
            label: "Team",
            render: (r) =>
              `<a href="#/team/${r.team_id}?season_id=${encodeURIComponent(season)}&schedule_id=${r.schedule_id}">${escapeHtml(r.team_name)}</a>`,
          },
          { key: "gp", label: "GP", numeric: true },
          { key: "w", label: "W", numeric: true },
          { key: "l", label: "L", numeric: true },
          { key: "t", label: "T", numeric: true },
          { key: "otl", label: "OTL", numeric: true },
          { key: "pts", label: "PTS", numeric: true },
          { key: "gf", label: "GF", numeric: true },
          { key: "ga", label: "GA", numeric: true },
          { key: "gd", label: "GD", numeric: true },
          { key: "pim", label: "PIM", numeric: true },
          { key: "sportsmanship", label: "FP", numeric: true },
        ],
        data.standings,
      );
    }
  } catch (err) {
    status.textContent = `Could not load standings: ${err.message}`;
    table.innerHTML = "";
  }
}

async function loadTeam(teamId, params) {
  showView("team", "Team");
  const status = document.getElementById("team-status");
  status.textContent = "Loading team…";
  try {
    const qs = query({
      season_id: params.season_id || filterData?.defaultSeason || "2026-27",
      schedule_id: params.schedule_id,
    });
    const data = await getJson(`/api/teams/${teamId}/roster?${qs}`);
    const team = data.team;
    pageTitle.textContent = team.name || `Team ${teamId}`;
    status.textContent = "";
    const st = data.standings[0];
    document.getElementById("team-header").innerHTML = `
      <p class="meta">
        <a href="#/standings">← Standings</a>
        ${st ? ` · ${st.gp} GP · ${st.pts} PTS · ${st.w}-${st.l}-${st.t}` : ""}
      </p>
      <h2>${escapeHtml(team.name || "")}</h2>
    `;

    const season = data.seasonId;
    sortableTable(
      document.getElementById("team-skaters"),
      [
        { key: "number", label: "#", numeric: true },
        {
          key: "player_name",
          label: "Name",
          render: (r) =>
            `<a href="#/player/${r.participant_id}?season_id=${encodeURIComponent(season)}&schedule_id=${r.schedule_id}">${escapeHtml(titleCase(r.player_name))}</a>${
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
      ],
      data.skaters,
    );
    sortState.get(document.getElementById("team-skaters")).key = "p";
    sortState.get(document.getElementById("team-skaters")).dir = "desc";

    sortableTable(
      document.getElementById("team-goalies"),
      [
        { key: "number", label: "#", numeric: true },
        {
          key: "player_name",
          label: "Name",
          render: (r) =>
            `<a href="#/player/${r.participant_id}?season_id=${encodeURIComponent(season)}">${escapeHtml(titleCase(r.player_name))}</a>`,
        },
        { key: "goalie_gp", label: "GP", numeric: true },
        { key: "goalie_w", label: "W", numeric: true },
        { key: "goalie_l", label: "L", numeric: true },
        { key: "goalie_t", label: "T", numeric: true },
        { key: "ga", label: "GA", numeric: true },
        { key: "gaa", label: "GAA", numeric: true },
      ],
      data.goalies,
    );

    document.getElementById("team-games").innerHTML = data.games
      .map((g) => {
        const opponent =
          g.home_team_id === Number(teamId)
            ? `vs ${g.away_name || "TBD"}`
            : `@ ${g.home_name || "TBD"}`;
        return `
          <article class="game compact">
            <div>
              <div class="meta">${escapeHtml(g.date)} · ${escapeHtml(g.number || "")} · ${escapeHtml(g.schedule_name || "")}</div>
              <div class="title">${escapeHtml(opponent)}</div>
            </div>
            <div class="side">
              <div class="score">${g.away_score ?? "—"} – ${g.home_score ?? "—"}</div>
              <a class="sheet" href="#/game/${g.id}">Recap</a>
              <a class="sheet ghost" href="${escapeHtml(g.scoresheetUrl)}" target="_blank" rel="noreferrer">PDF</a>
            </div>
          </article>
        `;
      })
      .join("") || `<p class="muted">No approved games yet.</p>`;
  } catch (err) {
    status.textContent = `Could not load team: ${err.message}`;
  }
}

async function loadLeadersSchedules() {
  const season = leadersForm.elements.season_id.value;
  const division = leadersForm.elements.division?.value || "";
  const data = await getJson(
    `/api/standings/schedules?${query({ season_id: season, division })}`,
  );
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
          ? `<a href="#/team/${r.team_id}?season_id=${encodeURIComponent(season)}&schedule_id=${r.schedule_id}">${escapeHtml(r.team_name || "")}</a>`
          : "",
    };

    root.innerHTML = "";
    root.append(
      board("Points", data.points, [nameCol, teamCol, { key: "gp", label: "GP", numeric: true }, { key: "g", label: "G", numeric: true }, { key: "a", label: "A", numeric: true }, { key: "p", label: "P", numeric: true }]),
      board("Goals", data.goals, [nameCol, teamCol, { key: "g", label: "G", numeric: true }, { key: "p", label: "P", numeric: true }]),
      board("Assists", data.assists, [nameCol, teamCol, { key: "a", label: "A", numeric: true }, { key: "p", label: "P", numeric: true }]),
      board("Penalty minutes", data.pim, [nameCol, teamCol, { key: "pim", label: "PIM", numeric: true }]),
      board("Goalie wins", data.goalies, [nameCol, teamCol, { key: "goalie_gp", label: "GP", numeric: true }, { key: "goalie_w", label: "W", numeric: true }, { key: "goalie_l", label: "L", numeric: true }, { key: "gaa", label: "GAA", numeric: true }]),
      board("Goals against avg (min 2 GP)", data.gaa, [nameCol, teamCol, { key: "goalie_gp", label: "GP", numeric: true }, { key: "ga", label: "GA", numeric: true }, { key: "gaa", label: "GAA", numeric: true }]),
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
    document.getElementById("player-header").innerHTML = `
      <p class="meta"><a href="#/search">← Search</a> · Point streak: ${data.pointStreak} game${data.pointStreak === 1 ? "" : "s"}</p>
      <h2>${escapeHtml(data.player.displayName || data.player.full_name)}</h2>
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
            `<a href="#/team/${r.team_id}?season_id=${encodeURIComponent(data.seasonId)}&schedule_id=${r.schedule_id}">${escapeHtml(r.team_name || "")}</a>`,
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
          render: (r) =>
            `<a href="#/game/${r.game_id}">${escapeHtml(r.isHome ? `vs ${r.away_name}` : `@ ${r.home_name}`)}</a>`,
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
    const data = await getJson(
      `/api/search?${query({ q, season_id: searchForm.elements.season_id.value })}`,
    );
    status.textContent = `${data.players.length} players · ${data.teams.length} teams`;
    const season = searchForm.elements.season_id.value;
    root.innerHTML = `
      <div>
        <h2 class="section-title">Players</h2>
        <div class="result-list">
          ${
            data.players
              .map(
                (p) => `
              <a class="result" href="#/player/${p.participant_id}?season_id=${encodeURIComponent(season)}${p.schedule_id ? `&schedule_id=${p.schedule_id}` : ""}">
                <strong>${escapeHtml(p.displayName || titleCase(p.full_name))}</strong>
                <span>${escapeHtml(p.team_name || "")}${p.p != null ? ` · ${p.p} pts` : ""}</span>
              </a>`,
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
              <a class="result" href="#/team/${t.id}?season_id=${encodeURIComponent(season)}${t.schedule_id ? `&schedule_id=${t.schedule_id}` : ""}">
                <strong>${escapeHtml(t.name)}</strong>
                <span>${escapeHtml(t.schedule_name || "")}${t.pts != null ? ` · ${t.pts} pts` : ""}</span>
              </a>`,
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
      <h2>
        <a href="#/team/${g.away_team_id}?season_id=${encodeURIComponent(g.season_id || "2026-27")}">${escapeHtml(g.away_name || "Away")}</a>
        ${g.away_score ?? "—"} – ${g.home_score ?? "—"}
        <a href="#/team/${g.home_team_id}?season_id=${encodeURIComponent(g.season_id || "2026-27")}">${escapeHtml(g.home_name || "Home")}</a>
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
          render: (r) =>
            escapeHtml(r.team_id === g.home_team_id ? g.home_name : g.away_name),
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
        const members = data.lineups[side] || [];
        return `
          <div>
            <h3 class="section-title">${escapeHtml(title || side)}</h3>
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

async function route() {
  const { parts, params } = parseHash();
  const view = parts[0] || "schedule";

  if (view === "standings") {
    showView("standings", "Standings");
    if (params.season_id) standingsForm.elements.season_id.value = params.season_id;
    if (params.division) standingsForm.elements.division.value = params.division;
    if (params.type) standingsForm.elements.type.value = params.type;
    await loadStandingsSchedules();
    if (params.schedule_id) {
      standingsForm.elements.schedule_id.value = params.schedule_id;
      updateStandingsGroups();
      if (params.group_id) standingsForm.elements.group_id.value = params.group_id;
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

  showView("schedule", "Schedule");
  await loadGames();
}

form.addEventListener("change", async (event) => {
  page = 1;
  const name = event.target.name;
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
  page = Number(button.dataset.page);
  await loadGames();
});

standingsForm.addEventListener("change", async (event) => {
  const name = event.target.name;
  if (["season_id", "division", "type"].includes(name)) {
    await loadStandingsSchedules({ preserveSchedule: name !== "season_id" });
  } else if (name === "schedule_id") {
    updateStandingsGroups();
  }
  setHash("standings", {
    season_id: standingsForm.elements.season_id.value,
    division: standingsForm.elements.division.value,
    type: standingsForm.elements.type.value,
    schedule_id: standingsForm.elements.schedule_id.value,
    group_id: standingsForm.elements.group_id.value,
  });
});

leadersForm.addEventListener("change", async (event) => {
  if (["season_id", "division"].includes(event.target.name)) await loadLeadersSchedules();
  setHash("leaders", {
    season_id: leadersForm.elements.season_id.value,
    division: leadersForm.elements.division.value,
    schedule_id: leadersForm.elements.schedule_id.value,
  });
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

(async function init() {
  try {
    await loadFilters();
    await loadSchedules();
    await loadGroups();
    await route();
  } catch (err) {
    statusEl.textContent = `Could not load filters: ${err.message}`;
  }
})();

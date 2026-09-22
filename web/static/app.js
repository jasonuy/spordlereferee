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

let page = 1;
let loading = false;
const divisionNames = new Map();

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

async function loadFilters() {
  const data = await getJson("/api/filters");
  fields.day.value = data.defaultDate;
  fillSelect(fields.season_id, data.seasons, {
    value: (s) => s,
    label: (s) => s,
  });
  fields.season_id.value = data.defaultSeason;
  fillSelect(fields.office_id, data.offices, {
    blank: "All associations",
    value: (o) => o.id,
    label: (o) => o.name,
  });
  fillSelect(fields.division, data.divisions, {
    blank: "All divisions",
    value: (d) => d.id,
    label: (d) => d.name,
  });
  for (const d of data.divisions) {
    divisionNames.set(String(d.id), d.name);
    const opt = [...fields.division.options].find((o) => o.value === String(d.id));
    if (opt) opt.dataset.name = d.name;
  }
  fillSelect(fields.gender, data.genders, {
    blank: "All genders",
    value: (g) => g,
    label: (g) => g,
  });
  fillSelect(fields.type, data.scheduleTypes, {
    blank: "All types",
    value: (t) => t,
    label: (t) => t,
  });
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

function titleCase(name) {
  return name
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
            <div class="title">${escapeHtml(game.away)} @ ${escapeHtml(game.home)}</div>
            <div class="venue">${escapeHtml([game.venue, game.city].filter(Boolean).join(" · "))}</div>
          </div>
          <div class="side">
            ${scoreline(game)}
            <a class="sheet" href="${escapeHtml(game.scoresheetUrl)}" target="_blank" rel="noreferrer">Scoresheet PDF</a>
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

(async function init() {
  try {
    await loadFilters();
    await loadSchedules();
    await loadGroups();
    await loadGames();
  } catch (err) {
    statusEl.textContent = `Could not load filters: ${err.message}`;
  }
})();

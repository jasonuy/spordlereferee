#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.9"
# dependencies = ["fastapi>=0.115", "uvicorn>=0.32", "requests>=2.32", "pydantic>=2"]
# ///
"""Local PCAHA schedule + standings viewer.

Uses the same public Spordle Play API as https://games.pcaha.ca (API-Key
embedded in that site's frontend) and adds assigned official names, a
scoresheet PDF link, and SQLite-backed standings / player stats.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import requests
import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

WEB_DIR = Path(__file__).resolve().parent
if str(WEB_DIR) not in sys.path:
    sys.path.insert(0, str(WEB_DIR))

from db import DEFAULT_DB, connect, get_meta, rows_to_dicts  # noqa: E402
from refs import router as refs_router  # noqa: E402

PUBLIC_API = "https://api.play.spordle.com/api"
# Public key shipped in games.pcaha.ca's JS bundle — not a secret.
PUBLIC_KEY = "13f2ae2dff59c0b501294a244626f32d2873034a"
PCAHA_OFFICE_ID = 15
PCAHA_TZ = ZoneInfo("America/Vancouver")
SCORESHEET_URL = "https://pdf.play.spordle.com/game/{id}"
PAGE_SIZE = 25
SEASONS = [
    "2026-27",
    "2025-26",
    "2024-25",
    "2023-24",
    "2022-23",
    "2021-22",
    "2020-21",
    "2019-20",
    "2018-19",
]
CURRENT_SEASON = "2026-27"
GENDERS = ["Male", "Female", "Integrated"]
SCHEDULE_TYPES = [
    "League",
    "Exhibition",
    "Playoffs",
    "Tournament",
    "Championship",
    "Cup",
    "Placement",
]
_POSITION_ORDER = {
    "Referee": 0,
    "Linesperson": 1,
    "Scorekeeper": 2,
    "Timekeeper": 3,
    "Supervisor": 4,
}

STATIC = WEB_DIR / "static"
session = requests.Session()
session.headers.update(
    {
        "Authorization": f"API-Key {PUBLIC_KEY}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
        ),
    }
)

app = FastAPI(title="PCAHA schedule")
app.include_router(refs_router)


@app.middleware("http")
async def no_cache_static(request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


app.mount("/static", StaticFiles(directory=STATIC), name="static")


def db() -> sqlite3.Connection:
    return connect(Path(os.environ.get("PCAHA_DB", str(DEFAULT_DB))))


def _public_get(path: str, params: dict | None = None) -> Any:
    resp = session.get(f"{PUBLIC_API}{path}", params=params, timeout=30)
    if resp.status_code >= 400:
        raise HTTPException(resp.status_code, resp.text[:500])
    return resp.json()


def _filter_param(payload: dict) -> dict:
    return {"filter": json.dumps(payload, separators=(",", ":"))}


def office_name(office: dict) -> str:
    return office.get("name") or office.get("shortName") or str(office.get("id"))


def is_pcaha_office(office: dict) -> bool:
    if office.get("id") == PCAHA_OFFICE_ID:
        return True
    path = str(office.get("path") or "")
    return path == "467.1.2.15" or path.startswith("467.1.2.15.")


def official_row(assignment: dict) -> dict | None:
    if (assignment.get("status") or "").lower() != "confirmed":
        return None
    participant = assignment.get("participant") or {}
    name = participant.get("fullName") or " ".join(
        filter(None, [participant.get("firstName"), participant.get("lastName")])
    )
    if not name:
        return None
    return {
        "position": assignment.get("position") or "Official",
        "name": name,
        "status": assignment.get("status"),
    }


def date_iso(day: str) -> str:
    return f"{day}T00:00:00.000Z"


def games_where(
    *,
    day: str | None = None,
    from_day: str | None = None,
    office_id: int | None = None,
    division: str | None = None,
    gender: str | None = None,
    schedule_id: int | None = None,
    group_id: int | None = None,
    team_id: int | None = None,
    season_id: str | None = None,
) -> dict:
    clauses: list[dict] = []
    if from_day:
        clauses.append({"date": {"gte": date_iso(from_day)}})
    elif day:
        clauses.append({"date": date_iso(day)})
    else:
        clauses.append({"date": date_iso(date.today().isoformat())})
    if season_id:
        clauses.append({"seasonId": season_id})
    if division:
        clauses.append({"division": division})
    if gender:
        clauses.append({"gender": gender})
    if schedule_id is not None:
        clauses.append({"scheduleId": schedule_id})
    if group_id is not None:
        clauses.append({"or": [{"groupId": group_id}, {"groupId": None}]})
    if team_id is not None:
        clauses.append({"or": [{"homeTeamId": team_id}, {"awayTeamId": team_id}]})
    where: dict[str, Any] = {"and": clauses}
    if office_id is not None:
        # Association offices are not the game's own officeId. The public
        # site's `officeId` field is an effective-office scope.
        where["effectiveOffices"] = office_id
    return where


def format_local_time(iso: str | None, tz_name: str | None) -> str | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        tz = ZoneInfo(tz_name) if tz_name else PCAHA_TZ
        return dt.astimezone(tz).strftime("%-I:%M %p")
    except Exception:
        return iso


def duration_hhmm(start_iso: str | None, end_iso: str | None) -> str:
    """TeamSnap Duration (HH:MM) column — e.g. 1:15, 2:00."""
    if not start_iso or not end_iso:
        return "1:15"
    try:
        start = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
        end = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
        minutes = max(int((end - start).total_seconds() // 60), 0)
        if minutes <= 0:
            return "1:15"
        return f"{minutes // 60}:{minutes % 60:02d}"
    except Exception:
        return "1:15"


def venue_label(surface: dict | None) -> str:
    if not surface:
        return ""
    venue = surface.get("venue") or {}
    name = venue.get("name") or surface.get("name") or ""
    rink = surface.get("name")
    if name and rink and rink != name:
        return f"{name} ({rink})"
    return name or rink or ""


def venue_name(surface: dict | None) -> str:
    if not surface:
        return ""
    venue = surface.get("venue") or {}
    return venue.get("name") or surface.get("name") or ""


def location_details(surface: dict | None) -> str:
    """Rink / pad within a multi-surface venue."""
    if not surface:
        return ""
    venue = surface.get("venue") or {}
    rink = surface.get("name") or ""
    vname = venue.get("name") or ""
    if rink and vname and rink != vname:
        return f"Rink {rink}" if rink.isdigit() else rink
    return ""


def venue_address(surface: dict | None) -> str:
    if not surface:
        return ""
    venue = surface.get("venue") or {}
    parts = [
        venue.get("address"),
        venue.get("city"),
        venue.get("region"),
        venue.get("postalCode"),
    ]
    return ", ".join(p for p in parts if p)


def city_label(surface: dict | None) -> str:
    if not surface:
        return ""
    venue = surface.get("venue") or {}
    return ", ".join(filter(None, [venue.get("city"), venue.get("region")]))


def serialize_public_game(g: dict, teams: dict[int, dict]) -> dict:
    officials = [row for row in (official_row(a) for a in g.get("officials") or []) if row]
    officials.sort(key=lambda r: (_POSITION_ORDER.get(r["position"], 99), r["name"]))
    stats = {s.get("teamId"): s for s in (g.get("teamStats") or [])}
    home = teams.get(g.get("homeTeamId"), {})
    away = teams.get(g.get("awayTeamId"), {})
    surface = g.get("surface")
    return {
        "id": g["id"],
        "number": g.get("number"),
        "date": (g.get("date") or "")[:10],
        "seasonId": g.get("seasonId"),
        "startTime": format_local_time(g.get("startTime"), g.get("timezone")),
        "endTime": format_local_time(g.get("endTime"), g.get("timezone")),
        "duration": duration_hhmm(g.get("startTime"), g.get("endTime")),
        "timezone": g.get("timezone"),
        "division": g.get("division"),
        "category": g.get("category"),
        "gender": g.get("gender"),
        "status": g.get("status"),
        "isApproved": g.get("isApproved"),
        "venue": venue_label(surface),
        "venueName": venue_name(surface),
        "venueAddress": venue_address(surface),
        "locationDetails": location_details(surface),
        "city": city_label(surface),
        "schedule": (g.get("schedule") or {}).get("name"),
        "scheduleType": (g.get("schedule") or {}).get("type"),
        "scheduleId": g.get("scheduleId"),
        "group": (g.get("group") or {}).get("name"),
        "home": home.get("name") or "TBD",
        "away": away.get("name") or "TBD",
        "homeTeamId": g.get("homeTeamId"),
        "awayTeamId": g.get("awayTeamId"),
        "homeLogoUrl": home.get("logoUrl"),
        "awayLogoUrl": away.get("logoUrl"),
        "homeScore": (stats.get(g.get("homeTeamId")) or {}).get("goalFor"),
        "awayScore": (stats.get(g.get("awayTeamId")) or {}).get("goalFor"),
        "officials": officials,
        "scoresheetUrl": SCORESHEET_URL.format(id=g["id"]),
    }


def title_case_name(name: str | None) -> str:
    if not name:
        return ""
    return " ".join(part.capitalize() for part in name.split())


def _is_staff_positions(positions: str | None) -> bool:
    if not positions:
        return False
    parts = [p.strip().lower() for p in str(positions).split(",") if p.strip()]
    if not parts:
        return False
    staff = {
        "head coach",
        "assistant coach",
        "coach",
        "manager",
        "trainer",
        "safety person",
        "safety",
        "hcsp",
        "staff",
    }
    return all(p in staff or "coach" in p or "manager" in p or "trainer" in p for p in parts)


def _normalize_player_name(name: Optional[str]) -> str:
    return " ".join(str(name or "").upper().split())


_PLAYER_STAT_SUM_FIELDS = (
    "gp",
    "g",
    "a",
    "p",
    "pim",
    "ppg",
    "shg",
    "gwg",
    "affiliate_gp",
    "goalie_gp",
    "goalie_w",
    "goalie_l",
    "goalie_t",
    "ga",
)


def _participant_ids_share_a_game(
    conn: sqlite3.Connection,
    *,
    team_id: int,
    season_id: str,
    schedule_id: Optional[int],
    participant_ids: list[int],
) -> bool:
    """True if two+ of these participants appear in the same game for the team."""
    if len(participant_ids) < 2:
        return False
    placeholders = ",".join("?" * len(participant_ids))
    sql = f"""
        SELECT le.game_id
        FROM lineup_entries le
        JOIN games g ON g.id = le.game_id
        WHERE le.team_id = ?
          AND g.season_id = ?
          AND g.is_approved = 1
          AND le.participant_id IN ({placeholders})
    """
    params: list[Any] = [team_id, season_id, *participant_ids]
    if schedule_id is not None:
        sql += " AND g.schedule_id = ?"
        params.append(schedule_id)
    sql += " GROUP BY le.game_id HAVING COUNT(DISTINCT le.participant_id) > 1 LIMIT 1"
    return conn.execute(sql, params).fetchone() is not None


def merge_duplicate_name_players(
    conn: sqlite3.Connection,
    players: list[dict],
    *,
    season_id: str,
    schedule_id: Optional[int] = None,
    across_schedules: bool = False,
) -> list[dict]:
    """Combine same-name teammates split across Spordle IDs / jersey numbers.

    Spordle sometimes issues a second participant id when a player shows up in a
    different sweater. Merge those rows when the ids never share a game (so two
    kids with the same name on one roster stay separate).

    When across_schedules=True (Type=All, or a type with multiple schedules),
    also collapse the same participant (and same-name teammates) across
    schedule rows so the table doesn't list one person repeatedly.
    """
    from collections import defaultdict

    # First: same participant_id on multiple schedules → one combined row.
    if across_schedules:
        by_pid: dict[tuple, list[dict]] = defaultdict(list)
        leftovers: list[dict] = []
        for row in players:
            pid = row.get("participant_id")
            if pid is None:
                leftovers.append(row)
                continue
            by_pid[(int(pid), int(row.get("is_goalie") or 0))].append(row)
        collapsed: list[dict] = list(leftovers)
        for (pid, _goalie), rows in by_pid.items():
            if len(rows) == 1:
                collapsed.append(rows[0])
                continue
            collapsed.append(
                _sum_player_stat_rows(
                    conn,
                    rows,
                    season_id=season_id,
                    schedule_id=None,
                    across_schedules=True,
                )
            )
        players = collapsed

    groups: dict[tuple, list[dict]] = defaultdict(list)
    for row in players:
        if across_schedules:
            key = (
                row.get("team_id"),
                _normalize_player_name(row.get("player_name")),
                int(row.get("is_goalie") or 0),
            )
        else:
            key = (
                row.get("team_id"),
                row.get("schedule_id"),
                _normalize_player_name(row.get("player_name")),
                int(row.get("is_goalie") or 0),
            )
        groups[key].append(row)

    merged: list[dict] = []
    for key, rows in groups.items():
        team_id = key[0]
        if len(rows) == 1 or not team_id or not key[1 if across_schedules else 2]:
            merged.extend(rows)
            continue

        pids = [int(r["participant_id"]) for r in rows if r.get("participant_id") is not None]
        sid = None if across_schedules else (
            schedule_id if schedule_id is not None else key[1]
        )
        if _participant_ids_share_a_game(
            conn,
            team_id=int(team_id),
            season_id=season_id,
            schedule_id=int(sid) if sid is not None else None,
            participant_ids=pids,
        ):
            merged.extend(rows)
            continue

        merged.append(
            _sum_player_stat_rows(
                conn,
                rows,
                season_id=season_id,
                schedule_id=sid,
                across_schedules=across_schedules,
            )
        )

    merged.sort(
        key=lambda r: (
            int(r.get("is_goalie") or 0),
            -(r.get("p") or 0),
            -(r.get("g") or 0),
            (r.get("player_name") or "").lower(),
        )
    )
    return merged


def _sum_player_stat_rows(
    conn: sqlite3.Connection,
    rows: list[dict],
    *,
    season_id: str,
    schedule_id: Optional[int],
    across_schedules: bool,
) -> dict:
    primary = max(rows, key=lambda r: (r.get("gp") or 0, r.get("p") or 0, r.get("g") or 0))
    out = dict(primary)
    for field in _PLAYER_STAT_SUM_FIELDS:
        out[field] = sum((r.get(field) or 0) for r in rows)

    pids = [int(r["participant_id"]) for r in rows if r.get("participant_id") is not None]
    team_id = primary.get("team_id")
    if pids and team_id is not None:
        placeholders = ",".join("?" * len(pids))
        gp_sql = f"""
            SELECT COUNT(DISTINCT le.game_id)
            FROM lineup_entries le
            JOIN games g ON g.id = le.game_id
            WHERE le.team_id = ? AND g.season_id = ? AND g.is_approved = 1
              AND le.participant_id IN ({placeholders})
        """
        gp_params: list[Any] = [int(team_id), season_id, *pids]
        schedule_ids = sorted(
            {
                int(r["schedule_id"])
                for r in rows
                if r.get("schedule_id") is not None
            }
        )
        if schedule_id is not None:
            gp_sql += " AND g.schedule_id = ?"
            gp_params.append(int(schedule_id))
        elif across_schedules and schedule_ids:
            gp_sql += f" AND g.schedule_id IN ({','.join('?' * len(schedule_ids))})"
            gp_params.extend(schedule_ids)
        out["gp"] = int(conn.execute(gp_sql, gp_params).fetchone()[0] or out["gp"])

        numbers_sql = f"""
            SELECT DISTINCT le.number
            FROM lineup_entries le
            JOIN games g ON g.id = le.game_id
            WHERE le.team_id = ? AND g.season_id = ? AND g.is_approved = 1
              AND le.participant_id IN ({placeholders})
              AND le.number IS NOT NULL
        """
        num_params: list[Any] = [int(team_id), season_id, *pids]
        if schedule_id is not None:
            numbers_sql += " AND g.schedule_id = ?"
            num_params.append(int(schedule_id))
        elif across_schedules and schedule_ids:
            numbers_sql += f" AND g.schedule_id IN ({','.join('?' * len(schedule_ids))})"
            num_params.extend(schedule_ids)
        numbers_sql += " ORDER BY le.number ASC"
        nums = [r[0] for r in conn.execute(numbers_sql, num_params).fetchall()]
    else:
        nums = sorted({r.get("number") for r in rows if r.get("number") is not None})

    if not nums:
        nums = sorted({r.get("number") for r in rows if r.get("number") is not None})
    out["number"] = nums[0] if len(nums) == 1 else None
    out["numbers"] = nums
    out["number_display"] = "/".join(str(n) for n in nums) if nums else ""

    pos_parts: list[str] = []
    seen_pos: set[str] = set()
    for r in rows:
        for part in str(r.get("positions") or "").split(","):
            piece = part.strip()
            pkey = piece.upper()
            if piece and pkey not in seen_pos:
                seen_pos.add(pkey)
                pos_parts.append(piece)
    if pos_parts:
        out["positions"] = ",".join(pos_parts)
    out["merged_participant_ids"] = pids
    out["is_affiliate"] = 1 if any(r.get("is_affiliate") for r in rows) else 0
    out["p"] = int(out.get("g") or 0) + int(out.get("a") or 0)
    if out.get("is_goalie") and out.get("goalie_gp"):
        out["gaa"] = round(float(out.get("ga") or 0) / float(out["goalie_gp"]), 2)
    if across_schedules:
        out["schedule_id"] = None
        types = sorted({str(r.get("schedule_type") or "") for r in rows if r.get("schedule_type")})
        names = sorted({str(r.get("schedule_name") or "") for r in rows if r.get("schedule_name")})
        if types:
            out["schedule_types"] = types
        if names:
            out["schedule_names"] = names
    return out


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/filters")
def filters() -> dict:
    offices = _public_get("/offices", _filter_param({"where": {}}))
    divisions = _public_get("/divisions", _filter_param({"order": "order"}))
    associations = [
        {"id": o["id"], "name": office_name(o), "type": o.get("type")}
        for o in offices
        if is_pcaha_office(o) and o.get("type") in {"Association", "District"}
    ]
    associations.sort(key=lambda o: (o["type"] != "District", o["name"]))
    return {
        "seasons": SEASONS,
        "genders": GENDERS,
        "scheduleTypes": SCHEDULE_TYPES,
        "offices": associations,
        "divisions": [{"id": d["id"], "name": d["name"]} for d in divisions],
        "defaultSeason": CURRENT_SEASON,
        "defaultDate": date.today().isoformat(),
    }


@app.get("/api/schedules")
def schedules(
    season_id: str = Query("2026-27"),
    office_id: Optional[int] = None,
    division_id: Optional[str] = None,
    gender: Optional[str] = None,
    type: Optional[str] = None,
) -> list:
    where: dict[str, Any] = {"seasonId": season_id}
    if office_id is not None:
        where["officeId"] = office_id
    if division_id:
        where["category.divisionId"] = division_id
    if gender:
        where["gender"] = gender
    if type:
        where["type"] = type
    rows = _public_get(
        "/schedules",
        _filter_param(
            {
                "where": where,
                "order": ["startDate ASC", "category.order ASC", "name ASC"],
                "include": "category",
            }
        ),
    )
    return [
        {
            "id": s["id"],
            "name": s.get("name"),
            "type": s.get("type"),
            "division": s.get("division"),
            "gender": s.get("gender"),
            "category": s.get("category"),
            "officeId": s.get("officeId"),
        }
        for s in rows
    ]


@app.get("/api/groups")
def groups(office_id: Optional[int] = None, type: Optional[str] = None) -> list:
    if office_id is None:
        return []
    where: dict[str, Any] = {"officeId": office_id}
    if type:
        where["type"] = type
    rows = _public_get("/groups", _filter_param({"where": where}))
    return [{"id": g["id"], "name": g.get("name"), "type": g.get("type")} for g in groups_unique(rows)]


def groups_unique(rows: list) -> list:
    seen = set()
    out = []
    for g in rows:
        if g["id"] in seen:
            continue
        seen.add(g["id"])
        out.append(g)
    return out


@app.get("/api/games")
def games(
    day: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    from_day: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    office_id: Optional[int] = None,
    division: Optional[str] = None,
    gender: Optional[str] = None,
    schedule_id: Optional[int] = None,
    group_id: Optional[int] = None,
    team_id: Optional[int] = None,
    season_id: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(PAGE_SIZE, ge=1, le=100),
) -> dict:
    where = games_where(
        day=None if from_day else day,
        from_day=from_day,
        office_id=office_id,
        division=division,
        gender=gender,
        schedule_id=schedule_id,
        group_id=group_id,
        team_id=team_id,
        season_id=season_id,
    )
    total = _public_get("/games/count", {"where": json.dumps(where, separators=(",", ":"))}).get("count", 0)
    skip = (page - 1) * page_size
    raw = _public_get(
        "/games",
        _filter_param(
            {
                "where": where,
                "include": ["surface", "schedule", "group", "teamStats", "officials"],
                "order": ["startTime ASC", "id DESC"],
                "limit": page_size,
                "skip": skip,
            }
        ),
    )
    team_ids = {t for g in raw for t in (g.get("homeTeamId"), g.get("awayTeamId")) if t}
    teams: dict[int, dict] = {}
    if team_ids:
        for team in _public_get(
            "/teams",
            _filter_param({"where": {"id": {"inq": sorted(team_ids)}}, "scope": "Tenant"}),
        ):
            teams[team["id"]] = team

    items = [serialize_public_game(g, teams) for g in raw]

    return {
        "total": total,
        "page": page,
        "pageSize": page_size,
        "pages": max(1, (total + page_size - 1) // page_size) if total else 1,
        "fromDay": from_day,
        "day": None if from_day else (day or date.today().isoformat()),
        "games": items,
    }


@app.get("/api/stats/meta")
def stats_meta() -> dict:
    conn = db()
    try:
        games = conn.execute("SELECT COUNT(*) AS c FROM games WHERE is_approved=1").fetchone()["c"]
        return {
            "db": str(DEFAULT_DB),
            "approvedGames": games,
            "lastIngestAt": get_meta(conn, "last_ingest_at"),
            "rollupsRebuiltAt": get_meta(conn, "rollups_rebuilt_at"),
            "lastIngestCount": get_meta(conn, "last_ingest_count"),
        }
    finally:
        conn.close()


def _natural_group_key(name: str | None) -> list:
    """Sort Tier/Flight/Group numbers naturally; colours alphabetically."""
    text = name or ""
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", text)]


@app.get("/api/standings")
def standings(
    season_id: str = Query("2026-27"),
    schedule_id: int = Query(...),
    group_id: Optional[int] = None,
) -> dict:
    conn = db()
    try:
        schedule = conn.execute(
            "SELECT * FROM schedules WHERE id=?", (schedule_id,)
        ).fetchone()
        group_key = group_id if group_id is not None else None
        if group_key is None:
            rows = conn.execute(
                """
                SELECT st.*, t.logo_url AS logoUrl, gr.name AS groupName
                FROM standings st
                LEFT JOIN teams t ON t.id = st.team_id
                LEFT JOIN groups gr ON gr.id = st.group_id
                WHERE st.season_id=? AND st.schedule_id=?
                ORDER BY st.pts DESC, st.gd DESC, st.gf DESC, st.team_name ASC
                """,
                (season_id, schedule_id),
            ).fetchall()
            # If multiple groups exist, prefer requiring group_id — still return combined
            groups = sorted({r["group_id"] for r in rows})
        else:
            rows = conn.execute(
                """
                SELECT st.*, t.logo_url AS logoUrl, gr.name AS groupName
                FROM standings st
                LEFT JOIN teams t ON t.id = st.team_id
                LEFT JOIN groups gr ON gr.id = st.group_id
                WHERE st.season_id=? AND st.schedule_id=? AND st.group_id=?
                ORDER BY st.pts DESC, st.gd DESC, st.gf DESC, st.team_name ASC
                """,
                (season_id, schedule_id, group_key),
            ).fetchall()
            groups = [group_key]

        items = [dict(r) for r in rows]
        if group_key is None and len(groups) > 1:
            # Keep standings ordered by group (Tier 1, Tier 2… / Blue, Gold…), then points.
            items.sort(
                key=lambda r: (
                    _natural_group_key(r.get("groupName")),
                    -(r.get("pts") or 0),
                    -(r.get("gd") or 0),
                    -(r.get("gf") or 0),
                    (r.get("team_name") or "").lower(),
                )
            )

        return {
            "seasonId": season_id,
            "scheduleId": schedule_id,
            "groupId": group_id,
            "schedule": dict(schedule) if schedule else None,
            "standings": items,
        }
    finally:
        conn.close()


@app.get("/api/teams/{team_id}/roster")
def team_roster(
    team_id: int,
    season_id: str = Query("2026-27"),
    schedule_id: Optional[int] = None,
    type: Optional[str] = None,
    scope: Optional[str] = None,
) -> dict:
    conn = db()
    try:
        team = conn.execute("SELECT * FROM teams WHERE id=?", (team_id,)).fetchone()
        if not team:
            # Still allow if we have stats rows
            team = {"id": team_id, "name": f"Team {team_id}", "short_name": None, "office_id": None}

        # Schedules this team appears in for the season (standings, player stats, or games).
        schedule_rows = conn.execute(
            """
            SELECT DISTINCT s.id, s.name, s.type, s.division, s.gender, s.category
            FROM schedules s
            WHERE s.id IN (
              SELECT schedule_id FROM standings
                WHERE season_id=? AND team_id=? AND schedule_id IS NOT NULL
              UNION
              SELECT schedule_id FROM player_stats
                WHERE season_id=? AND team_id=? AND schedule_id IS NOT NULL
              UNION
              SELECT schedule_id FROM games
                WHERE season_id=? AND is_approved=1 AND schedule_id IS NOT NULL
                  AND (home_team_id=? OR away_team_id=?)
            )
            ORDER BY
              CASE s.type
                WHEN 'League' THEN 1
                WHEN 'Placement' THEN 2
                WHEN 'Playoffs' THEN 3
                WHEN 'Exhibition' THEN 4
                WHEN 'Tournament' THEN 5
                ELSE 9
              END,
              s.name ASC
            """,
            (season_id, team_id, season_id, team_id, season_id, team_id, team_id),
        ).fetchall()
        schedules = [dict(r) for r in schedule_rows]
        types = []
        for s in schedules:
            t = s.get("type")
            if t and t not in types:
                types.append(t)

        # Resolve active schedule filter from schedule_id and/or type.
        # Default to the team's League schedule (else first available) so roster
        # points match league standings; callers can pass scope=all to compare everything.
        scope = (scope or "").strip().lower() if scope else ""
        active_type = type or None
        active_schedule_id = schedule_id
        if scope == "all":
            active_type = None
            active_schedule_id = None
        elif active_schedule_id is not None:
            match = next((s for s in schedules if s["id"] == active_schedule_id), None)
            if match and not active_type:
                active_type = match.get("type")
        elif active_type:
            typed = [s for s in schedules if s.get("type") == active_type]
            if len(typed) == 1:
                active_schedule_id = typed[0]["id"]
        elif schedules:
            preferred = next((s for s in schedules if s.get("type") == "League"), None)
            pick = preferred or schedules[0]
            active_schedule_id = pick["id"]
            active_type = pick.get("type")

        schedule_ids: list[int] | None = None
        if active_schedule_id is not None:
            schedule_ids = [active_schedule_id]
        elif active_type:
            schedule_ids = [s["id"] for s in schedules if s.get("type") == active_type]

        active_schedule = next(
            (s for s in schedules if s["id"] == active_schedule_id), None
        ) if active_schedule_id is not None else None

        sql = """
            SELECT * FROM player_stats
            WHERE season_id=? AND team_id=?
        """
        params: list[Any] = [season_id, team_id]
        if schedule_ids is not None:
            placeholders = ",".join("?" * len(schedule_ids))
            sql += f" AND schedule_id IN ({placeholders})"
            params.extend(schedule_ids)
        sql += " ORDER BY is_goalie ASC, p DESC, g DESC, player_name ASC"
        players = rows_to_dicts(conn.execute(sql, params).fetchall())
        # When aggregating across schedules of one type (or All), merge same player.
        across = active_schedule_id is None and len(players) > 1
        players = merge_duplicate_name_players(
            conn,
            players,
            season_id=season_id,
            schedule_id=active_schedule_id,
            across_schedules=across,
        )

        skaters = [
            p
            for p in players
            if not p.get("is_goalie")
            and not _is_staff_positions(p.get("positions"))
        ]
        goalies = [p for p in players if p.get("is_goalie")]

        # Recent games for this team
        game_sql = """
            SELECT g.*,
                   ht.name AS home_name, at.name AS away_name,
                   ht.logo_url AS homeLogoUrl, at.logo_url AS awayLogoUrl,
                   s.name AS schedule_name, s.type AS schedule_type,
                   gr.name AS group_name
            FROM games g
            LEFT JOIN teams ht ON ht.id = g.home_team_id
            LEFT JOIN teams at ON at.id = g.away_team_id
            LEFT JOIN schedules s ON s.id = g.schedule_id
            LEFT JOIN groups gr ON gr.id = g.group_id
            WHERE g.season_id=? AND (g.home_team_id=? OR g.away_team_id=?) AND g.is_approved=1
        """
        game_params: list[Any] = [season_id, team_id, team_id]
        if schedule_ids is not None:
            placeholders = ",".join("?" * len(schedule_ids))
            game_sql += f" AND g.schedule_id IN ({placeholders})"
            game_params.extend(schedule_ids)
        game_sql += " ORDER BY g.date DESC, g.id DESC LIMIT 40"
        games_rows = []
        for g in conn.execute(game_sql, game_params).fetchall():
            games_rows.append(
                {
                    **dict(g),
                    "scoresheetUrl": SCORESHEET_URL.format(id=g["id"]),
                }
            )

        # Standings snippet for this team
        standings_sql = """
            SELECT st.*, s.name AS schedule_name, s.type AS schedule_type
            FROM standings st
            LEFT JOIN schedules s ON s.id = st.schedule_id
            WHERE st.season_id=? AND st.team_id=?
        """
        st_params: list[Any] = [season_id, team_id]
        if schedule_ids is not None:
            placeholders = ",".join("?" * len(schedule_ids))
            standings_sql += f" AND st.schedule_id IN ({placeholders})"
            st_params.extend(schedule_ids)
        standings_sql += """
            ORDER BY
              CASE s.type
                WHEN 'League' THEN 1
                WHEN 'Placement' THEN 2
                WHEN 'Playoffs' THEN 3
                ELSE 9
              END,
              st.pts DESC
        """
        standings_rows = rows_to_dicts(conn.execute(standings_sql, st_params).fetchall())

        return {
            "team": dict(team) if not isinstance(team, dict) else team,
            "seasonId": season_id,
            "scheduleId": active_schedule_id,
            "type": active_type,
            "scope": "all" if scope == "all" else None,
            "schedule": active_schedule,
            "schedules": schedules,
            "types": types,
            "skaters": skaters,
            "goalies": goalies,
            "games": games_rows,
            "standings": standings_rows,
        }
    finally:
        conn.close()


@app.get("/api/leaders")
def leaders(
    season_id: str = Query("2026-27"),
    schedule_id: Optional[int] = None,
    group_id: Optional[int] = None,
    limit: int = Query(25, ge=1, le=100),
) -> dict:
    conn = db()
    try:
        where = ["ps.season_id=?"]
        params: list[Any] = [season_id]
        if schedule_id is not None:
            where.append("ps.schedule_id=?")
            params.append(schedule_id)
        if group_id is not None:
            where.append(
                """EXISTS (
                    SELECT 1 FROM standings st
                    WHERE st.season_id=ps.season_id AND st.schedule_id=ps.schedule_id
                      AND st.team_id=ps.team_id AND st.group_id=?
                )"""
            )
            params.append(group_id)
        where_sql = " AND ".join(where)

        def top(metric: str, goalie: bool = False, extra: str = "") -> list:
            gfilter = "ps.is_goalie=1" if goalie else "ps.is_goalie=0"
            # Fetch a wider pool so same-name merges can still fill the board.
            fetch_n = min(max(limit * 3, limit), 200)
            q = f"""
                SELECT ps.*, t.name AS team_name, t.logo_url AS logoUrl,
                       s.name AS schedule_name, s.type AS schedule_type
                FROM player_stats ps
                LEFT JOIN teams t ON t.id = ps.team_id
                LEFT JOIN schedules s ON s.id = ps.schedule_id
                WHERE {where_sql} AND {gfilter} {extra}
                ORDER BY {metric} DESC, ps.player_name ASC
                LIMIT ?
            """
            rows = rows_to_dicts(conn.execute(q, [*params, fetch_n]).fetchall())
            rows = merge_duplicate_name_players(
                conn, rows, season_id=season_id, schedule_id=schedule_id
            )
            reverse = not metric.endswith("gaa")
            rows.sort(
                key=lambda r: (
                    r.get(metric.split(".")[-1]) is None,
                    -(r.get(metric.split(".")[-1]) or 0)
                    if reverse
                    else (r.get(metric.split(".")[-1]) or 0),
                    str(r.get("player_name") or ""),
                )
            )
            return rows[:limit]

        return {
            "seasonId": season_id,
            "scheduleId": schedule_id,
            "groupId": group_id,
            "points": top("ps.p"),
            "goals": top("ps.g"),
            "assists": top("ps.a"),
            "pim": top("ps.pim"),
            "goalies": top("ps.goalie_w", goalie=True, extra="AND ps.goalie_gp > 0"),
            "gaa": top("ps.gaa", goalie=True, extra="AND ps.goalie_gp >= 2 AND ps.gaa IS NOT NULL"),
        }
    finally:
        conn.close()


@app.get("/api/players/{participant_id}")
def player_page(
    participant_id: int,
    season_id: str = Query("2026-27"),
    schedule_id: Optional[int] = None,
) -> dict:
    conn = db()
    try:
        player = conn.execute(
            "SELECT * FROM players WHERE participant_id=?", (participant_id,)
        ).fetchone()
        if not player:
            raise HTTPException(404, "Player not found")

        stats_sql = "SELECT ps.*, t.name AS team_name, t.logo_url AS logoUrl FROM player_stats ps LEFT JOIN teams t ON t.id=ps.team_id WHERE ps.season_id=? AND ps.participant_id=?"
        stats_params: list[Any] = [season_id, participant_id]
        if schedule_id is not None:
            stats_sql += " AND ps.schedule_id=?"
            stats_params.append(schedule_id)
        stats = rows_to_dicts(conn.execute(stats_sql, stats_params).fetchall())

        log_sql = """
            SELECT g.id AS game_id, g.number, g.date, g.home_team_id, g.away_team_id,
                   g.home_score, g.away_score, g.schedule_id,
                   le.team_id, le.number AS sweater, le.positions, le.is_affiliate,
                   ht.name AS home_name, at.name AS away_name,
                   ht.logo_url AS homeLogoUrl, at.logo_url AS awayLogoUrl,
                   s.name AS schedule_name,
                   (SELECT COUNT(*) FROM goals go WHERE go.game_id=g.id AND go.participant_id=? AND go.team_id=le.team_id) AS g,
                   (SELECT COUNT(*) FROM assists a JOIN goals go ON go.id=a.goal_id
                      WHERE go.game_id=g.id AND a.participant_id=? AND go.team_id=le.team_id) AS a,
                   (SELECT COALESCE(SUM(pe.pim),0) FROM penalties pe
                      WHERE pe.game_id=g.id AND pe.participant_id=? AND pe.team_id=le.team_id) AS pim
            FROM lineup_entries le
            JOIN games g ON g.id = le.game_id
            LEFT JOIN teams ht ON ht.id = g.home_team_id
            LEFT JOIN teams at ON at.id = g.away_team_id
            LEFT JOIN schedules s ON s.id = g.schedule_id
            WHERE le.participant_id=? AND g.season_id=? AND g.is_approved=1
        """
        log_params: list[Any] = [
            participant_id,
            participant_id,
            participant_id,
            participant_id,
            season_id,
        ]
        if schedule_id is not None:
            log_sql += " AND g.schedule_id=?"
            log_params.append(schedule_id)
        log_sql += " ORDER BY g.date DESC, g.id DESC"
        game_log = []
        for row in conn.execute(log_sql, log_params).fetchall():
            item = dict(row)
            item["p"] = (item.get("g") or 0) + (item.get("a") or 0)
            item["scoresheetUrl"] = SCORESHEET_URL.format(id=item["game_id"])
            item["isHome"] = item["team_id"] == item["home_team_id"]
            game_log.append(item)

        home = [g for g in game_log if g["isHome"]]
        away = [g for g in game_log if not g["isHome"]]

        def split_sum(rows: list[dict]) -> dict:
            return {
                "gp": len(rows),
                "g": sum(r.get("g") or 0 for r in rows),
                "a": sum(r.get("a") or 0 for r in rows),
                "p": sum(r.get("p") or 0 for r in rows),
                "pim": sum(r.get("pim") or 0 for r in rows),
            }

        # Point streak: consecutive games with a point from most recent
        streak = 0
        for g in game_log:
            if (g.get("p") or 0) > 0:
                streak += 1
            else:
                break

        return {
            "player": {
                **dict(player),
                "displayName": title_case_name(player["full_name"]),
            },
            "seasonId": season_id,
            "scheduleId": schedule_id,
            "stats": stats,
            "gameLog": game_log,
            "splits": {"home": split_sum(home), "away": split_sum(away)},
            "pointStreak": streak,
        }
    finally:
        conn.close()


@app.get("/api/search")
def search(
    q: str = Query(..., min_length=2),
    season_id: str = Query("2026-27"),
    limit: int = Query(20, ge=1, le=50),
) -> dict:
    conn = db()
    try:
        like = f"%{q.strip()}%"
        players = rows_to_dicts(
            conn.execute(
                """
                SELECT DISTINCT p.participant_id, p.full_name, ps.team_id, t.name AS team_name,
                       t.logo_url AS logoUrl,
                       ps.schedule_id, s.name AS schedule_name, ps.number, ps.p, ps.gp
                FROM players p
                LEFT JOIN player_stats ps ON ps.participant_id = p.participant_id AND ps.season_id=?
                LEFT JOIN teams t ON t.id = ps.team_id
                LEFT JOIN schedules s ON s.id = ps.schedule_id
                WHERE p.full_name LIKE ? COLLATE NOCASE
                ORDER BY CASE WHEN ps.p IS NULL THEN 1 ELSE 0 END, ps.p DESC, p.full_name ASC
                LIMIT ?
                """,
                (season_id, like, limit),
            ).fetchall()
        )
        teams = rows_to_dicts(
            conn.execute(
                """
                SELECT DISTINCT t.id, t.name, t.logo_url AS logoUrl, st.schedule_id, s.name AS schedule_name,
                       st.pts, st.gp, st.group_id
                FROM teams t
                LEFT JOIN standings st ON st.team_id = t.id AND st.season_id=?
                LEFT JOIN schedules s ON s.id = st.schedule_id
                WHERE t.name LIKE ? COLLATE NOCASE
                ORDER BY CASE WHEN st.pts IS NULL THEN 1 ELSE 0 END, st.pts DESC, t.name ASC
                LIMIT ?
                """,
                (season_id, like, limit),
            ).fetchall()
        )
        for p in players:
            p["displayName"] = title_case_name(p.get("full_name"))
        return {"q": q, "seasonId": season_id, "players": players, "teams": teams}
    finally:
        conn.close()


@app.get("/api/games/{game_id}/recap")
def game_recap(game_id: int) -> dict:
    conn = db()
    try:
        game = conn.execute(
            """
            SELECT g.*, ht.name AS home_name, at.name AS away_name,
                   ht.logo_url AS homeLogoUrl, at.logo_url AS awayLogoUrl,
                   s.name AS schedule_name, s.type AS schedule_type,
                   gr.name AS group_name
            FROM games g
            LEFT JOIN teams ht ON ht.id = g.home_team_id
            LEFT JOIN teams at ON at.id = g.away_team_id
            LEFT JOIN schedules s ON s.id = g.schedule_id
            LEFT JOIN groups gr ON gr.id = g.group_id
            WHERE g.id=?
            """,
            (game_id,),
        ).fetchone()
        if not game:
            raise HTTPException(404, "Game not found in stats DB (ingest may be pending)")

        goals = rows_to_dicts(
            conn.execute(
                """
                SELECT go.*, p.full_name AS scorer_name
                FROM goals go
                LEFT JOIN players p ON p.participant_id = go.participant_id
                WHERE go.game_id=?
                ORDER BY CAST(go.period AS INTEGER), go.minutes DESC, go.seconds DESC
                """,
                (game_id,),
            ).fetchall()
        )
        for goal in goals:
            assists = rows_to_dicts(
                conn.execute(
                    """
                    SELECT a.participant_id, a.ordinal, p.full_name
                    FROM assists a
                    LEFT JOIN players p ON p.participant_id = a.participant_id
                    WHERE a.goal_id=?
                    ORDER BY a.ordinal
                    """,
                    (goal["id"],),
                ).fetchall()
            )
            goal["assists"] = [
                {**a, "displayName": title_case_name(a.get("full_name"))} for a in assists
            ]
            goal["scorerDisplay"] = title_case_name(goal.get("scorer_name"))

        penalties = rows_to_dicts(
            conn.execute(
                """
                SELECT pe.*, p.full_name
                FROM penalties pe
                LEFT JOIN players p ON p.participant_id = pe.participant_id
                WHERE pe.game_id=?
                ORDER BY CAST(pe.period AS INTEGER), pe.minutes DESC, pe.seconds DESC
                """,
                (game_id,),
            ).fetchall()
        )
        for pe in penalties:
            pe["displayName"] = title_case_name(pe.get("full_name"))

        lineups = {}
        for side in ("home", "away"):
            tid = game["home_team_id"] if side == "home" else game["away_team_id"]
            members = rows_to_dicts(
                conn.execute(
                    """
                    SELECT le.*, p.full_name
                    FROM lineup_entries le
                    LEFT JOIN players p ON p.participant_id = le.participant_id
                    WHERE le.game_id=? AND le.team_id=?
                    ORDER BY CASE WHEN le.number IS NULL THEN 1 ELSE 0 END, le.number ASC, p.full_name ASC
                    """,
                    (game_id, tid),
                ).fetchall()
            )
            for m in members:
                m["displayName"] = title_case_name(m.get("full_name"))
            lineups[side] = members

        team_stats = rows_to_dicts(
            conn.execute(
                "SELECT * FROM team_game_stats WHERE game_id=?", (game_id,)
            ).fetchall()
        )

        return {
            "game": {
                **dict(game),
                "scoresheetUrl": SCORESHEET_URL.format(id=game_id),
            },
            "teamStats": team_stats,
            "goals": goals,
            "penalties": penalties,
            "lineups": lineups,
            "stars": [],  # scoresheet stars rarely populated; reserved for paywall-ready UI
        }
    finally:
        conn.close()


def _age_from_schedule_name(name: Optional[str]) -> Optional[str]:
    """Pull U11/U13/… from schedule titles when Spordle labels division as Other."""
    if not name:
        return None
    # Match U11, U18A, U11 A, etc. (digit run after U, not requiring a word boundary after).
    m = re.search(r"(?<![A-Za-z0-9])U(\d{1,2})(?!\d)", str(name), flags=re.IGNORECASE)
    if not m:
        return None
    return f"U{int(m.group(1))}"


def _effective_division(stored: Optional[str], name: Optional[str]) -> Optional[str]:
    raw = (stored or "").strip()
    if raw and raw.lower() not in {"other", "unknown", "n/a", "na"}:
        # Prefer canonical U-age when the stored value is a category letter etc.
        if re.match(r"^U\d{1,2}$", raw, flags=re.IGNORECASE):
            return f"U{int(raw[1:])}"
        inferred = _age_from_schedule_name(name)
        return inferred or raw
    return _age_from_schedule_name(name) or (raw or None)


@app.get("/api/standings/schedules")
def standings_schedules(
    season_id: str = Query("2026-27"),
    division: Optional[str] = None,
    type: Optional[str] = None,
    gender: Optional[str] = None,
) -> dict:
    """Schedules that have standings rows (for the league picker)."""
    conn = db()
    try:
        seasons = [
            r[0]
            for r in conn.execute(
                """
                SELECT DISTINCT season_id FROM standings
                WHERE season_id IS NOT NULL AND season_id != ''
                ORDER BY season_id DESC
                """
            ).fetchall()
        ]
        if not season_id and seasons:
            season_id = seasons[0]
        if CURRENT_SEASON in seasons:
            default_season = CURRENT_SEASON
        else:
            default_season = seasons[0] if seasons else (season_id or CURRENT_SEASON)
        # Prefer current season when the client sends the hardcoded Query default
        # but has not explicitly chosen a prior year yet.
        if season_id == CURRENT_SEASON or season_id not in seasons:
            if CURRENT_SEASON in seasons:
                season_id = CURRENT_SEASON
            elif seasons:
                season_id = seasons[0]

        sql = """
            SELECT DISTINCT s.id, s.name, s.type, s.division, s.gender, s.category, s.office_id AS officeId,
                   st.group_id,
                   gr.name AS group_name
            FROM standings st
            JOIN schedules s ON s.id = st.schedule_id
            LEFT JOIN groups gr ON gr.id = st.group_id
            WHERE st.season_id=?
        """
        params: list[Any] = [season_id]
        if type:
            sql += " AND s.type = ?"
            params.append(type)
        if gender:
            sql += " AND s.gender = ?"
            params.append(gender)
        sql += " ORDER BY s.division ASC, s.type ASC, s.name ASC, gr.name ASC"
        rows = conn.execute(sql, params).fetchall()
        by_id: dict[int, dict] = {}
        for r in rows:
            sid = r["id"]
            eff_div = _effective_division(r["division"], r["name"])
            if division and eff_div != division:
                continue
            if sid not in by_id:
                by_id[sid] = {
                    "id": sid,
                    "name": r["name"],
                    "type": r["type"],
                    "division": eff_div,
                    "gender": r["gender"],
                    "category": r["category"],
                    "officeId": r["officeId"],
                    "groups": [],
                }
            gid = r["group_id"]
            if gid and not any(g["id"] == gid for g in by_id[sid]["groups"]):
                by_id[sid]["groups"].append({"id": gid, "name": r["group_name"] or str(gid)})

        for sched in by_id.values():
            sched["groups"].sort(key=lambda g: _natural_group_key(g.get("name")))

        # Facets from all standings schedules this season (unfiltered), for the dropdowns
        facet_rows = conn.execute(
            """
            SELECT DISTINCT s.name, s.division, s.type, s.gender
            FROM standings st
            JOIN schedules s ON s.id = st.schedule_id
            WHERE st.season_id=?
            """,
            (season_id,),
        ).fetchall()
        all_divisions = sorted(
            {
                d
                for r in facet_rows
                if (d := _effective_division(r["division"], r["name"]))
            },
            key=_division_sort_key,
        )
        all_types = sorted({r["type"] for r in facet_rows if r["type"]})
        all_genders = sorted({r["gender"] for r in facet_rows if r["gender"]})

        return {
            "seasonId": season_id,
            "defaultSeason": default_season,
            "seasons": seasons,
            "division": division,
            "type": type,
            "gender": gender,
            "divisions": all_divisions,
            "types": all_types,
            "genders": all_genders,
            "schedules": list(by_id.values()),
        }
    finally:
        conn.close()


def _division_sort_key(name: str) -> tuple:
    """Sort U7…U21 numerically, then Other / unknown last."""
    m = re.match(r"^U(\d+)", str(name).upper())
    if m:
        return (0, int(m.group(1)), name)
    return (1, 99, name)


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=os.environ.get("PCAHA_HOST", "0.0.0.0"),
        port=int(os.environ.get("PCAHA_PORT", "8765")),
    )

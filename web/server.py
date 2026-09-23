#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.9"
# dependencies = ["fastapi>=0.115", "uvicorn>=0.32", "requests>=2.32"]
# ///
"""Local PCAHA schedule + standings viewer.

Uses the same public Spordle Play API as https://games.pcaha.ca (API-Key
embedded in that site's frontend) and adds assigned official names, a
scoresheet PDF link, and SQLite-backed standings / player stats.
"""

from __future__ import annotations

import json
import os
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
    day: str,
    office_id: int | None,
    division: str | None,
    gender: str | None,
    schedule_id: int | None,
    group_id: int | None,
    team_id: int | None,
) -> dict:
    clauses: list[dict] = [{"date": date_iso(day)}]
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


def venue_label(surface: dict | None) -> str:
    if not surface:
        return ""
    venue = surface.get("venue") or {}
    name = venue.get("name") or surface.get("name") or ""
    rink = surface.get("name")
    if name and rink and rink != name:
        return f"{name} ({rink})"
    return name or rink or ""


def city_label(surface: dict | None) -> str:
    if not surface:
        return ""
    venue = surface.get("venue") or {}
    return ", ".join(filter(None, [venue.get("city"), venue.get("region")]))


def title_case_name(name: str | None) -> str:
    if not name:
        return ""
    return " ".join(part.capitalize() for part in name.split())


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
        "defaultSeason": "2026-27",
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
    office_id: Optional[int] = None,
    division: Optional[str] = None,
    gender: Optional[str] = None,
    schedule_id: Optional[int] = None,
    group_id: Optional[int] = None,
    team_id: Optional[int] = None,
    page: int = Query(1, ge=1),
) -> dict:
    if not day:
        day = date.today().isoformat()
    where = games_where(day, office_id, division, gender, schedule_id, group_id, team_id)
    total = _public_get("/games/count", {"where": json.dumps(where, separators=(",", ":"))}).get("count", 0)
    skip = (page - 1) * PAGE_SIZE
    raw = _public_get(
        "/games",
        _filter_param(
            {
                "where": where,
                "include": ["surface", "schedule", "group", "teamStats", "officials"],
                "order": ["startTime ASC", "id DESC"],
                "limit": PAGE_SIZE,
                "skip": skip,
            }
        ),
    )
    team_ids = {t for g in raw for t in (g.get("homeTeamId"), g.get("awayTeamId")) if t}
    teams = {}
    if team_ids:
        for team in _public_get(
            "/teams",
            _filter_param({"where": {"id": {"inq": sorted(team_ids)}}, "scope": "Tenant"}),
        ):
            teams[team["id"]] = team

    items = []
    for g in raw:
        officials = [row for row in (official_row(a) for a in g.get("officials") or []) if row]
        officials.sort(key=lambda r: (_POSITION_ORDER.get(r["position"], 99), r["name"]))
        stats = {s.get("teamId"): s for s in (g.get("teamStats") or [])}
        home = teams.get(g.get("homeTeamId"), {})
        away = teams.get(g.get("awayTeamId"), {})
        items.append(
            {
                "id": g["id"],
                "number": g.get("number"),
                "date": g.get("date"),
                "startTime": format_local_time(g.get("startTime"), g.get("timezone")),
                "endTime": format_local_time(g.get("endTime"), g.get("timezone")),
                "division": g.get("division"),
                "category": g.get("category"),
                "gender": g.get("gender"),
                "status": g.get("status"),
                "isApproved": g.get("isApproved"),
                "venue": venue_label(g.get("surface")),
                "city": city_label(g.get("surface")),
                "schedule": (g.get("schedule") or {}).get("name"),
                "scheduleType": (g.get("schedule") or {}).get("type"),
                "group": (g.get("group") or {}).get("name"),
                "home": home.get("name") or "TBD",
                "away": away.get("name") or "TBD",
                "homeTeamId": g.get("homeTeamId"),
                "awayTeamId": g.get("awayTeamId"),
                "homeScore": (stats.get(g.get("homeTeamId")) or {}).get("goalFor"),
                "awayScore": (stats.get(g.get("awayTeamId")) or {}).get("goalFor"),
                "officials": officials,
                "scoresheetUrl": SCORESHEET_URL.format(id=g["id"]),
            }
        )

    return {
        "total": total,
        "page": page,
        "pageSize": PAGE_SIZE,
        "pages": max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE) if total else 1,
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
                SELECT * FROM standings
                WHERE season_id=? AND schedule_id=?
                ORDER BY pts DESC, gd DESC, gf DESC, team_name ASC
                """,
                (season_id, schedule_id),
            ).fetchall()
            # If multiple groups exist, prefer requiring group_id — still return combined
            groups = sorted({r["group_id"] for r in rows})
        else:
            rows = conn.execute(
                """
                SELECT * FROM standings
                WHERE season_id=? AND schedule_id=? AND group_id=?
                ORDER BY pts DESC, gd DESC, gf DESC, team_name ASC
                """,
                (season_id, schedule_id, group_key),
            ).fetchall()
            groups = [group_key]

        group_names = {}
        for gid in groups:
            if not gid:
                continue
            g = conn.execute("SELECT name FROM groups WHERE id=?", (gid,)).fetchone()
            if g:
                group_names[gid] = g["name"]

        items = []
        for r in rows:
            item = dict(r)
            item["groupName"] = group_names.get(r["group_id"])
            items.append(item)

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
) -> dict:
    conn = db()
    try:
        team = conn.execute("SELECT * FROM teams WHERE id=?", (team_id,)).fetchone()
        if not team:
            # Still allow if we have stats rows
            team = {"id": team_id, "name": f"Team {team_id}", "short_name": None, "office_id": None}

        sql = """
            SELECT * FROM player_stats
            WHERE season_id=? AND team_id=?
        """
        params: list[Any] = [season_id, team_id]
        if schedule_id is not None:
            sql += " AND schedule_id=?"
            params.append(schedule_id)
        sql += " ORDER BY is_goalie ASC, p DESC, g DESC, player_name ASC"
        players = rows_to_dicts(conn.execute(sql, params).fetchall())

        skaters = [p for p in players if not p.get("is_goalie")]
        goalies = [p for p in players if p.get("is_goalie")]

        # Recent games for this team
        game_sql = """
            SELECT g.*,
                   ht.name AS home_name, at.name AS away_name,
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
        if schedule_id is not None:
            game_sql += " AND g.schedule_id=?"
            game_params.append(schedule_id)
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
            SELECT * FROM standings WHERE season_id=? AND team_id=?
        """
        st_params: list[Any] = [season_id, team_id]
        if schedule_id is not None:
            standings_sql += " AND schedule_id=?"
            st_params.append(schedule_id)
        standings_rows = rows_to_dicts(conn.execute(standings_sql, st_params).fetchall())

        return {
            "team": dict(team) if not isinstance(team, dict) else team,
            "seasonId": season_id,
            "scheduleId": schedule_id,
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
            q = f"""
                SELECT ps.*, t.name AS team_name, s.name AS schedule_name, s.type AS schedule_type
                FROM player_stats ps
                LEFT JOIN teams t ON t.id = ps.team_id
                LEFT JOIN schedules s ON s.id = ps.schedule_id
                WHERE {where_sql} AND {gfilter} {extra}
                ORDER BY {metric} DESC, ps.player_name ASC
                LIMIT ?
            """
            return rows_to_dicts(conn.execute(q, [*params, limit]).fetchall())

        return {
            "seasonId": season_id,
            "scheduleId": schedule_id,
            "groupId": group_id,
            "points": top("ps.p"),
            "goals": top("ps.g"),
            "assists": top("ps.a"),
            "pim": top("ps.pim"),
            "goalies": top("ps.goalie_w", goalie=True, extra="AND ps.goalie_gp > 0"),
            "gaa": rows_to_dicts(
                conn.execute(
                    f"""
                    SELECT ps.*, t.name AS team_name, s.name AS schedule_name, s.type AS schedule_type
                    FROM player_stats ps
                    LEFT JOIN teams t ON t.id = ps.team_id
                    LEFT JOIN schedules s ON s.id = ps.schedule_id
                    WHERE {where_sql} AND ps.is_goalie=1 AND ps.goalie_gp >= 2 AND ps.gaa IS NOT NULL
                    ORDER BY ps.gaa ASC, ps.goalie_gp DESC
                    LIMIT ?
                    """,
                    [*params, limit],
                ).fetchall()
            ),
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

        stats_sql = "SELECT ps.*, t.name AS team_name FROM player_stats ps LEFT JOIN teams t ON t.id=ps.team_id WHERE ps.season_id=? AND ps.participant_id=?"
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
                SELECT DISTINCT t.id, t.name, st.schedule_id, s.name AS schedule_name,
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


@app.get("/api/standings/schedules")
def standings_schedules(season_id: str = Query("2026-27")) -> list:
    """Schedules that have standings rows (for the league picker)."""
    conn = db()
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT s.id, s.name, s.type, s.division, s.gender, s.category, s.office_id AS officeId,
                   st.group_id,
                   gr.name AS group_name
            FROM standings st
            JOIN schedules s ON s.id = st.schedule_id
            LEFT JOIN groups gr ON gr.id = st.group_id
            WHERE st.season_id=?
            ORDER BY s.division ASC, s.type ASC, s.name ASC, gr.name ASC
            """,
            (season_id,),
        ).fetchall()
        # Collapse to schedule list with nested groups
        by_id: dict[int, dict] = {}
        for r in rows:
            sid = r["id"]
            if sid not in by_id:
                by_id[sid] = {
                    "id": sid,
                    "name": r["name"],
                    "type": r["type"],
                    "division": r["division"],
                    "gender": r["gender"],
                    "category": r["category"],
                    "officeId": r["officeId"],
                    "groups": [],
                }
            gid = r["group_id"]
            if gid and not any(g["id"] == gid for g in by_id[sid]["groups"]):
                by_id[sid]["groups"].append({"id": gid, "name": r["group_name"] or str(gid)})
        return list(by_id.values())
    finally:
        conn.close()


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=os.environ.get("PCAHA_HOST", "127.0.0.1"),
        port=int(os.environ.get("PCAHA_PORT", "8765")),
    )

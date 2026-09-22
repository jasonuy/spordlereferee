#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.9"
# dependencies = ["fastapi>=0.115", "uvicorn>=0.32", "requests>=2.32"]
# ///
"""Local PCAHA schedule viewer.

Uses the same public Spordle Play API as https://games.pcaha.ca (API-Key
embedded in that site's frontend) and adds assigned official names plus a
scoresheet PDF link for each game.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import requests
import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

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

STATIC = Path(__file__).resolve().parent / "static"
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
    return [{"id": g["id"], "name": g.get("name"), "type": g.get("type")} for g in rows]


@app.get("/api/games")
def games(
    day: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    office_id: Optional[int] = None,
    division: Optional[str] = None,
    gender: Optional[str] = None,
    schedule_id: Optional[int] = None,
    group_id: Optional[int] = None,
    team_id: Optional[int] = None,
    page: int = Query(1, ge=1),
) -> dict:
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


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=os.environ.get("PCAHA_HOST", "127.0.0.1"),
        port=int(os.environ.get("PCAHA_PORT", "8765")),
    )

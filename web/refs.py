"""Authenticated Spordle refs helpers.

Passwords are used only for a one-shot OAuth exchange and are never written
to disk, logs, or session storage. Access/refresh tokens live in an
in-memory server session keyed by an httpOnly cookie.
"""

from __future__ import annotations

import json
import logging
import math
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional
from zoneinfo import ZoneInfo

import requests
from fastapi import APIRouter, Cookie, HTTPException, Query, Response
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

PLAY_BASE = "https://play.spordle.com"
PLAY_API = f"{PLAY_BASE}/api"
CLIENT_ID = "hi-scoresheet"
CLIENT_SECRET = "secret"  # public SPA client secret embedded in Spordle Play
COOKIE_NAME = "pcaha_refs_sid"
SESSION_TTL_SEC = 60 * 60 * 12  # 12 hours
PCAHA_TZ = ZoneInfo("America/Vancouver")
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)

router = APIRouter(prefix="/api/refs", tags=["refs"])

_lock = threading.Lock()
_sessions: dict[str, "RefSession"] = {}


@dataclass
class RefSession:
    access_token: str
    refresh_token: Optional[str]
    expires_at: float
    identity_id: str
    participant_id: int
    account_id: Any
    display_name: str
    username: str
    grades: dict[str, Any] = field(default_factory=dict)
    office_ids: list[int] = field(default_factory=list)
    identities: list[dict] = field(default_factory=list)
    last_used: float = field(default_factory=time.time)


class LoginBody(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)
    identity_id: Optional[str] = None


class IdentityBody(BaseModel):
    identity_id: str = Field(min_length=1)


class RequestBody(BaseModel):
    position: str = Field(min_length=1)  # Referee | Linesperson


def _purge_expired() -> None:
    now = time.time()
    dead = [k for k, s in _sessions.items() if s.last_used + SESSION_TTL_SEC < now]
    for k in dead:
        _sessions.pop(k, None)


def _get_session(sid: Optional[str]) -> RefSession:
    if not sid:
        raise HTTPException(401, "Sign in with Spordle to use the Refs tab")
    with _lock:
        _purge_expired()
        sess = _sessions.get(sid)
        if not sess:
            raise HTTPException(401, "Session expired — sign in again")
        sess.last_used = time.time()
        return sess


def _play_session(sess: RefSession) -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {sess.access_token}",
            "X-Identity": sess.identity_id,
            "X-Device": str(uuid.uuid4()),
            "X-Client-Version": "5.0.0-development",
        }
    )
    return s


def _play_get(sess: RefSession, path: str, params: dict | None = None) -> Any:
    http = _play_session(sess)
    resp = http.get(f"{PLAY_API}{path}", params=params, timeout=45)
    if resp.status_code == 401 and sess.refresh_token:
        _refresh(sess)
        http = _play_session(sess)
        resp = http.get(f"{PLAY_API}{path}", params=params, timeout=45)
    if resp.status_code >= 400:
        raise HTTPException(resp.status_code, resp.text[:400])
    return resp.json()


def _play_post(sess: RefSession, path: str, body: dict | None = None) -> Any:
    http = _play_session(sess)
    resp = http.post(f"{PLAY_API}{path}", json=body or {}, timeout=45)
    if resp.status_code == 401 and sess.refresh_token:
        _refresh(sess)
        http = _play_session(sess)
        resp = http.post(f"{PLAY_API}{path}", json=body or {}, timeout=45)
    if resp.status_code >= 400:
        raise HTTPException(resp.status_code, resp.text[:400])
    if not resp.content:
        return {}
    try:
        return resp.json()
    except Exception:
        return {"ok": True}


def _play_delete(sess: RefSession, path: str, body: dict | None = None) -> Any:
    http = _play_session(sess)
    resp = http.delete(f"{PLAY_API}{path}", json=body, timeout=45)
    if resp.status_code == 401 and sess.refresh_token:
        _refresh(sess)
        http = _play_session(sess)
        resp = http.delete(f"{PLAY_API}{path}", json=body, timeout=45)
    if resp.status_code >= 400:
        raise HTTPException(resp.status_code, resp.text[:400])
    if not resp.content:
        return {}
    try:
        return resp.json()
    except Exception:
        return {"ok": True}


def _refresh(sess: RefSession) -> None:
    """Rotate access token; never touches the user's password."""
    if not sess.refresh_token:
        raise HTTPException(401, "Session expired — sign in again")
    resp = requests.post(
        f"{PLAY_BASE}/oauth/token",
        json={
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "refresh_token": sess.refresh_token,
        },
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=30,
    )
    if resp.status_code >= 400:
        with _lock:
            for k, v in list(_sessions.items()):
                if v is sess:
                    _sessions.pop(k, None)
        raise HTTPException(401, "Session expired — sign in again")
    data = resp.json()
    sess.access_token = data["access_token"]
    if data.get("refresh_token"):
        sess.refresh_token = data["refresh_token"]
    sess.expires_at = time.time() + float(data.get("expires_in") or 3600)


def _is_hockey_canada_identity(ident: dict) -> bool:
    tenant = ident.get("tenant") or {}
    name = str(tenant.get("name") or "").lower()
    sport = str(tenant.get("sport") or "").lower()
    if "ball" in name or "ball" in sport:
        return False
    return "hockey canada" in name or sport == "hockey"


def _pick_official_identity(account: dict, identity_id: Optional[str]) -> dict:
    """Always default to Hockey Canada — this UI is for ice hockey / PCAHA."""
    identities = account.get("identities") or []
    if not identities:
        raise HTTPException(400, "No Spordle identities on this account")
    if identity_id:
        match = next((i for i in identities if str(i.get("id")) == str(identity_id)), None)
        if not match:
            raise HTTPException(400, "Identity not found on this account")
        return match

    hc = next((i for i in identities if _is_hockey_canada_identity(i)), None)
    if hc:
        return hc

    perms = account.get("permissions") or []
    official_pids = {
        p.get("participantId")
        for p in perms
        if p.get("roleName") == "official" or "assigning:official" in (p.get("scopes") or [])
    }
    for ident in identities:
        if ident.get("participantId") in official_pids:
            return ident
    primary = next((i for i in identities if i.get("isPrimary")), None)
    return primary or identities[0]


def _official_meta(account: dict, participant_id: int) -> tuple[dict, list[int]]:
    grades: dict[str, Any] = {}
    office_ids: list[int] = []
    for p in account.get("permissions") or []:
        if p.get("participantId") not in (None, participant_id) and p.get("roleName") != "official":
            if p.get("roleName") != "official" and "assigning:official" not in (p.get("scopes") or []):
                continue
        if p.get("roleName") == "official" or "assigning:official" in (p.get("scopes") or []):
            if p.get("participantId") not in (None, participant_id):
                continue
            if p.get("grades"):
                grades.update(p["grades"])
            for oid in p.get("officeIds") or []:
                if oid not in office_ids:
                    office_ids.append(oid)
    return grades, office_ids


def _identity_public(identities: list[dict]) -> list[dict]:
    out = []
    for i in identities:
        p = i.get("participant") or {}
        out.append(
            {
                "id": i.get("id"),
                "participantId": i.get("participantId"),
                "isPrimary": bool(i.get("isPrimary")),
                "name": p.get("fullName")
                or " ".join(filter(None, [p.get("firstName"), p.get("lastName")])),
                "tenant": (i.get("tenant") or {}).get("name"),
                "isHockeyCanada": _is_hockey_canada_identity(i),
            }
        )
    # Hockey Canada first in the switcher
    out.sort(key=lambda row: (0 if row.get("isHockeyCanada") else 1, row.get("tenant") or ""))
    return out


def _session_public(sess: RefSession) -> dict:
    return {
        "signedIn": True,
        "username": sess.username,
        "displayName": sess.display_name,
        "participantId": sess.participant_id,
        "identityId": sess.identity_id,
        "grades": sess.grades,
        "officeIds": sess.office_ids,
        "identities": _identity_public(sess.identities),
        "passwordStored": False,
    }


@router.get("/me")
def refs_me(pcaha_refs_sid: Optional[str] = Cookie(default=None, alias=COOKIE_NAME)) -> dict:
    try:
        sess = _get_session(pcaha_refs_sid)
    except HTTPException:
        return {"signedIn": False, "passwordStored": False}
    # Keep ice-hockey default sticky: if Hockey Canada exists and isn't active, switch.
    hc = next((i for i in (sess.identities or []) if _is_hockey_canada_identity(i)), None)
    if hc and str(sess.identity_id) != str(hc.get("id")):
        sess.identity_id = str(hc.get("id"))
        sess.participant_id = int(hc.get("participantId") or sess.participant_id)
        part = hc.get("participant") or {}
        sess.display_name = (
            part.get("fullName")
            or " ".join(filter(None, [part.get("firstName"), part.get("lastName")]))
            or sess.display_name
        )
        try:
            account = _play_get(sess, "/accounts/current")
            sess.identities = account.get("identities") or sess.identities
            sess.grades, sess.office_ids = _official_meta(account, sess.participant_id)
        except HTTPException:
            pass
    return _session_public(sess)


@router.post("/login")
def refs_login(body: LoginBody, response: Response) -> dict:
    """Exchange Spordle credentials for tokens. Password is not retained."""
    username = body.username.strip()
    password = body.password  # local only; never assign onto session / disk
    try:
        resp = requests.post(
            f"{PLAY_BASE}/oauth/token",
            json={
                "grant_type": "password",
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "username": username,
                "password": password,
            },
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=30,
        )
    finally:
        # Drop local reference ASAP; do not log body/password.
        password = ""
        body.password = ""

    if resp.status_code >= 400:
        raise HTTPException(401, "Spordle sign-in failed — check email/username and password")

    token = resp.json()
    access = token.get("access_token")
    if not access:
        raise HTTPException(502, "Spordle did not return an access token")

    # Temporary session headers to load account
    tmp = requests.Session()
    tmp.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Authorization": f"Bearer {access}",
        }
    )
    account_resp = tmp.get(f"{PLAY_API}/accounts/current", timeout=30)
    if account_resp.status_code >= 400:
        raise HTTPException(502, "Could not load Spordle account after sign-in")
    account = account_resp.json()
    identities = account.get("identities") or []
    ident = _pick_official_identity(account, body.identity_id)
    participant_id = int(ident.get("participantId") or account.get("participantId") or 0)
    if not participant_id:
        raise HTTPException(400, "No participant on this Spordle identity")
    part = ident.get("participant") or {}
    display = (
        part.get("fullName")
        or " ".join(filter(None, [part.get("firstName"), part.get("lastName")]))
        or username
    )
    grades, office_ids = _official_meta(account, participant_id)

    sid = secrets.token_urlsafe(32)
    sess = RefSession(
        access_token=access,
        refresh_token=token.get("refresh_token"),
        expires_at=time.time() + float(token.get("expires_in") or 3600),
        identity_id=str(ident.get("id")),
        participant_id=participant_id,
        account_id=account.get("id"),
        display_name=display,
        username=username,
        grades=grades,
        office_ids=office_ids,
        identities=identities,
    )
    with _lock:
        _purge_expired()
        _sessions[sid] = sess

    response.set_cookie(
        key=COOKIE_NAME,
        value=sid,
        httponly=True,
        samesite="lax",
        secure=False,  # local/LAN http on Studio; set True behind HTTPS reverse proxy if desired
        max_age=SESSION_TTL_SEC,
        path="/",
    )
    return _session_public(sess)


@router.post("/logout")
def refs_logout(
    response: Response,
    pcaha_refs_sid: Optional[str] = Cookie(default=None, alias=COOKIE_NAME),
) -> dict:
    if pcaha_refs_sid:
        with _lock:
            _sessions.pop(pcaha_refs_sid, None)
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"signedIn": False, "passwordStored": False}


@router.post("/identity")
def refs_set_identity(
    body: IdentityBody,
    pcaha_refs_sid: Optional[str] = Cookie(default=None, alias=COOKIE_NAME),
) -> dict:
    sess = _get_session(pcaha_refs_sid)
    ident = _pick_official_identity(
        {"identities": sess.identities, "permissions": []}, body.identity_id
    )
    # Re-fetch account for accurate grades under new identity
    account = _play_get(sess, "/accounts/current")
    # Switch identity header for subsequent calls
    sess.identity_id = str(ident.get("id"))
    sess.participant_id = int(ident.get("participantId") or sess.participant_id)
    part = ident.get("participant") or {}
    sess.display_name = (
        part.get("fullName")
        or " ".join(filter(None, [part.get("firstName"), part.get("lastName")]))
        or sess.display_name
    )
    sess.identities = account.get("identities") or sess.identities
    sess.grades, sess.office_ids = _official_meta(account, sess.participant_id)
    return _session_public(sess)


def _filter_param(payload: dict) -> dict:
    return {"filter": json.dumps(payload, separators=(",", ":"))}


def _date_iso(day: str) -> str:
    return f"{day}T00:00:00.000Z"


def _format_local_time(iso: str | None, tz_name: str | None) -> str | None:
    if not iso:
        return None
    try:
        dt = __import__("datetime").datetime.fromisoformat(iso.replace("Z", "+00:00"))
        tz = ZoneInfo(tz_name) if tz_name else PCAHA_TZ
        return dt.astimezone(tz).strftime("%-I:%M %p")
    except Exception:
        return iso


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


_geocode_cache: dict[str, Optional[tuple[float, float]]] = {}


def _geocode(query: str) -> Optional[tuple[float, float]]:
    q = " ".join(query.split())
    if not q:
        return None
    if q in _geocode_cache:
        return _geocode_cache[q]
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": q, "format": "json", "limit": 1},
            headers={"User-Agent": "pcaha-schedule-refs/1.0"},
            timeout=15,
        )
        if resp.status_code >= 400:
            _geocode_cache[q] = None
            return None
        rows = resp.json()
        if not rows:
            _geocode_cache[q] = None
            return None
        coords = (float(rows[0]["lat"]), float(rows[0]["lon"]))
        _geocode_cache[q] = coords
        return coords
    except Exception:
        _geocode_cache[q] = None
        return None


def _venue_label(surface: dict | None) -> str:
    if not surface:
        return ""
    venue = surface.get("venue") or {}
    name = venue.get("name") or surface.get("name") or ""
    rink = surface.get("name")
    if name and rink and rink != name:
        return f"{name} ({rink})"
    return name or rink or ""


def _venue_address(surface: dict | None) -> str:
    if not surface:
        return ""
    venue = surface.get("venue") or {}
    return ", ".join(
        filter(
            None,
            [
                venue.get("address"),
                venue.get("city"),
                venue.get("region"),
                venue.get("postalCode"),
            ],
        )
    )


def _slot_summary(assignments: list[dict], my_pid: int) -> dict:
    by_pos: dict[str, dict] = {}
    my_statuses: list[dict] = []
    for a in assignments or []:
        pos = a.get("position") or "Official"
        st = (a.get("status") or "").lower()
        bucket = by_pos.setdefault(
            pos, {"confirmed": 0, "requested": 0, "pending": 0, "open": 0, "total": 0}
        )
        bucket["total"] += 1
        if st == "confirmed":
            bucket["confirmed"] += 1
        elif st == "requested":
            bucket["requested"] += 1
        elif st in ("pending", "offered"):
            bucket["pending"] += 1
        elif st in ("", "unassigned", "open", "declined") or a.get("participantId") is None:
            bucket["open"] += 1
        pid = a.get("participantId")
        if pid is not None and int(pid) == int(my_pid):
            my_statuses.append({"position": pos, "status": st, "id": a.get("id")})
    needs_referee = by_pos.get("Referee", {}).get("open", 0) > 0 or (
        by_pos.get("Referee", {}).get("total", 0) == 0
        and False  # unknown requirement
    )
    # Prefer explicit open slots; also treat missing confirmed vs typical 1–2 refs via gamestatus
    return {"byPosition": by_pos, "mine": my_statuses, "needsReferee": needs_referee}


def _enrich_games(
    sess: RefSession,
    raw_games: list[dict],
    *,
    home: Optional[str] = None,
    max_distance_km: Optional[float] = None,
) -> list[dict]:
    if not raw_games:
        return []
    arena_ids = {g.get("arenaId") for g in raw_games if g.get("arenaId")}
    team_ids = {t for g in raw_games for t in (g.get("homeTeamId"), g.get("awayTeamId")) if t}
    group_ids = {g.get("groupId") for g in raw_games if g.get("groupId")}
    game_ids = {g["id"] for g in raw_games}

    surfaces = {}
    teams = {}
    groups = {}
    statuses = {}
    if arena_ids:
        surfaces = {
            i["id"]: i
            for i in _play_get(
                sess,
                "/surfaces",
                _filter_param({"where": {"id": {"inq": sorted(arena_ids)}}, "scope": "Tenant"}),
            )
        }
    if team_ids:
        teams = {
            i["id"]: i
            for i in _play_get(
                sess,
                "/teams",
                _filter_param({"where": {"id": {"inq": sorted(team_ids)}}, "scope": "Tenant"}),
            )
        }
    if group_ids:
        groups = {
            i["id"]: i
            for i in _play_get(
                sess,
                "/groups",
                _filter_param({"where": {"id": {"inq": sorted(group_ids)}}, "scope": "Tenant"}),
            )
        }
    if game_ids:
        statuses = {
            i["id"]: i
            for i in _play_get(
                sess,
                "/gamestatuses",
                _filter_param({"where": {"id": {"inq": sorted(game_ids)}}, "scope": "Tenant"}),
            )
        }

    home_coords = _geocode(home) if home else None
    out = []
    for g in raw_games:
        status = statuses.get(g["id"]) or {}
        officials_meta = status.get("officials") or {}
        surface = surfaces.get(g.get("arenaId"))
        home_t = teams.get(g.get("homeTeamId")) or {}
        away_t = teams.get(g.get("awayTeamId")) or {}
        distance = None
        if home_coords and surface:
            venue = surface.get("venue") or {}
            addr = ", ".join(
                filter(
                    None,
                    [
                        venue.get("address"),
                        venue.get("city"),
                        venue.get("region"),
                        venue.get("country"),
                    ],
                )
            )
            fallback = ", ".join(
                filter(None, [venue.get("name"), venue.get("city"), venue.get("region")])
            )
            rink = _geocode(addr) or _geocode(fallback)
            if rink:
                distance = round(_haversine_km(*home_coords, *rink), 1)
        if max_distance_km is not None and distance is not None and distance > max_distance_km:
            continue
        if max_distance_km is not None and home_coords and distance is None:
            continue

        unassigned = officials_meta.get("unassigned")
        all_assigned = officials_meta.get("allAssigned")
        out.append(
            {
                "id": g["id"],
                "number": g.get("number"),
                "date": (g.get("date") or "")[:10],
                "startTime": _format_local_time(g.get("startTime"), g.get("timezone")),
                "endTime": _format_local_time(g.get("endTime"), g.get("timezone")),
                "startTimeIso": g.get("startTime"),
                "endTimeIso": g.get("endTime"),
                "division": g.get("division"),
                "category": g.get("category"),
                "gender": g.get("gender"),
                "seasonId": g.get("seasonId"),
                "venue": _venue_label(surface),
                "venueAddress": _venue_address(surface),
                "arenaId": g.get("arenaId"),
                "home": home_t.get("name") or "TBD",
                "away": away_t.get("name") or "TBD",
                "homeTeamId": g.get("homeTeamId"),
                "awayTeamId": g.get("awayTeamId"),
                "homeLogoUrl": home_t.get("logoUrl"),
                "awayLogoUrl": away_t.get("logoUrl"),
                "group": (groups.get(g.get("groupId")) or {}).get("name") if g.get("groupId") else None,
                "scheduleId": g.get("scheduleId"),
                "gameStatus": status.get("status"),
                "unassigned": unassigned,
                "allAssigned": all_assigned,
                "refereesFilled": officials_meta.get("referees"),
                "linespersonsFilled": officials_meta.get("linespersons"),
                "requests": officials_meta.get("requests"),
                "pending": officials_meta.get("pending"),
                "distanceKm": distance,
                "scoresheetUrl": f"https://pdf.play.spordle.com/game/{g['id']}",
                "open": bool(
                    all_assigned is False
                    or (isinstance(unassigned, int) and unassigned > 0)
                ),
            }
        )
    return out


def _fetch_authorized_games(
    sess: RefSession,
    *,
    season_id: str,
    from_day: Optional[str],
    day: Optional[str],
    division: Optional[list[str]],
    gender: Optional[str],
    schedule_id: Optional[int],
    page: int,
    page_size: int,
) -> tuple[list[dict], int]:
    clauses: list[dict] = [{"seasonId": season_id}]
    if from_day:
        clauses.append({"date": {"gte": _date_iso(from_day)}})
    elif day:
        clauses.append({"date": _date_iso(day)})
    else:
        clauses.append({"date": {"gte": _date_iso(date.today().isoformat())}})
    if division:
        names = [d for d in division if d]
        if len(names) == 1:
            clauses.append({"division": names[0]})
        elif len(names) > 1:
            clauses.append({"division": {"inq": names}})
    if gender:
        clauses.append({"gender": gender})
    if schedule_id is not None:
        clauses.append({"scheduleId": schedule_id})

    where = {"and": clauses}
    # Count via header on first page
    skip = (page - 1) * page_size
    filt = {
        "scope": "Authorized",
        "where": where,
        "order": ["date ASC", "startTime ASC", "number ASC"],
        "limit": page_size,
        "skip": skip,
    }
    http = _play_session(sess)
    resp = http.get(f"{PLAY_API}/games", params=_filter_param(filt), timeout=45)
    if resp.status_code == 401 and sess.refresh_token:
        _refresh(sess)
        http = _play_session(sess)
        resp = http.get(f"{PLAY_API}/games", params=_filter_param(filt), timeout=45)
    if resp.status_code >= 400:
        raise HTTPException(resp.status_code, resp.text[:400])
    games = resp.json() or []
    total = int(resp.headers.get("X-Total-Count") or len(games))
    return games, total


def _attach_assignments(sess: RefSession, games: list[dict]) -> None:
    """Add per-game slot detail + my request/assignment state (current page only)."""
    for g in games:
        try:
            raw = _play_get(sess, f"/games/{g['id']}/officials")
        except HTTPException:
            try:
                raw = _play_get(sess, f"/games/{g['id']}/officialAssignments")
            except HTTPException:
                raw = []
        if not isinstance(raw, list):
            raw = []
        summary = _slot_summary(raw, sess.participant_id)
        by_pos = summary["byPosition"]
        g["slots"] = by_pos
        g["mine"] = summary["mine"]
        ref = by_pos.get("Referee") or {}
        lines = by_pos.get("Linesperson") or {}
        if "Referee" in by_pos:
            g["needsReferee"] = ref.get("open", 0) > 0
        else:
            g["needsReferee"] = bool(g.get("open"))
        if "Linesperson" in by_pos:
            g["needsLinesperson"] = lines.get("open", 0) > 0
        else:
            g["needsLinesperson"] = bool(g.get("open"))
        g["myRequested"] = any(m.get("status") == "requested" for m in summary["mine"])
        g["myAssigned"] = any(m.get("status") == "confirmed" for m in summary["mine"])
        crew = []
        for a in raw:
            if (a.get("status") or "").lower() != "confirmed":
                continue
            p = a.get("participant") or {}
            name = p.get("fullName") or " ".join(
                filter(None, [p.get("firstName"), p.get("lastName")])
            )
            if name:
                crew.append({"position": a.get("position") or "Official", "name": name})
        g["crew"] = crew


@router.get("/games")
def refs_games(
    season_id: str = Query("2026-27"),
    day: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    from_day: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    division: Optional[list[str]] = Query(None),
    gender: Optional[str] = None,
    schedule_id: Optional[int] = None,
    crew: str = Query("open"),  # open | any | staffed
    position: str = Query("any"),  # any | Referee | Linesperson
    my_status: str = Query("any"),  # any | available | requested | assigned
    home: Optional[str] = None,
    max_distance_km: Optional[float] = Query(None, ge=0),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=50),
    pcaha_refs_sid: Optional[str] = Cookie(default=None, alias=COOKIE_NAME),
) -> dict:
    sess = _get_session(pcaha_refs_sid)
    # Over-fetch a bit when filtering client-side on open/position
    fetch_size = page_size
    raw, total = _fetch_authorized_games(
        sess,
        season_id=season_id,
        from_day=from_day,
        day=day,
        division=division,
        gender=gender,
        schedule_id=schedule_id,
        page=page,
        page_size=fetch_size,
    )
    games = _enrich_games(sess, raw, home=home, max_distance_km=max_distance_km)
    _attach_assignments(sess, games)

    def keep(g: dict) -> bool:
        if crew == "open" and not g.get("open"):
            return False
        if crew == "staffed" and g.get("open"):
            return False
        if position == "Referee" and not g.get("needsReferee"):
            return False
        if position == "Linesperson" and not g.get("needsLinesperson"):
            return False
        if my_status == "requested" and not g.get("myRequested"):
            return False
        if my_status == "assigned" and not g.get("myAssigned"):
            return False
        if my_status == "available" and (g.get("myRequested") or g.get("myAssigned")):
            return False
        return True

    filtered = [g for g in games if keep(g)]

    # Conflicts: overlap with my confirmed/requested on this page set is weak;
    # also load assignHistory for season to mark conflicts.
    my_blocks = _my_time_blocks(sess, season_id)
    for g in filtered:
        g["conflicts"] = _conflicts_for(g, my_blocks)

    return {
        "total": total,
        "page": page,
        "pageSize": page_size,
        "pages": max(1, (total + page_size - 1) // page_size) if total else 0,
        "returned": len(filtered),
        "games": filtered,
        "me": _session_public(sess),
    }


def _my_time_blocks(sess: RefSession, season_id: str) -> list[dict]:
    try:
        rows = _play_get(
            sess,
            f"/participants/{sess.participant_id}/assignHistory",
            {"seasonId": season_id},
        )
    except HTTPException:
        return []
    blocks = []
    for row in rows or []:
        game = row.get("game") or {}
        blocks.append(
            {
                "gameId": row.get("gameId") or game.get("id"),
                "status": (row.get("status") or "").lower(),
                "position": row.get("position"),
                "start": game.get("startTime"),
                "end": game.get("endTime"),
                "date": (game.get("date") or "")[:10],
            }
        )
    return blocks


def _conflicts_for(game: dict, blocks: list[dict]) -> list[dict]:
    start = game.get("startTimeIso")
    end = game.get("endTimeIso")
    if not start or not end:
        return []
    hits = []
    for b in blocks:
        if b.get("gameId") == game.get("id"):
            continue
        if not b.get("start") or not b.get("end"):
            continue
        if b["start"] < end and b["end"] > start:
            hits.append(
                {
                    "gameId": b.get("gameId"),
                    "status": b.get("status"),
                    "position": b.get("position"),
                }
            )
    return hits


def _assign_history_for_identity(
    sess: RefSession, identity: dict, season_id: str
) -> list[dict]:
    """Fetch assignHistory under a specific identity (restores session after)."""
    prev_id = sess.identity_id
    prev_pid = sess.participant_id
    sess.identity_id = str(identity.get("id"))
    sess.participant_id = int(identity.get("participantId") or prev_pid)
    try:
        rows = _play_get(
            sess,
            f"/participants/{sess.participant_id}/assignHistory",
            {"seasonId": season_id},
        )
        return rows if isinstance(rows, list) else []
    except HTTPException:
        return []
    finally:
        sess.identity_id = prev_id
        sess.participant_id = prev_pid


def _game_from_history_row(row: dict) -> Optional[dict]:
    g = row.get("game")
    if isinstance(g, dict) and g.get("id") is not None:
        return g
    return None


def _season_window(season_id: str) -> tuple[str, str]:
    """PCAHA ice season window: Aug 1 → Mar 31."""
    try:
        start_year = int(str(season_id).split("-")[0])
    except Exception:
        start_year = date.today().year if date.today().month >= 8 else date.today().year - 1
    return f"{start_year}-08-01", f"{start_year + 1}-03-31"


def _fetch_pay_by_game(sess: RefSession, participant_id: int) -> dict[int, float]:
    """Sum officialTransactions amounts per gameId."""
    pay: dict[int, float] = {}
    skip = 0
    while skip < 2000:
        filt = {
            "where": {"participantId": participant_id},
            "order": "date DESC",
            "limit": 100,
            "skip": skip,
        }
        try:
            page = _play_get(sess, "/officialTransactions", _filter_param(filt))
        except HTTPException:
            break
        if not isinstance(page, list) or not page:
            break
        for row in page:
            gid = row.get("gameId")
            if gid is None:
                continue
            try:
                amt = float(row.get("amount") or 0)
            except (TypeError, ValueError):
                amt = 0.0
            pay[int(gid)] = pay.get(int(gid), 0.0) + amt
        if len(page) < 100:
            break
        skip += 100
    return pay


def _minimal_game_card(g: dict, meta: dict) -> dict:
    """Fallback when Tenant enrich fails — still show the assignment."""
    return {
        "id": g.get("id"),
        "number": g.get("number"),
        "date": (g.get("date") or "")[:10],
        "startTime": _format_local_time(g.get("startTime"), g.get("timezone")),
        "endTime": _format_local_time(g.get("endTime"), g.get("timezone")),
        "startTimeIso": g.get("startTime"),
        "endTimeIso": g.get("endTime"),
        "division": g.get("division"),
        "category": g.get("category"),
        "gender": g.get("gender"),
        "seasonId": g.get("seasonId"),
        "venue": "",
        "venueAddress": "",
        "home": "TBD",
        "away": "TBD",
        "homeTeamId": g.get("homeTeamId"),
        "awayTeamId": g.get("awayTeamId"),
        "scoresheetUrl": f"https://pdf.play.spordle.com/game/{g.get('id')}",
        "mine": [meta],
        "myAssigned": (meta.get("status") or "").lower() in ("confirmed", "assigned"),
        "myRequested": (meta.get("status") or "").lower() == "requested",
        "open": False,
        "crew": [],
        "conflicts": [],
    }


@router.get("/mine")
def refs_mine(
    season_id: str = Query("2026-27"),
    kind: str = Query("assigned"),  # assigned | requested
    when: Optional[str] = Query("all"),  # all | upcoming | completed — assigned only
    position: Optional[str] = Query("any"),
    pcaha_refs_sid: Optional[str] = Cookie(default=None, alias=COOKIE_NAME),
) -> dict:
    sess = _get_session(pcaha_refs_sid)
    if kind == "assigned":
        # Ice hockey only — Hockey Canada identity (not Ball Hockey).
        identities = sess.identities or [
            {"id": sess.identity_id, "participantId": sess.participant_id}
        ]
        hc_idents = [i for i in identities if _is_hockey_canada_identity(i)]
        use_idents = hc_idents or [
            next(
                (i for i in identities if str(i.get("id")) == str(sess.identity_id)),
                {"id": sess.identity_id, "participantId": sess.participant_id},
            )
        ]

        season_start, season_end = _season_window(season_id)
        rows_by_game: dict[int, tuple[dict, dict]] = {}
        for ident in use_idents:
            for row in _assign_history_for_identity(sess, ident, season_id):
                g = _game_from_history_row(row)
                if not g:
                    continue
                gdate = (g.get("date") or "")[:10]
                if gdate < season_start or gdate > season_end:
                    continue
                status = (row.get("status") or "").lower()
                if status not in ("confirmed", "assigned"):
                    continue
                pos = row.get("position") or ""
                if position and position != "any" and pos != position:
                    continue
                gid = int(g["id"])
                meta = {
                    "assignmentId": row.get("id"),
                    "position": row.get("position"),
                    "status": row.get("status"),
                    "gameId": gid,
                    "identityId": ident.get("id"),
                    "tenant": (ident.get("tenant") or {}).get("name"),
                }
                rows_by_game[gid] = (g, meta)

        pay_pid = int(use_idents[0].get("participantId") or sess.participant_id)
        prev_id, prev_pid = sess.identity_id, sess.participant_id
        sess.identity_id = str(use_idents[0].get("id") or sess.identity_id)
        sess.participant_id = pay_pid
        try:
            pay_by_game = _fetch_pay_by_game(sess, pay_pid)
        finally:
            sess.identity_id = prev_id
            sess.participant_id = prev_pid

        games_raw = [g for g, _ in rows_by_game.values()]
        try:
            enriched = _enrich_games(sess, games_raw)
        except HTTPException:
            enriched = []
        by_id = {int(g["id"]): g for g in enriched if g.get("id") is not None}
        items = []
        today = date.today().isoformat()
        earned = 0.0
        future = 0.0
        for gid, (raw_g, meta) in rows_by_game.items():
            pay = pay_by_game.get(gid)
            g = by_id.get(gid)
            if g:
                card = {
                    **g,
                    "mine": [meta],
                    "myAssigned": True,
                    "myRequested": False,
                    "assignmentPosition": meta.get("position"),
                    "assignmentTenant": meta.get("tenant"),
                    "pay": pay,
                }
            else:
                card = _minimal_game_card(raw_g, meta)
                card["assignmentPosition"] = meta.get("position")
                card["assignmentTenant"] = meta.get("tenant")
                card["pay"] = pay
            gdate = card.get("date") or ""
            # Season pay totals ignore the when-filter so summary stays stable.
            if pay is not None:
                if gdate < today:
                    earned += pay
                else:
                    future += pay
            if when == "upcoming" and gdate < today:
                continue
            if when == "completed" and gdate >= today:
                continue
            items.append(card)

        items.sort(
            key=lambda x: (
                0 if (x.get("date") or "") >= today else 1,
                x.get("date") or "",
                x.get("startTimeIso") or "",
            )
        )
        return {
            "kind": "assigned",
            "games": items,
            "count": len(items),
            "seasonStart": season_start,
            "seasonEnd": season_end,
            "when": when,
            "pay": {
                "earned": round(earned, 2),
                "future": round(future, 2),
                "total": round(earned + future, 2),
                "currency": "CAD",
            },
            "me": _session_public(sess),
        }

    # requested
    today = date.today().isoformat()
    found: list[dict] = []
    page = 1
    while page <= 8 and len(found) < 100:
        raw, total = _fetch_authorized_games(
            sess,
            season_id=season_id,
            from_day=today,
            day=None,
            division=None,
            gender=None,
            schedule_id=None,
            page=page,
            page_size=50,
        )
        if not raw:
            break
        enriched = _enrich_games(sess, raw)
        _attach_assignments(sess, enriched)
        for g in enriched:
            if not g.get("myRequested"):
                continue
            if position and position != "any":
                mine_pos = {(m.get("position") or "") for m in (g.get("mine") or [])}
                if position not in mine_pos and not (
                    position == "Referee" and g.get("myRequested")
                ):
                    # Keep if any requested slot matches
                    if not any(
                        (m.get("status") == "requested" and m.get("position") == position)
                        for m in (g.get("mine") or [])
                    ):
                        continue
            found.append(g)
        if page * 50 >= total:
            break
        page += 1
    found.sort(key=lambda x: (x.get("date") or "", x.get("startTimeIso") or ""))
    return {"kind": "requested", "games": found, "me": _session_public(sess)}


@router.post("/games/{game_id}/request")
def refs_request(
    game_id: int,
    body: RequestBody,
    pcaha_refs_sid: Optional[str] = Cookie(default=None, alias=COOKIE_NAME),
) -> dict:
    sess = _get_session(pcaha_refs_sid)
    position = body.position.strip()
    # Try common Spordle shapes
    errors = []
    for path, payload in [
        (f"/games/{game_id}/requestAssignment", {"position": position}),
        (f"/games/{game_id}/requestAssignment", {"positions": [position]}),
        (f"/games/{game_id}/officialAssignments/request", {"position": position}),
    ]:
        try:
            data = _play_post(sess, path, payload)
            return {"ok": True, "path": path, "result": data}
        except HTTPException as err:
            errors.append(f"{path}: {err.detail}")
            if err.status_code not in (404, 405, 400):
                # auth/permission real failure
                if err.status_code in (401, 403):
                    raise
    raise HTTPException(
        502,
        "Could not request this game via Spordle. "
        "The request API shape may need a one-time capture from Play network traffic. "
        + " | ".join(errors[:2]),
    )


@router.post("/games/{game_id}/unrequest")
def refs_unrequest(
    game_id: int,
    pcaha_refs_sid: Optional[str] = Cookie(default=None, alias=COOKIE_NAME),
) -> dict:
    sess = _get_session(pcaha_refs_sid)
    # Find my requested assignment id
    try:
        assignments = _play_get(sess, f"/games/{game_id}/officials")
    except HTTPException:
        assignments = _play_get(sess, f"/games/{game_id}/officialAssignments")
    mine = [
        a
        for a in (assignments or [])
        if a.get("participantId") == sess.participant_id
        and (a.get("status") or "").lower() == "requested"
    ]
    if not mine:
        raise HTTPException(404, "No pending request found on this game")
    assignment_id = mine[0].get("id")
    errors = []
    for path, payload in [
        (f"/games/{game_id}/removeAssignment", {"id": assignment_id}),
        (f"/games/{game_id}/removeAssignment", {"assignmentId": assignment_id}),
        (f"/games/{game_id}/officialAssignments/{assignment_id}", None),
    ]:
        try:
            if path.endswith(str(assignment_id)) and payload is None:
                data = _play_delete(sess, path)
            else:
                data = _play_post(sess, path, payload or {})
            return {"ok": True, "path": path, "result": data}
        except HTTPException as err:
            errors.append(f"{path}: {err.detail}")
            if err.status_code in (401, 403):
                raise
    raise HTTPException(502, "Could not withdraw request. " + " | ".join(errors[:2]))

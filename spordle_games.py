#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = ["requests>=2.32"]
# ///
"""Download the Spordle Play games list into a CSV/JSON snapshot.

See API.md in this repo for how this endpoint was found.

Credentials are read from SPORDLE_USERNAME / SPORDLE_PASSWORD env vars (or
--username/--password) - never hardcode them in this file or pass them where
they'd land in shell history. The identity to act as can likewise come from
SPORDLE_UUID (or --identity); defaults to the account's primary identity.

Examples:
    export SPORDLE_USERNAME=you@example.com
    export SPORDLE_PASSWORD='...'
    uv run spordle_games.py --list-identities
    export SPORDLE_UUID=<uuid-from-the-list-above>
    uv run spordle_games.py --season 2026-27
    uv run spordle_games.py --identity <uuid> --season 2026-27 --from-date 2026-01-01 --format json
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import requests

BASE = "https://play.spordle.com"
API = f"{BASE}/api"
CLIENT_ID = "hi-scoresheet"
CLIENT_SECRET = "secret"  # literal public SPA "secret", not a real credential
GEOCODE_URL = "https://nominatim.openstreetmap.org/search"
GEOCODE_CACHE_PATH = Path("data/.geocode_cache.json")
ROUTING_URL = "https://router.project-osrm.org/route/v1/driving"
ROUTING_CACHE_PATH = Path("data/.driving_cache.json")
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)


@dataclass
class Game:
    id: int
    number: str
    date: str
    division: str
    category: str | None
    home: str
    away: str
    venue: str
    group: str
    game_status: str | None
    all_assigned: bool | None
    unassigned: int | None
    referees: int | None
    linespersons: int | None
    distance_km: float | None = None


class SpordleClient:
    def __init__(self, username: str, password: str):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "X-Device": str(uuid.uuid4()),
                "X-Client-Version": "5.0.0-development",
            }
        )
        self._username = username
        self._password = password

    def login(self) -> None:
        resp = self.session.post(
            f"{BASE}/oauth/token",
            json={
                "grant_type": "password",
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "username": self._username,
                "password": self._password,
            },
        )
        resp.raise_for_status()
        token = resp.json()["access_token"]
        self.session.headers["Authorization"] = f"Bearer {token}"

    def set_identity(self, identity_id: str) -> None:
        self.session.headers["X-Identity"] = identity_id

    def current_account(self) -> dict:
        return self._get(f"{API}/accounts/current").json()

    def _get(self, url: str, params: dict | None = None, retries: int = 4) -> requests.Response:
        for attempt in range(retries):
            resp = self.session.get(url, params=params)
            if resp.status_code == 403 and attempt < retries - 1:
                time.sleep(2**attempt)
                continue
            resp.raise_for_status()
            return resp
        resp.raise_for_status()
        return resp

    def games(
        self,
        season_id: str,
        from_date: str,
        arena_ids: set | None = None,
        page_size: int = 25,
    ) -> list[dict]:
        skip = 0
        results: list[dict] = []
        total = None
        and_clauses = [
            {"date": {"gte": from_date}},
            {"seasonId": season_id, "_withCount": True},
        ]
        if arena_ids:
            and_clauses.append({"arenaId": {"inq": sorted(arena_ids)}})
        while total is None or skip < total:
            filt = {
                "scope": "Authorized",
                "include": ["gameBracket", "shootouts"],
                "where": {"and": and_clauses},
                "order": ["date ASC", "startTime ASC", "number ASC"],
                "limit": page_size,
                "skip": skip,
            }
            resp = self._get(f"{API}/games", params={"filter": json.dumps(filt)})
            page = resp.json()
            if not page:
                break
            results.extend(page)
            total = int(resp.headers.get("X-Total-Count", len(page)))
            skip += page_size
            time.sleep(0.2)
        return results

    def find_arenas(self, city: str | None = None, venue_name: str | None = None) -> dict:
        """Resolve rink location filters to arenaIds.

        The games endpoint's `where` only matches columns on the Game model
        itself (arenaId, a plain foreign key) - it can't filter on the nested
        venue.city/venue.name fields the surfaces endpoint embeds. So this
        pages through /api/surfaces (scope: Tenant, no server-side location
        filter available) and matches city/venue name client-side instead.
        """
        matches: dict = {}
        skip = 0
        page_size = 5000
        while True:
            filt = {"scope": "Tenant", "limit": page_size, "skip": skip}
            resp = self._get(f"{API}/surfaces", params={"filter": json.dumps(filt)})
            page = resp.json()
            if not page:
                break
            for surface in page:
                venue = surface.get("venue") or {}
                if city and city.lower() not in (venue.get("city") or "").lower():
                    continue
                if venue_name and venue_name.lower() not in (venue.get("name") or "").lower():
                    continue
                if not city and not venue_name:
                    continue
                matches[surface["id"]] = surface
            skip += page_size
            if len(page) < page_size:
                break
        return matches

    def lookup(self, resource: str, ids: set) -> dict:
        if not ids:
            return {}
        filt = {"where": {"id": {"inq": sorted(ids)}}, "scope": "Tenant"}
        resp = self._get(f"{API}/{resource}", params={"filter": json.dumps(filt)})
        return {item["id"]: item for item in resp.json()}


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


_ORDINAL_WORDS = {
    "1st": "First", "2nd": "Second", "3rd": "Third", "4th": "Fourth", "5th": "Fifth",
    "6th": "Sixth", "7th": "Seventh", "8th": "Eighth", "9th": "Ninth", "10th": "Tenth",
    "11th": "Eleventh", "12th": "Twelfth", "13th": "Thirteenth", "14th": "Fourteenth",
    "15th": "Fifteenth", "16th": "Sixteenth", "17th": "Seventeenth", "18th": "Eighteenth",
    "19th": "Nineteenth", "20th": "Twentieth",
}  # fmt: skip
_ORDINAL_RE = re.compile(r"\b(\d{1,2}(?:st|nd|rd|th))\b", re.IGNORECASE)


def _spell_out_ordinals(address: str) -> str | None:
    """'51 3rd Avenue' -> '51 Third Avenue'.

    OSM/Nominatim data for the Vancouver area sometimes has street names
    spelled out as words (Queen's Park Arena is mapped as "Third Avenue")
    even though the numeral form ("3rd Avenue") is what everyone actually
    writes, including Spordle's own venue records. Returns None if nothing
    changed, so the caller knows not to bother re-querying.
    """
    changed = False

    def repl(m: re.Match) -> str:
        nonlocal changed
        word = _ORDINAL_WORDS.get(m.group(1).lower())
        if word:
            changed = True
            return word
        return m.group(0)

    result = _ORDINAL_RE.sub(repl, address)
    return result if changed else None


class Geocoder:
    """Address -> (lat, lon) via OpenStreetMap Nominatim.

    Spordle's venue records only carry street addresses, not coordinates, so
    ranking rinks by distance needs a separate geocoding step. Nominatim's
    usage policy requires a real User-Agent and no more than ~1 request/sec -
    this caches every lookup to disk (data/.geocode_cache.json) so a rerun
    against the same rinks never re-hits the geocoder at all.

    Some addresses fail outright - either the "3rd Avenue" vs "Third Avenue"
    mismatch above, or cases where Spordle's own address is simply wrong
    (UBC's rink is listed at "Thunderbird Ave", a street that doesn't exist -
    the real one is "Thunderbird Boulevard"). `geocode()` retries with the
    ordinal spelled out, then as a last resort with the venue's own name
    (`fallback_query`) as a landmark search, which is less precise (it can
    resolve to a general area rather than the exact building) but far better
    than dropping the location entirely.
    """

    def __init__(self, cache_path: Path = GEOCODE_CACHE_PATH):
        self.cache_path = cache_path
        self._cache: dict = {}
        if cache_path.exists():
            self._cache = json.loads(cache_path.read_text())

    def geocode(self, address: str, fallback_query: str | None = None) -> tuple[float, float] | None:
        if address in self._cache:
            cached = self._cache[address]
            return tuple(cached) if cached else None

        coords = self._query(address)

        if coords is None:
            spelled_out = _spell_out_ordinals(address)
            if spelled_out:
                coords = self._query(spelled_out)

        if coords is None and fallback_query and fallback_query != address:
            coords = self._query(fallback_query)
            if coords:
                print(f"  approximate location used for {address!r} -> {fallback_query!r}", file=sys.stderr)

        self._cache[address] = list(coords) if coords else None
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(self._cache, indent=2))
        return coords

    def _query(self, address: str) -> tuple[float, float] | None:
        resp = requests.get(
            GEOCODE_URL,
            params={"q": address, "format": "json", "limit": 1},
            headers={"User-Agent": f"spordle-games-script/1.0 ({USER_AGENT})"},
        )
        resp.raise_for_status()
        results = resp.json()
        time.sleep(1.0)  # Nominatim's usage policy: max ~1 request/sec
        return (float(results[0]["lat"]), float(results[0]["lon"])) if results else None


class RoutingClient:
    """Real road driving distance between two coordinates, via OSRM's free public demo router.

    Straight-line (haversine) distance is misleading around here - e.g. North
    Vancouver is close as the crow flies across Burrard Inlet but a long drive
    around it. This calls the public router.project-osrm.org demo server
    (no API key), which is fine for occasional personal lookups but isn't
    meant for heavy/production traffic - results are cached to
    data/.driving_cache.json so a rerun over the same rinks costs nothing.
    """

    def __init__(self, cache_path: Path = ROUTING_CACHE_PATH):
        self.cache_path = cache_path
        self._cache: dict = {}
        if cache_path.exists():
            self._cache = json.loads(cache_path.read_text())

    def driving_km(self, origin: tuple[float, float], dest: tuple[float, float]) -> float | None:
        key = f"{origin[0]:.5f},{origin[1]:.5f}|{dest[0]:.5f},{dest[1]:.5f}"
        if key in self._cache:
            return self._cache[key]
        url = f"{ROUTING_URL}/{origin[1]},{origin[0]};{dest[1]},{dest[0]}"
        resp = requests.get(
            url,
            params={"overview": "false"},
            headers={"User-Agent": f"spordle-games-script/1.0 ({USER_AGENT})"},
        )
        resp.raise_for_status()
        data = resp.json()
        routes = data.get("routes") if data.get("code") == "Ok" else None
        km = round(routes[0]["distance"] / 1000, 1) if routes else None
        self._cache[key] = km
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(self._cache, indent=2))
        time.sleep(0.5)  # be polite to the free demo server
        return km


def compute_distances(surfaces: dict, home: str, driving: bool) -> dict:
    """arenaId -> distance in km from `home`, driving or straight-line."""
    geocoder = Geocoder()
    home_coords = geocoder.geocode(home)
    if not home_coords:
        sys.exit(f"Could not geocode home address: {home!r} - try adding city/province/postal code")
    print(f"Home geocoded to {home_coords}", file=sys.stderr)

    router = RoutingClient() if driving else None
    distances: dict = {}
    for arena_id, surface in surfaces.items():
        v = surface.get("venue") or {}
        address = ", ".join(filter(None, [v.get("address"), v.get("city"), v.get("region"), v.get("country")]))
        fallback = ", ".join(filter(None, [v.get("name"), v.get("city"), v.get("region"), v.get("country")]))
        coords = geocoder.geocode(address, fallback_query=fallback) if address else None
        if coords is None:
            print(f"  could not geocode: {address!r} ({v.get('name')})", file=sys.stderr)
            distances[arena_id] = None
            continue
        distances[arena_id] = router.driving_km(home_coords, coords) if driving else round(haversine_km(*home_coords, *coords), 1)
    return distances


def build_games(
    raw_games: list[dict],
    surfaces: dict,
    teams: dict,
    groups: dict,
    statuses: dict,
    distances: dict | None = None,
) -> list[Game]:
    out = []
    for g in raw_games:
        status = statuses.get(g["id"], {})
        officials = status.get("officials", {})
        surface = surfaces.get(g.get("arenaId"))
        venue = f"{surface['venue']['name']} ({surface.get('name')})" if surface else f"arena#{g.get('arenaId')}"
        home = teams.get(g.get("homeTeamId"), {}).get("name") or "TBD"
        away = teams.get(g.get("awayTeamId"), {}).get("name") or "TBD"
        group = groups.get(g.get("groupId"), {}).get("name", "") if g.get("groupId") else ""
        out.append(
            Game(
                id=g["id"],
                number=g["number"],
                date=g["date"],
                division=g["division"],
                category=g.get("category"),
                home=home,
                away=away,
                venue=venue,
                group=group,
                game_status=status.get("status"),
                all_assigned=officials.get("allAssigned"),
                unassigned=officials.get("unassigned"),
                referees=officials.get("referees"),
                linespersons=officials.get("linespersons"),
                distance_km=(distances or {}).get(g.get("arenaId")),
            )
        )
    return out


def write_rows(rows: list[dict], output: Path, fmt: str) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "json":
        output.write_text(json.dumps(rows, indent=2))
    else:
        with output.open("w", newline="") as f:
            fieldnames = list(rows[0].keys()) if rows else []
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)


def write_output(games: list[Game], output: Path, fmt: str) -> None:
    write_rows([g.__dict__ for g in games], output, fmt)


def rinks_by_distance(rinks: list[dict], home: str, driving: bool = False) -> list[dict]:
    surfaces = {s["id"]: s for s in rinks}
    distances = compute_distances(surfaces, home, driving)

    rows = []
    for arena_id, surface in surfaces.items():
        v = surface.get("venue") or {}
        rows.append(
            {
                "distance_km": distances.get(arena_id),
                "arena_id": arena_id,
                "venue": v.get("name"),
                "rink": surface.get("name"),
                "address": v.get("address"),
                "city": v.get("city"),
                "region": v.get("region"),
            }
        )
    rows.sort(key=lambda r: (r["distance_km"] is None, r["distance_km"]))
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--username", default=os.environ.get("SPORDLE_USERNAME"))
    parser.add_argument("--password", default=os.environ.get("SPORDLE_PASSWORD"))
    parser.add_argument("--season", default=None, help="e.g. 2026-27; defaults to the active identity's current season")
    parser.add_argument("--from-date", default=None, help="YYYY-MM-DD, default = today")
    parser.add_argument(
        "--identity",
        default=os.environ.get("SPORDLE_UUID"),
        help="identity UUID to switch to; see --list-identities (or set SPORDLE_UUID)",
    )
    parser.add_argument("--list-identities", action="store_true", help="print available identities and exit")
    parser.add_argument("--city", default=None, help="only games at rinks in this city (substring, case-insensitive)")
    parser.add_argument("--rink", default=None, help="only games at rinks whose venue name matches this (substring, case-insensitive)")
    parser.add_argument("--list-rinks", action="store_true", help="print matching rinks and exit instead of fetching games")
    parser.add_argument(
        "--home",
        default=os.environ.get("SPORDLE_HOME"),
        help="your address (or SPORDLE_HOME env var) - sorts rinks/games by distance using free OSM geocoding",
    )
    parser.add_argument(
        "--max-distance-km",
        type=float,
        default=None,
        help="only rinks/games within this many km of --home; uses real driving distance (OSRM), not straight-line",
    )
    parser.add_argument(
        "--driving",
        action="store_true",
        help="with --list-rinks and no --max-distance-km, sort by driving distance instead of straight-line",
    )
    parser.add_argument("--format", choices=["csv", "json"], default="csv")
    parser.add_argument("--output", default=None, help="output path; default data/<date>_games.<format>")
    args = parser.parse_args()

    if not args.username or not args.password:
        sys.exit("Set SPORDLE_USERNAME/SPORDLE_PASSWORD env vars (or --username/--password)")

    client = SpordleClient(args.username, args.password)
    client.login()
    account = client.current_account()

    if args.list_identities:
        for ident in account["identities"]:
            marker = " (primary)" if ident.get("isPrimary") else ""
            print(f"{ident['id']}  {ident['participant']['fullName']}  |  {ident['tenant']['name']}{marker}")
        return

    identities = account.get("identities", [])
    if args.identity:
        client.set_identity(args.identity)
        account = client.current_account()
    elif identities:
        primary = next((i for i in identities if i.get("isPrimary")), identities[0])
        client.set_identity(primary["id"])
        account = client.current_account()

    season = args.season or account.get("seasonId")
    from_date = args.from_date or date.today().isoformat()

    if args.max_distance_km is not None and not args.home:
        sys.exit("--max-distance-km requires --home")

    if args.list_rinks:
        if args.city or args.rink:
            matched = client.find_arenas(city=args.city, venue_name=args.rink)
            if not matched:
                sys.exit(f"No rinks matched city={args.city!r} rink={args.rink!r}")
        else:
            print("No --city/--rink given - deriving rink list from this season's games...", file=sys.stderr)
            raw_games = client.games(season, from_date)
            season_arena_ids = {g["arenaId"] for g in raw_games if g.get("arenaId")}
            matched = client.lookup("surfaces", season_arena_ids)
            if not matched:
                sys.exit("No rinks found in this season's games")

        if args.home:
            driving = args.driving or args.max_distance_km is not None
            rows = rinks_by_distance(list(matched.values()), args.home, driving=driving)
            if args.max_distance_km is not None:
                before = len(rows)
                rows = [r for r in rows if r["distance_km"] is not None and r["distance_km"] <= args.max_distance_km]
                print(f"Filtered to {len(rows)}/{before} rinks within {args.max_distance_km} km driving distance", file=sys.stderr)
        else:
            rows = [
                {
                    "arena_id": s["id"],
                    "venue": s["venue"].get("name"),
                    "rink": s.get("name"),
                    "address": s["venue"].get("address"),
                    "city": s["venue"].get("city"),
                    "region": s["venue"].get("region"),
                }
                for s in matched.values()
            ]

        if args.output or args.format == "json":
            output = Path(args.output) if args.output else Path(f"data/{date.today().isoformat()}_rinks.{args.format}")
            write_rows(rows, output, args.format)
            print(f"Wrote {len(rows)} rinks to {output}", file=sys.stderr)
        else:
            for r in rows:
                prefix = f"{r['distance_km']:>6.1f} km  " if r.get("distance_km") is not None else ""
                print(f"{prefix}{r['venue']} ({r['rink']})  -  {r['address']}, {r['city']}, {r['region']}")
        return

    rink_arena_ids = None
    if args.city or args.rink:
        matched = client.find_arenas(city=args.city, venue_name=args.rink)
        if not matched:
            sys.exit(f"No rinks matched city={args.city!r} rink={args.rink!r}")
        rink_arena_ids = set(matched.keys())
        print(f"Matched {len(rink_arena_ids)} rink(s) for city={args.city!r} rink={args.rink!r}", file=sys.stderr)

    print(f"Fetching games for season {season} from {from_date}...", file=sys.stderr)
    raw_games = client.games(season, from_date, arena_ids=rink_arena_ids)
    print(f"Fetched {len(raw_games)} games", file=sys.stderr)

    arena_ids = {g["arenaId"] for g in raw_games if g.get("arenaId")}
    surfaces = client.lookup("surfaces", arena_ids)

    distances = None
    if args.max_distance_km is not None:
        distances = compute_distances(surfaces, args.home, driving=True)
        before = len(raw_games)
        raw_games = [
            g
            for g in raw_games
            if (d := distances.get(g.get("arenaId"))) is not None and d <= args.max_distance_km
        ]
        print(
            f"Filtered to {len(raw_games)}/{before} games within {args.max_distance_km} km driving distance of home",
            file=sys.stderr,
        )

    team_ids = {t for g in raw_games for t in (g.get("homeTeamId"), g.get("awayTeamId")) if t}
    group_ids = {g["groupId"] for g in raw_games if g.get("groupId")}
    game_ids = {g["id"] for g in raw_games}

    teams = client.lookup("teams", team_ids)
    groups = client.lookup("groups", group_ids)
    statuses = client.lookup("gamestatuses", game_ids)

    games = build_games(raw_games, surfaces, teams, groups, statuses, distances=distances)

    output = Path(args.output) if args.output else Path(f"data/{date.today().isoformat()}_games.{args.format}")
    write_output(games, output, args.format)
    print(f"Wrote {len(games)} games to {output}", file=sys.stderr)


if __name__ == "__main__":
    main()

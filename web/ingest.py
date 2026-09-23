#!/usr/bin/env python3
"""Ingest approved PCAHA scoresheets into SQLite and rebuild rollups."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import requests

from db import (
    DEFAULT_DB,
    SCORESHEET_CACHE,
    connect,
    set_meta,
)

PUBLIC_API = "https://api.play.spordle.com/api"
PUBLIC_KEY = "13f2ae2dff59c0b501294a244626f32d2873034a"
PCAHA_OFFICE_ID = 15
PCAHA_TZ = ZoneInfo("America/Vancouver")
DEFAULT_DELAY = 0.2
PAGE_SIZE = 100

DURATION_PIM = {
    "minor": 2.0,
    "double_minor": 4.0,
    "doubleminor": 4.0,
    "major": 5.0,
    "misconduct": 10.0,
    "game_misconduct": 10.0,
    "gamemisconduct": 10.0,
    "match": 5.0,
    "penalty_shot": 0.0,
    "penaltyshot": 0.0,
    "bench_minor": 2.0,
    "benchminor": 2.0,
}

log = logging.getLogger("pcaha-ingest")


def session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
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
    return s


def filter_param(payload: dict) -> dict:
    return {"filter": json.dumps(payload, separators=(",", ":"))}


def public_get(s: requests.Session, path: str, params: Optional[dict] = None) -> Any:
    resp = s.get(f"{PUBLIC_API}{path}", params=params, timeout=60)
    resp.raise_for_status()
    return resp.json()


def pim_for_duration(duration: Optional[str]) -> float:
    if not duration:
        return 0.0
    key = str(duration).strip().lower().replace(" ", "_").replace("-", "_")
    if key in DURATION_PIM:
        return DURATION_PIM[key]
    # Fall back: extract leading number if present ("2 min", etc.)
    digits = "".join(ch if ch.isdigit() or ch == "." else " " for ch in key).split()
    if digits:
        try:
            return float(digits[0])
        except ValueError:
            return 0.0
    return 0.0


def upsert_schedule(conn, schedule: Optional[dict]) -> None:
    if not schedule or schedule.get("id") is None:
        return
    cat = schedule.get("category")
    if isinstance(cat, dict):
        cat = cat.get("name") or cat.get("id")
    conn.execute(
        """
        INSERT INTO schedules(id, name, type, division, gender, category, season_id, office_id)
        VALUES(?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
          name=excluded.name, type=excluded.type, division=excluded.division,
          gender=excluded.gender, category=excluded.category, season_id=excluded.season_id,
          office_id=excluded.office_id
        """,
        (
            schedule["id"],
            schedule.get("name"),
            schedule.get("type"),
            schedule.get("division"),
            schedule.get("gender"),
            cat,
            schedule.get("seasonId"),
            schedule.get("officeId"),
        ),
    )


def upsert_group(conn, group: Optional[dict]) -> None:
    if not group or group.get("id") is None:
        return
    conn.execute(
        """
        INSERT INTO groups(id, name, type, office_id)
        VALUES(?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
          name=excluded.name, type=excluded.type, office_id=excluded.office_id
        """,
        (group["id"], group.get("name"), group.get("type"), group.get("officeId")),
    )


def upsert_team(conn, team: dict) -> None:
    conn.execute(
        """
        INSERT INTO teams(id, name, short_name, office_id)
        VALUES(?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
          name=excluded.name, short_name=excluded.short_name, office_id=excluded.office_id
        """,
        (
            team["id"],
            team.get("name"),
            team.get("shortName") or team.get("abbreviation"),
            team.get("officeId"),
        ),
    )


def upsert_player(conn, participant: dict) -> None:
    pid = participant.get("id") or participant.get("participantId")
    if pid is None:
        return
    full = participant.get("fullName") or " ".join(
        filter(None, [participant.get("firstName"), participant.get("lastName")])
    )
    full = (full or "").strip()
    placeholder = f"Player {pid}"
    conn.execute(
        """
        INSERT INTO players(participant_id, full_name, first_name, last_name)
        VALUES(?,?,?,?)
        ON CONFLICT(participant_id) DO UPDATE SET
          full_name=CASE
            WHEN excluded.full_name IS NOT NULL
             AND excluded.full_name != ''
             AND excluded.full_name NOT LIKE 'Player %'
            THEN excluded.full_name
            ELSE players.full_name
          END,
          first_name=COALESCE(excluded.first_name, players.first_name),
          last_name=COALESCE(excluded.last_name, players.last_name)
        """,
        (
            pid,
            full or placeholder,
            participant.get("firstName"),
            participant.get("lastName"),
        ),
    )


def list_approved_games(
    s: requests.Session,
    season_id: str,
    day: Optional[str] = None,
    delay: float = DEFAULT_DELAY,
) -> list[dict]:
    clauses: list[dict] = [{"seasonId": season_id}, {"isApproved": True}]
    if day:
        clauses.append({"date": f"{day}T00:00:00.000Z"})
    where: dict[str, Any] = {"and": clauses, "effectiveOffices": PCAHA_OFFICE_ID}
    count = public_get(
        s, "/games/count", {"where": json.dumps(where, separators=(",", ":"))}
    ).get("count", 0)
    log.info("Approved games to consider: %s (day=%s)", count, day or "all")
    out: list[dict] = []
    skip = 0
    while skip < count or (count == 0 and skip == 0 and not out):
        batch = public_get(
            s,
            "/games",
            filter_param(
                {
                    "where": where,
                    "include": ["schedule", "group", "teamStats"],
                    "order": ["date ASC", "id ASC"],
                    "limit": PAGE_SIZE,
                    "skip": skip,
                }
            ),
        )
        if not batch:
            break
        out.extend(batch)
        skip += len(batch)
        time.sleep(delay)
        if len(batch) < PAGE_SIZE:
            break
    return out


def fetch_teams(s: requests.Session, team_ids: set[int], delay: float) -> dict[int, dict]:
    teams: dict[int, dict] = {}
    ids = sorted(team_ids)
    for i in range(0, len(ids), 80):
        chunk = ids[i : i + 80]
        rows = public_get(
            s,
            "/teams",
            filter_param({"where": {"id": {"inq": chunk}}, "scope": "Tenant"}),
        )
        for t in rows:
            teams[t["id"]] = t
        time.sleep(delay)
    return teams


def scoresheet_cache_path(game_id: int) -> Path:
    return SCORESHEET_CACHE / f"{game_id}.json"


def load_cached_scoresheet(game_id: int) -> Optional[dict]:
    path = scoresheet_cache_path(game_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def save_cached_scoresheet(game_id: int, payload: dict) -> None:
    path = scoresheet_cache_path(game_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


def fetch_scoresheet(
    s: requests.Session, game_id: int, delay: float, force: bool = False
) -> dict:
    if not force:
        cached = load_cached_scoresheet(game_id)
        if cached is not None:
            return cached
    payload = public_get(s, f"/games/{game_id}/scoresheet")
    save_cached_scoresheet(game_id, payload)
    time.sleep(delay)
    return payload


def clear_game_events(conn, game_id: int) -> None:
    for table in (
        "assists",
        "goals",
        "penalties",
        "goalie_stints",
        "lineup_entries",
        "team_game_stats",
    ):
        if table == "assists":
            conn.execute(
                "DELETE FROM assists WHERE goal_id IN (SELECT id FROM goals WHERE game_id=?)",
                (game_id,),
            )
        else:
            conn.execute(f"DELETE FROM {table} WHERE game_id=?", (game_id,))


def mark_game_winners(goals: list[dict], home_id: int, away_id: int, home_score: int, away_score: int) -> set[str]:
    """Return goal ids that are game-winners (winning team's last goal that made the lead permanent)."""
    if home_score == away_score:
        return set()
    winner_id = home_id if home_score > away_score else away_id
    winning_goals = [g for g in goals if g.get("teamId") == winner_id]
    if not winning_goals:
        return set()

    def sort_key(g: dict) -> tuple:
        gt = g.get("gameTime") or {}
        period = str(gt.get("period") or "0")
        try:
            pnum = int(period)
        except ValueError:
            pnum = 99 if period.upper() in {"OT", "SO"} else 50
        return (
            pnum,
            int(gt.get("elapsedMinutes") or 0),
            int(gt.get("elapsedSeconds") or 0),
            int(gt.get("minutes") or 0),
            int(gt.get("seconds") or 0),
        )

    winning_goals.sort(key=sort_key)
    # Approximate: the goal that put the winner ahead for good — last goal by winner
    # after which they never trailed. Simplified: last goal by the winning team.
    last = winning_goals[-1]
    gid = last.get("id")
    return {gid} if gid else set()


def ingest_game(
    conn,
    s: requests.Session,
    game: dict,
    teams: dict[int, dict],
    delay: float,
    force_scoresheet: bool = False,
) -> bool:
    game_id = game["id"]
    existing = conn.execute(
        "SELECT ingested_at, scoresheet_status, is_approved FROM games WHERE id=?",
        (game_id,),
    ).fetchone()
    sheet_status = game.get("scoresheetStatus") or (game.get("status") if game.get("isApproved") else None)
    if (
        existing
        and existing["is_approved"]
        and not force_scoresheet
        and scoresheet_cache_path(game_id).exists()
        and existing["scoresheet_status"] == (sheet_status or "approved")
    ):
        # Still refresh team stats / score in case they changed
        pass

    payload = fetch_scoresheet(s, game_id, delay, force=force_scoresheet)
    sheet = payload.get("scoresheet") or payload
    status = sheet.get("status") or sheet_status or ("approved" if game.get("isApproved") else None)

    upsert_schedule(conn, game.get("schedule"))
    upsert_group(conn, game.get("group"))
    for tid in (game.get("homeTeamId"), game.get("awayTeamId")):
        if tid and tid in teams:
            upsert_team(conn, teams[tid])

    stats_by_team = {s.get("teamId"): s for s in (game.get("teamStats") or [])}
    home_id = game.get("homeTeamId")
    away_id = game.get("awayTeamId")
    home_stats = stats_by_team.get(home_id) or {}
    away_stats = stats_by_team.get(away_id) or {}
    home_score = home_stats.get("goalFor")
    away_score = away_stats.get("goalFor")
    # Fallback: count goals from scoresheet
    goals_raw = sheet.get("goals") or []
    if home_score is None or away_score is None:
        home_score = sum(1 for g in goals_raw if g.get("teamId") == home_id and not g.get("isOwnGoal"))
        away_score = sum(1 for g in goals_raw if g.get("teamId") == away_id and not g.get("isOwnGoal"))

    clear_game_events(conn, game_id)
    conn.execute(
        """
        INSERT INTO games(
          id, number, date, season_id, schedule_id, group_id, division, gender,
          home_team_id, away_team_id, home_score, away_score, is_approved,
          scoresheet_status, updated_at, ingested_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
          number=excluded.number, date=excluded.date, season_id=excluded.season_id,
          schedule_id=excluded.schedule_id, group_id=excluded.group_id,
          division=excluded.division, gender=excluded.gender,
          home_team_id=excluded.home_team_id, away_team_id=excluded.away_team_id,
          home_score=excluded.home_score, away_score=excluded.away_score,
          is_approved=excluded.is_approved, scoresheet_status=excluded.scoresheet_status,
          updated_at=excluded.updated_at, ingested_at=excluded.ingested_at
        """,
        (
            game_id,
            game.get("number"),
            (game.get("date") or "")[:10],
            game.get("seasonId"),
            game.get("scheduleId"),
            game.get("groupId"),
            game.get("division"),
            game.get("gender"),
            home_id,
            away_id,
            home_score,
            away_score,
            1 if game.get("isApproved") else 0,
            status,
            game.get("updated") or game.get("updatedAt"),
            datetime.now(tz=PCAHA_TZ).isoformat(),
        ),
    )

    for tid, stats in stats_by_team.items():
        conn.execute(
            """
            INSERT INTO team_game_stats(
              game_id, team_id, points, goal_for, goal_against, pim, sportsmanship,
              game_result, win_loss_type
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                game_id,
                tid,
                stats.get("points"),
                stats.get("goalFor"),
                stats.get("goalAgainst"),
                stats.get("pim"),
                stats.get("sportsmanship"),
                stats.get("gameResult"),
                stats.get("winLossType"),
            ),
        )

    lineups = sheet.get("lineups") or {}
    for team_key, lineup in lineups.items():
        try:
            team_id = int(lineup.get("teamId") or team_key)
        except (TypeError, ValueError):
            continue
        seen: set[int] = set()
        for member in lineup.get("members") or []:
            part = member.get("participant") or {}
            pid = member.get("participantId") or part.get("id")
            if pid is None or pid in seen:
                continue
            seen.add(pid)
            upsert_player(conn, {**part, "id": pid})
            positions = member.get("positions") or []
            conn.execute(
                """
                INSERT INTO lineup_entries(
                  game_id, team_id, participant_id, number, positions,
                  is_affiliate, is_starter, dressed
                ) VALUES(?,?,?,?,?,?,?,1)
                """,
                (
                    game_id,
                    team_id,
                    pid,
                    member.get("number"),
                    ",".join(positions) if isinstance(positions, list) else str(positions or ""),
                    1 if member.get("isAffiliate") else 0,
                    1 if member.get("isStarter") else 0,
                ),
            )

    gwg_ids = mark_game_winners(
        goals_raw,
        home_id or 0,
        away_id or 0,
        int(home_score or 0),
        int(away_score or 0),
    )
    for goal in goals_raw:
        gid = goal.get("id")
        if not gid:
            continue
        part_id = goal.get("participantId")
        if part_id:
            upsert_player(conn, {"id": part_id})
        gt = goal.get("gameTime") or {}
        conn.execute(
            """
            INSERT INTO goals(
              id, game_id, team_id, participant_id, period, minutes, seconds,
              is_powerplay, is_shorthanded, is_empty_net, is_penalty_shot, is_game_winner
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                gid,
                game_id,
                goal.get("teamId"),
                part_id,
                str(gt.get("period")) if gt.get("period") is not None else None,
                gt.get("minutes"),
                gt.get("seconds"),
                1 if goal.get("isPowerplay") else 0,
                1 if goal.get("isShorthanded") else 0,
                1 if goal.get("isEmptyNet") else 0,
                1 if goal.get("isPenaltyShot") else 0,
                1 if gid in gwg_ids else 0,
            ),
        )
        for i, aid in enumerate(goal.get("assistIds") or [], start=1):
            if aid is None:
                continue
            upsert_player(conn, {"id": aid})
            conn.execute(
                "INSERT OR IGNORE INTO assists(goal_id, participant_id, ordinal) VALUES(?,?,?)",
                (gid, aid, i),
            )

    for pen in sheet.get("penalties") or []:
        pid = pen.get("id")
        if not pid:
            continue
        part_id = pen.get("participantId")
        if part_id:
            upsert_player(conn, {"id": part_id})
        gt = pen.get("gameTime") or {}
        duration = pen.get("duration")
        conn.execute(
            """
            INSERT INTO penalties(
              id, game_id, team_id, participant_id, duration, pim, infraction,
              period, minutes, seconds
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                pid,
                game_id,
                pen.get("teamId"),
                part_id,
                duration,
                pim_for_duration(duration),
                pen.get("infraction"),
                str(gt.get("period")) if gt.get("period") is not None else None,
                gt.get("minutes"),
                gt.get("seconds"),
            ),
        )

    for stint in sheet.get("goalies") or []:
        part_id = stint.get("onParticipantId") or stint.get("participantId")
        if part_id is None or stint.get("teamId") is None:
            continue
        upsert_player(conn, {"id": part_id})
        gt = stint.get("gameTime") or {}
        conn.execute(
            """
            INSERT OR IGNORE INTO goalie_stints(
              game_id, team_id, participant_id, is_starter, period, minutes, seconds
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (
                game_id,
                stint.get("teamId"),
                part_id,
                1 if stint.get("isStarter") else 0,
                str(gt.get("period")) if gt.get("period") is not None else None,
                gt.get("minutes"),
                gt.get("seconds"),
            ),
        )

    return True


def rebuild_rollups(conn, season_id: Optional[str] = None) -> None:
    if season_id:
        conn.execute("DELETE FROM standings WHERE season_id=?", (season_id,))
        conn.execute("DELETE FROM player_stats WHERE season_id=?", (season_id,))
        season_filter = "AND g.season_id = ?"
        params: tuple = (season_id,)
    else:
        conn.execute("DELETE FROM standings")
        conn.execute("DELETE FROM player_stats")
        season_filter = ""
        params = ()

    # Standings from team_game_stats joined to games
    conn.execute(
        f"""
        INSERT INTO standings(
          season_id, schedule_id, group_id, team_id, team_name,
          gp, w, l, t, otl, pts, gf, ga, gd, pim, sportsmanship
        )
        SELECT
          g.season_id,
          g.schedule_id,
          COALESCE(g.group_id, 0),
          tgs.team_id,
          COALESCE(t.name, 'Team ' || tgs.team_id),
          COUNT(*),
          SUM(CASE WHEN lower(COALESCE(tgs.game_result,'')) = 'win' THEN 1 ELSE 0 END),
          SUM(CASE
                WHEN lower(COALESCE(tgs.game_result,'')) = 'loss'
                 AND lower(COALESCE(tgs.win_loss_type,'')) NOT IN ('ot','overtime','so','shootout')
                THEN 1 ELSE 0 END),
          SUM(CASE WHEN lower(COALESCE(tgs.game_result,'')) IN ('tie','draw') THEN 1 ELSE 0 END),
          SUM(CASE
                WHEN lower(COALESCE(tgs.game_result,'')) = 'loss'
                 AND lower(COALESCE(tgs.win_loss_type,'')) IN ('ot','overtime','so','shootout')
                THEN 1 ELSE 0 END),
          COALESCE(SUM(tgs.points), 0),
          COALESCE(SUM(tgs.goal_for), 0),
          COALESCE(SUM(tgs.goal_against), 0),
          COALESCE(SUM(tgs.goal_for), 0) - COALESCE(SUM(tgs.goal_against), 0),
          COALESCE(SUM(tgs.pim), 0),
          COALESCE(SUM(tgs.sportsmanship), 0)
        FROM team_game_stats tgs
        JOIN games g ON g.id = tgs.game_id
        LEFT JOIN teams t ON t.id = tgs.team_id
        WHERE g.is_approved = 1 AND g.schedule_id IS NOT NULL {season_filter}
        GROUP BY g.season_id, g.schedule_id, COALESCE(g.group_id, 0), tgs.team_id
        """,
        params,
    )

    # Base player rows from lineup appearances
    conn.execute(
        f"""
        INSERT INTO player_stats(
          season_id, schedule_id, team_id, participant_id, player_name, number, positions,
          is_affiliate, is_goalie, gp, affiliate_gp
        )
        SELECT
          g.season_id,
          g.schedule_id,
          le.team_id,
          le.participant_id,
          COALESCE(p.full_name, 'Player ' || le.participant_id),
          MAX(le.number),
          GROUP_CONCAT(DISTINCT le.positions),
          MAX(le.is_affiliate),
          0,
          COUNT(DISTINCT le.game_id),
          SUM(CASE WHEN le.is_affiliate = 1 THEN 1 ELSE 0 END)
        FROM lineup_entries le
        JOIN games g ON g.id = le.game_id
        LEFT JOIN players p ON p.participant_id = le.participant_id
        WHERE g.is_approved = 1 AND g.schedule_id IS NOT NULL {season_filter}
        GROUP BY g.season_id, g.schedule_id, le.team_id, le.participant_id
        """,
        params,
    )

    # Skater counting stats
    conn.execute(
        f"""
        UPDATE player_stats
        SET
          g = COALESCE((
            SELECT COUNT(*) FROM goals go
            JOIN games g ON g.id = go.game_id
            WHERE g.season_id = player_stats.season_id
              AND g.schedule_id = player_stats.schedule_id
              AND go.team_id = player_stats.team_id
              AND go.participant_id = player_stats.participant_id
              AND g.is_approved = 1
          ), 0),
          a = COALESCE((
            SELECT COUNT(*) FROM assists a
            JOIN goals go ON go.id = a.goal_id
            JOIN games g ON g.id = go.game_id
            WHERE g.season_id = player_stats.season_id
              AND g.schedule_id = player_stats.schedule_id
              AND go.team_id = player_stats.team_id
              AND a.participant_id = player_stats.participant_id
              AND g.is_approved = 1
          ), 0),
          ppg = COALESCE((
            SELECT COUNT(*) FROM goals go
            JOIN games g ON g.id = go.game_id
            WHERE g.season_id = player_stats.season_id
              AND g.schedule_id = player_stats.schedule_id
              AND go.team_id = player_stats.team_id
              AND go.participant_id = player_stats.participant_id
              AND go.is_powerplay = 1 AND g.is_approved = 1
          ), 0),
          shg = COALESCE((
            SELECT COUNT(*) FROM goals go
            JOIN games g ON g.id = go.game_id
            WHERE g.season_id = player_stats.season_id
              AND g.schedule_id = player_stats.schedule_id
              AND go.team_id = player_stats.team_id
              AND go.participant_id = player_stats.participant_id
              AND go.is_shorthanded = 1 AND g.is_approved = 1
          ), 0),
          gwg = COALESCE((
            SELECT COUNT(*) FROM goals go
            JOIN games g ON g.id = go.game_id
            WHERE g.season_id = player_stats.season_id
              AND g.schedule_id = player_stats.schedule_id
              AND go.team_id = player_stats.team_id
              AND go.participant_id = player_stats.participant_id
              AND go.is_game_winner = 1 AND g.is_approved = 1
          ), 0),
          pim = COALESCE((
            SELECT SUM(pe.pim) FROM penalties pe
            JOIN games g ON g.id = pe.game_id
            WHERE g.season_id = player_stats.season_id
              AND g.schedule_id = player_stats.schedule_id
              AND pe.team_id = player_stats.team_id
              AND pe.participant_id = player_stats.participant_id
              AND g.is_approved = 1
          ), 0)
        WHERE 1=1 {('AND season_id = ?' if season_id else '')}
        """,
        params,
    )
    conn.execute(
        f"""
        UPDATE player_stats SET p = COALESCE(g,0) + COALESCE(a,0)
        WHERE 1=1 {('AND season_id = ?' if season_id else '')}
        """,
        params,
    )

    # Goalie flags + GP from stints
    conn.execute(
        f"""
        UPDATE player_stats
        SET is_goalie = 1,
            goalie_gp = (
              SELECT COUNT(DISTINCT gs.game_id)
              FROM goalie_stints gs
              JOIN games g ON g.id = gs.game_id
              WHERE g.season_id = player_stats.season_id
                AND g.schedule_id = player_stats.schedule_id
                AND gs.team_id = player_stats.team_id
                AND gs.participant_id = player_stats.participant_id
                AND g.is_approved = 1
            )
        WHERE EXISTS (
          SELECT 1 FROM goalie_stints gs
          JOIN games g ON g.id = gs.game_id
          WHERE g.season_id = player_stats.season_id
            AND g.schedule_id = player_stats.schedule_id
            AND gs.team_id = player_stats.team_id
            AND gs.participant_id = player_stats.participant_id
            AND g.is_approved = 1
        )
        {('AND season_id = ?' if season_id else '')}
        """,
        params,
    )
    # Also mark G position from lineup
    conn.execute(
        f"""
        UPDATE player_stats SET is_goalie = 1
        WHERE (',' || REPLACE(UPPER(COALESCE(positions,'')), ' ', '') || ',') LIKE '%,G,%'
           OR UPPER(COALESCE(positions,'')) LIKE '%GOALIE%'
           OR UPPER(COALESCE(positions,'')) LIKE '%GOALTENDER%'
        {('AND season_id = ?' if season_id else '')}
        """,
        params,
    )

    # Goalie W/L/T and GA: credit last goalie of record per team/game
    rows = conn.execute(
        f"""
        SELECT g.id AS game_id, g.season_id, g.schedule_id, g.home_team_id, g.away_team_id,
               g.home_score, g.away_score, tgs.team_id, tgs.game_result, tgs.goal_against
        FROM games g
        JOIN team_game_stats tgs ON tgs.game_id = g.id
        WHERE g.is_approved = 1 AND g.schedule_id IS NOT NULL {season_filter}
        """,
        params,
    ).fetchall()
    for row in rows:
        stints = conn.execute(
            """
            SELECT participant_id, is_starter, period, minutes, seconds
            FROM goalie_stints
            WHERE game_id=? AND team_id=?
            ORDER BY is_starter DESC, CAST(period AS INTEGER), minutes DESC, seconds DESC
            """,
            (row["game_id"], row["team_id"]),
        ).fetchall()
        if not stints:
            continue
        # Prefer the chronologically last change; fallback starter
        def stint_key(s):
            try:
                p = int(s["period"] or 0)
            except (TypeError, ValueError):
                p = 0
            return (p, s["minutes"] or 0, s["seconds"] or 0, s["is_starter"] or 0)

        last = sorted(stints, key=stint_key)[-1]
        pid = last["participant_id"]
        result = (row["game_result"] or "").lower()
        w = 1 if result == "win" else 0
        l = 1 if result == "loss" else 0
        t = 1 if result in {"tie", "draw"} else 0
        ga = row["goal_against"] or 0
        conn.execute(
            """
            UPDATE player_stats
            SET goalie_w = goalie_w + ?, goalie_l = goalie_l + ?, goalie_t = goalie_t + ?,
                ga = ga + ?, is_goalie = 1
            WHERE season_id=? AND schedule_id=? AND team_id=? AND participant_id=?
            """,
            (w, l, t, ga, row["season_id"], row["schedule_id"], row["team_id"], pid),
        )

    conn.execute(
        f"""
        UPDATE player_stats
        SET gaa = CASE WHEN goalie_gp > 0 THEN CAST(ga AS REAL) / goalie_gp ELSE NULL END
        WHERE is_goalie = 1 {('AND season_id = ?' if season_id else '')}
        """,
        params,
    )
    # Keep rollup display names in sync with the players table.
    conn.execute(
        """
        UPDATE player_stats
        SET player_name = (
          SELECT full_name FROM players p WHERE p.participant_id = player_stats.participant_id
        )
        WHERE EXISTS (
          SELECT 1 FROM players p
          WHERE p.participant_id = player_stats.participant_id
            AND p.full_name IS NOT NULL AND p.full_name != ''
        )
        """
        + (" AND season_id = ?" if season_id else ""),
        params,
    )
    set_meta(conn, "rollups_rebuilt_at", datetime.now(tz=PCAHA_TZ).isoformat())


def run_ingest(
    season_id: str = "2026-27",
    day: Optional[str] = None,
    db_path: Optional[Path] = None,
    delay: float = DEFAULT_DELAY,
    force: bool = False,
    yesterday: bool = False,
) -> dict:
    if yesterday:
        day = (datetime.now(tz=PCAHA_TZ).date() - timedelta(days=1)).isoformat()
    conn = connect(db_path)
    s = session()
    games = list_approved_games(s, season_id, day=day, delay=delay)
    team_ids = {
        tid
        for g in games
        for tid in (g.get("homeTeamId"), g.get("awayTeamId"))
        if tid
    }
    teams = fetch_teams(s, team_ids, delay) if team_ids else {}
    ingested = 0
    errors = 0
    for i, game in enumerate(games, start=1):
        try:
            ingest_game(conn, s, game, teams, delay, force_scoresheet=force)
            ingested += 1
            if i % 25 == 0:
                conn.commit()
                log.info("Ingested %s / %s", i, len(games))
        except Exception:
            errors += 1
            log.exception("Failed game %s", game.get("id"))
    conn.commit()
    log.info("Rebuilding rollups for %s", season_id)
    rebuild_rollups(conn, season_id=season_id)
    conn.commit()
    set_meta(conn, "last_ingest_at", datetime.now(tz=PCAHA_TZ).isoformat())
    set_meta(conn, "last_ingest_season", season_id)
    set_meta(conn, "last_ingest_day", day or "")
    set_meta(conn, "last_ingest_count", str(ingested))
    conn.commit()
    summary = {
        "season_id": season_id,
        "day": day,
        "listed": len(games),
        "ingested": ingested,
        "errors": errors,
        "db": str(db_path or DEFAULT_DB),
    }
    log.info("Done: %s", summary)
    conn.close()
    return summary


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--season", default="2026-27")
    parser.add_argument("--day", help="YYYY-MM-DD; ingest only that date")
    parser.add_argument("--yesterday", action="store_true")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY)
    parser.add_argument("--force", action="store_true", help="Refetch scoresheets")
    parser.add_argument("--rebuild-only", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.rebuild_only:
        conn = connect(args.db)
        rebuild_rollups(conn, season_id=args.season)
        conn.commit()
        conn.close()
        log.info("Rollups rebuilt")
        return 0

    run_ingest(
        season_id=args.season,
        day=args.day,
        db_path=args.db,
        delay=args.delay,
        force=args.force,
        yesterday=args.yesterday,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

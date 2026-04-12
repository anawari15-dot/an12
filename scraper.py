#!/usr/bin/env python3
"""
WNBA 2024 Regular Season – DND (Did Not Dress) Injury / Illness Scraper
========================================================================
Fetches every 2024 WNBA regular-season game and records players listed
as DND (Did Not Dress) due to injury or illness.

Data source: ESPN public API (no API key required, no bot blocking)
  - Schedule:     site.api.espn.com  – all games May–Sep 2024
  - Game summary: site.api.espn.com  – per-game boxscore with
                  didNotPlay flag + reason for each player

Output files (written to the current directory):
    dnd_players_2024.json   – full detail, one record per player-game
    dnd_players_2024.csv    – same data in CSV

Usage
-----
    python3 scraper.py                        # full season
    python3 scraper.py --test                 # first 5 games only
    python3 scraper.py --resume dnd_players_2024.json   # skip done games
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
ESPN_SCOREBOARD = (
    "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/scoreboard"
)
ESPN_SUMMARY = (
    "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/summary"
)

# 2024 WNBA regular season window
SEASON_START = date(2024, 5, 14)
SEASON_END   = date(2024, 9, 19)

REQUEST_DELAY = 0.5   # seconds between requests

_SESSION = requests.Session()
_SESSION.headers.update(
    {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
    }
)

# ---------------------------------------------------------------------------
# DND / injury filter  –  conservative: require explicit injury keyword
# ---------------------------------------------------------------------------
_INJURY_KW = frozenset({
    "injury", "injured", "illness", "ill", "sick",
    "knee", "ankle", "foot", "hip", "shoulder", "back",
    "wrist", "hand", "finger", "hamstring", "quad", "quadriceps",
    "calf", "achilles", "concussion", "head", "neck", "groin",
    "shin", "elbow", "toe", "thigh", "rib", "abdomen", "abdominal",
    "soreness", "sore", "strain", "sprain", "fracture", "surgery",
    "recovery", "non-covid illness", "covid", "health and safety",
    "left leg", "right leg", "lower leg", "upper leg",
})
_EXCLUDE_KW = frozenset({
    "coach's decision", "coaches decision", "coach decision",
    "dnp - cd", "dnp-cd", "dnp cd",
    "suspended", "suspension",
    "trade", "waived",
    "g league", "g-league",
    "personal",
    "rest",
    "conditioning",
    "load management",
    "league excused",
    "league",
    "not injury",
})


def is_dnd_injury(reason: str) -> bool:
    """
    Return True ONLY if reason explicitly mentions an injury or illness.
    Blank / vague / non-injury reasons → False.
    """
    if not reason or reason.strip().lower() in ("", "not provided"):
        return False          # unknown reason → exclude
    low = reason.lower()
    for kw in _EXCLUDE_KW:
        if kw in low:
            return False      # explicitly non-injury
    for kw in _INJURY_KW:
        if kw in low:
            return True       # explicit injury/illness mention
    return False              # unrecognised reason → exclude


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------
def _get(url: str, params: dict | None = None, retries: int = 4) -> dict | None:
    for attempt in range(retries):
        try:
            r = _SESSION.get(url, params=params, timeout=30)
            if r.status_code == 200:
                return r.json()
            print(f"  [WARN] HTTP {r.status_code} → {url}", file=sys.stderr)
        except requests.RequestException as exc:
            print(f"  [ERR] {exc} (attempt {attempt + 1}/{retries})", file=sys.stderr)
        time.sleep(min(2 ** attempt, 16))
    return None


# ---------------------------------------------------------------------------
# 1. Schedule via ESPN scoreboard (iterate day by day)
# ---------------------------------------------------------------------------
def fetch_schedule() -> list[dict]:
    """
    Fetch all completed 2024 WNBA regular-season games from ESPN.
    Returns list of dicts: eventId, gameDate, homeTeam, awayTeam, status.
    """
    print("Fetching 2024 WNBA schedule from ESPN …")
    games: list[dict] = []
    seen: set[str] = set()

    current = SEASON_START
    while current <= SEASON_END:
        # date_str is the LOCAL calendar date — always use this as the game
        # date, never event["date"] which is UTC and shifts west-coast games.
        date_str = current.strftime("%Y-%m-%d")
        query_str = current.strftime("%Y%m%d")
        data = _get(ESPN_SCOREBOARD, params={"dates": query_str, "limit": 50})
        if data:
            for event in data.get("events", []):
                eid = event.get("id", "")
                if eid in seen:
                    continue          # already captured on a prior date query
                seen.add(eid)

                # Only completed games
                status_type = (
                    event.get("status", {})
                        .get("type", {})
                        .get("name", "")
                )
                if status_type not in ("STATUS_FINAL", "STATUS_FINAL_OT",
                                       "STATUS_FINAL_FORFEIT"):
                    continue

                competitions = event.get("competitions", [{}])
                comp = competitions[0] if competitions else {}
                competitors = comp.get("competitors", [])
                home = next((c for c in competitors if c.get("homeAway") == "home"), {})
                away = next((c for c in competitors if c.get("homeAway") == "away"), {})

                games.append({
                    "eventId":        eid,
                    "gameDate":       date_str,   # local date from query — always correct
                    "homeTeamAbbrev": home.get("team", {}).get("abbreviation", ""),
                    "awayTeamAbbrev": away.get("team", {}).get("abbreviation", ""),
                    "homeTeamName":   home.get("team", {}).get("displayName", ""),
                    "awayTeamName":   away.get("team", {}).get("displayName", ""),
                })
        current += timedelta(days=1)
        time.sleep(REQUEST_DELAY)

    print(f"  → {len(games)} completed regular-season games found.")
    return games


# ---------------------------------------------------------------------------
# 2. Per-game DND players via ESPN game summary
# ---------------------------------------------------------------------------
def fetch_dnd_players(event_id: str, game_date: str, matchup: str) -> list[dict]:
    """
    Fetch the ESPN game summary and extract players who did not play
    due to injury / illness.
    """
    data = _get(ESPN_SUMMARY, params={"event": event_id})
    if not data:
        return []

    rows: list[dict] = []

    # ESPN boxscore → players section
    boxscore = data.get("boxscore", {})
    for team_block in boxscore.get("players", []):
        team_info  = team_block.get("team", {})
        team_abbrev = team_info.get("abbreviation", "")

        for stat_block in team_block.get("statistics", []):
            for athlete_block in stat_block.get("athletes", []):
                did_not_play = athlete_block.get("didNotPlay", False)
                reason       = athlete_block.get("reason", "")
                active       = athlete_block.get("active", True)
                athlete      = athlete_block.get("athlete", {})
                player_name  = athlete.get("displayName", "")
                player_id    = str(athlete.get("id", ""))

                # Only include if the reason explicitly mentions injury/illness
                if (did_not_play or not active) and is_dnd_injury(reason):
                    rows.append({
                        "eventId":    event_id,
                        "gameDate":   game_date,
                        "matchup":    matchup,
                        "playerName": player_name,
                        "playerId":   player_id,
                        "team":       team_abbrev,
                        "status":     "DND",
                        "reason":     reason,
                        "source":     "ESPN_Boxscore",
                    })

    # ESPN injuries section (pre-game report, may have more detail)
    seen_ids = {r["playerId"] for r in rows}
    for team_block in data.get("injuries", []):
        team_abbrev = team_block.get("team", {}).get("abbreviation", "")
        for inj in team_block.get("injuries", []):
            status = inj.get("status", "")
            athlete = inj.get("athlete", {})
            pid     = str(athlete.get("id", ""))
            name    = athlete.get("displayName", "")
            details = inj.get("details", {})
            reason  = details.get("detail", details.get("type", ""))
            if "DND" in status.upper() and pid not in seen_ids:
                rows.append({
                    "eventId":    event_id,
                    "gameDate":   game_date,
                    "matchup":    matchup,
                    "playerName": name,
                    "playerId":   pid,
                    "team":       team_abbrev,
                    "status":     status,
                    "reason":     reason if reason else "Not provided",
                    "source":     "ESPN_InjuryReport",
                })

    return rows


# ---------------------------------------------------------------------------
# 3. Save outputs
# ---------------------------------------------------------------------------
def save_results(rows: list[dict], json_path: Path, csv_path: Path) -> None:
    with json_path.open("w") as f:
        json.dump(rows, f, indent=2)
    print(f"Saved JSON → {json_path}  ({len(rows)} rows)")

    fieldnames = ["gameDate", "matchup", "eventId",
                  "playerName", "playerId", "team",
                  "status", "reason", "source"]
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"Saved CSV  → {csv_path}  ({len(rows)} rows)")

    if rows:
        player_counts = Counter(
            f"{r['playerName']} ({r['team']})" for r in rows
        )
        game_counts = Counter(r["eventId"] for r in rows)
        print(f"\nGames with ≥1 DND-injury entry : {len(game_counts)}")
        print(f"Total DND player-game entries  : {len(rows)}")
        print("\nTop 25 most-frequently DND players:")
        for player, count in player_counts.most_common(25):
            print(f"  {count:>3}x  {player}")


# ---------------------------------------------------------------------------
# 4. Main
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scrape WNBA 2024 DND injury/illness players (ESPN API)."
    )
    parser.add_argument("--test", action="store_true",
                        help="Process only the first 5 games (smoke test)")
    parser.add_argument("--resume", metavar="JSON_FILE",
                        help="Skip event IDs already in this output file")
    parser.add_argument("--out-json", default="dnd_players_2024.json")
    parser.add_argument("--out-csv",  default="dnd_players_2024.csv")
    args = parser.parse_args()

    # Load resume data
    existing_rows: list[dict] = []
    skip_ids: set[str] = set()
    if args.resume:
        p = Path(args.resume)
        if p.exists():
            with p.open() as f:
                existing_rows = json.load(f)
            skip_ids = {r["eventId"] for r in existing_rows}
            print(f"Resuming: {len(skip_ids)} games already processed.")

    games = fetch_schedule()
    if not games:
        sys.exit("Could not retrieve schedule. Check your internet connection.")

    if args.test:
        games = [g for g in games if g["eventId"] not in skip_ids][:5]
        print(f"TEST MODE – processing {len(games)} games.")

    all_rows: list[dict] = list(existing_rows)
    total = len(games)

    for idx, game in enumerate(games, 1):
        eid     = game["eventId"]
        if eid in skip_ids:
            continue

        gdate   = game["gameDate"]
        matchup = f"{game['awayTeamAbbrev']} @ {game['homeTeamAbbrev']}"
        print(f"[{idx:>3}/{total}] {gdate}  {matchup}  (ESPN:{eid})")

        dnd = fetch_dnd_players(eid, gdate, matchup)
        if dnd:
            print(f"  → {len(dnd)} DND-injury player(s)")
            all_rows.extend(dnd)

        time.sleep(REQUEST_DELAY)

    print(f"\n{'='*60}")
    save_results(all_rows, Path(args.out_json), Path(args.out_csv))


if __name__ == "__main__":
    main()

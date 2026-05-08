#!/usr/bin/env python3
"""
WNBA Regular Season – Active Players Scraper
=============================================
Fetches every regular-season game for a given WNBA season and records
all players who actually played (non-injured, active participants).

Data source: ESPN public API (no API key required)

Output file:
    active_players_{YEAR}.csv

Usage
-----
    python3 scraper_active.py                   # 2024 season (default)
    python3 scraper_active.py --season 2021     # 2021 season
    python3 scraper_active.py --test            # first 5 games only
    python3 scraper_active.py --all-seasons     # all seasons 2021-2025
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
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

SEASON_DATES: dict[int, tuple[date, date]] = {
    2021: (date(2021, 5, 14), date(2021, 9, 19)),
    2022: (date(2022, 5,  6), date(2022, 9, 18)),
    2023: (date(2023, 5, 19), date(2023, 9, 17)),
    2024: (date(2024, 5, 14), date(2024, 9, 19)),
    2025: (date(2025, 5, 16), date(2025, 9, 19)),
}

REQUEST_DELAY = 0.5

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

CSV_FIELDS = [
    "season", "gameDate", "matchup", "eventId",
    "playerName", "playerId", "team", "homeAway",
    "minutes", "points", "rebounds", "assists",
    "steals", "blocks", "turnovers", "fouls",
    "fgm", "fga", "fg_pct",
    "3pm", "3pa", "3p_pct",
    "ftm", "fta", "ft_pct",
    "plusMinus",
]


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
# Schedule fetch
# ---------------------------------------------------------------------------
def fetch_schedule(season_year: int) -> list[dict]:
    season_start, season_end = SEASON_DATES[season_year]
    print(f"Fetching {season_year} WNBA schedule from ESPN …")
    games: list[dict] = []
    seen: set[str] = set()

    current = season_start
    while current <= season_end:
        date_str  = current.strftime("%Y-%m-%d")
        query_str = current.strftime("%Y%m%d")
        data = _get(ESPN_SCOREBOARD, params={"dates": query_str, "limit": 50})
        if data:
            for event in data.get("events", []):
                eid = event.get("id", "")
                if eid in seen:
                    continue
                seen.add(eid)

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
                    "gameDate":       date_str,
                    "homeTeamAbbrev": home.get("team", {}).get("abbreviation", ""),
                    "awayTeamAbbrev": away.get("team", {}).get("abbreviation", ""),
                })
        current += timedelta(days=1)
        time.sleep(REQUEST_DELAY)

    print(f"  → {len(games)} completed games found.")
    return games


# ---------------------------------------------------------------------------
# Parse stats array into named fields
# ---------------------------------------------------------------------------
def _parse_stats(keys: list[str], stats: list[str]) -> dict:
    """Map ESPN stat keys to values; return empty strings for missing."""
    kmap = {k.lower(): v for k, v in zip(keys, stats)}

    def g(key: str) -> str:
        return kmap.get(key, "")

    # ESPN WNBA keys (may vary slightly by season)
    min_val = g("min") or g("minutes")
    pts     = g("pts") or g("points")
    reb     = g("reb") or g("rebounds")
    ast     = g("ast") or g("assists")
    stl     = g("stl") or g("steals")
    blk     = g("blk") or g("blocks")
    to_val  = g("to")  or g("turnovers")
    pf      = g("pf")  or g("fouls")
    fgm     = g("fgm")
    fga     = g("fga")
    fg_pct  = g("fg%") or g("fgpct") or g("fg_pct")
    tpm     = g("3pm") or g("3ptm")
    tpa     = g("3pa") or g("3pta")
    tp_pct  = g("3p%") or g("3ppct") or g("3p_pct")
    ftm     = g("ftm")
    fta     = g("fta")
    ft_pct  = g("ft%") or g("ftpct") or g("ft_pct")
    pm      = g("+/-") or g("plusminus") or g("plus_minus")

    return {
        "minutes":  min_val,
        "points":   pts,
        "rebounds": reb,
        "assists":  ast,
        "steals":   stl,
        "blocks":   blk,
        "turnovers": to_val,
        "fouls":    pf,
        "fgm":      fgm,
        "fga":      fga,
        "fg_pct":   fg_pct,
        "3pm":      tpm,
        "3pa":      tpa,
        "3p_pct":   tp_pct,
        "ftm":      ftm,
        "fta":      fta,
        "ft_pct":   ft_pct,
        "plusMinus": pm,
    }


# ---------------------------------------------------------------------------
# Per-game active players
# ---------------------------------------------------------------------------
def fetch_active_players(
    event_id: str, game_date: str, matchup: str,
    home_abbrev: str, away_abbrev: str, season: int,
) -> list[dict]:
    data = _get(ESPN_SUMMARY, params={"event": event_id})
    if not data:
        return []

    rows: list[dict] = []
    boxscore = data.get("boxscore", {})

    for team_block in boxscore.get("players", []):
        team_info   = team_block.get("team", {})
        team_abbrev = team_info.get("abbreviation", "")
        home_away   = "home" if team_abbrev == home_abbrev else "away"

        for stat_block in team_block.get("statistics", []):
            keys = stat_block.get("keys", [])  # stat column names

            for athlete_block in stat_block.get("athletes", []):
                did_not_play = athlete_block.get("didNotPlay", False)
                active       = athlete_block.get("active", True)

                # Skip anyone who didn't play
                if did_not_play or not active:
                    continue

                athlete     = athlete_block.get("athlete", {})
                player_name = athlete.get("displayName", "")
                player_id   = str(athlete.get("id", ""))
                raw_stats   = athlete_block.get("stats", [])

                stat_row = _parse_stats(keys, raw_stats)

                rows.append({
                    "season":     f"{season}-{str(season + 1)[2:]}",
                    "gameDate":   game_date,
                    "matchup":    matchup,
                    "eventId":    event_id,
                    "playerName": player_name,
                    "playerId":   player_id,
                    "team":       team_abbrev,
                    "homeAway":   home_away,
                    **stat_row,
                })

    return rows


# ---------------------------------------------------------------------------
# Run one season
# ---------------------------------------------------------------------------
def run_season(season_year: int, test: bool = False) -> list[dict]:
    games = fetch_schedule(season_year)
    if not games:
        print(f"No games found for {season_year}.", file=sys.stderr)
        return []

    if test:
        games = games[:5]
        print(f"TEST MODE – processing {len(games)} games.")

    all_rows: list[dict] = []
    total = len(games)

    for idx, game in enumerate(games, 1):
        eid        = game["eventId"]
        gdate      = game["gameDate"]
        home_ab    = game["homeTeamAbbrev"]
        away_ab    = game["awayTeamAbbrev"]
        matchup    = f"{away_ab} @ {home_ab}"
        print(f"[{idx:>3}/{total}] {gdate}  {matchup}  (ESPN:{eid})")

        players = fetch_active_players(
            eid, gdate, matchup, home_ab, away_ab, season_year
        )
        print(f"  → {len(players)} active player(s)")
        all_rows.extend(players)
        time.sleep(REQUEST_DELAY)

    return all_rows


# ---------------------------------------------------------------------------
# Save CSV
# ---------------------------------------------------------------------------
def save_csv(rows: list[dict], path: Path) -> None:
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"Saved → {path}  ({len(rows)} rows)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scrape WNBA active (non-injured) players from ESPN."
    )
    parser.add_argument("--season", type=int, default=2024,
                        choices=sorted(SEASON_DATES.keys()),
                        help="WNBA season year (default: 2024)")
    parser.add_argument("--all-seasons", action="store_true",
                        help="Run all seasons (2021-2025) and save separate CSVs")
    parser.add_argument("--test", action="store_true",
                        help="Process only the first 5 games (smoke test)")
    parser.add_argument("--out-csv", default=None,
                        help="Output CSV path (default: active_players_{YEAR}.csv)")
    args = parser.parse_args()

    if args.all_seasons:
        for yr in sorted(SEASON_DATES.keys()):
            print(f"\n{'='*60}\nSEASON {yr}\n{'='*60}")
            rows = run_season(yr, test=args.test)
            save_csv(rows, Path(f"active_players_{yr}.csv"))
    else:
        yr   = args.season
        rows = run_season(yr, test=args.test)
        out  = Path(args.out_csv or f"active_players_{yr}.csv")
        save_csv(rows, out)


if __name__ == "__main__":
    main()

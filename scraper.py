#!/usr/bin/env python3
"""
WNBA 2024 Regular Season – DND (Did Not Dress) Injury / Illness Scraper
========================================================================
Automates the manual process of clicking through every game on
  https://www.wnba.com/schedule?season=2024&month=all&team=all
and recording every player listed as DND due to injury or illness.

Two complementary strategies run in sequence:

  1. WNBA Stats API  (stats.wnba.com)
       – Fast.  Fetches the schedule + per-game box-score inactive list.
       – No browser required (uses requests + proper headers).

  2. Browser / Page-intercept  (Playwright + Chromium)
       – Slower but captures the same JSON the website fetches.
       – Enabled automatically if the API returns incomplete data,
         or if you pass --browser-only.

Output files (written to the current directory):
    dnd_players_2024.json   – full detail, one record per player-game
    dnd_players_2024.csv    – same data in CSV

Usage
-----
    # Normal run (API first, browser fallback per game)
    python scraper.py

    # Smoke-test on the first 5 games only
    python scraper.py --test

    # Skip games already present in a previous output file
    python scraper.py --resume dnd_players_2024.json

    # Use only the browser scraping path
    python scraper.py --browser-only

Requirements
------------
    pip install requests playwright
    playwright install chromium
        OR set CHROMIUM_PATH to point at an existing chrome/chromium binary.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
STATS_BASE = "https://stats.wnba.com/stats"
CDN_BASE = "https://cdn.wnba.com/static/json"
SCHEDULE_PAGE = "https://www.wnba.com/schedule?season=2024&month=all&team=all"
GAME_PAGE_TMPL = "https://www.wnba.com/game/{game_id}/"
SEASON = "2024"
API_DELAY = 0.6        # seconds between API requests
BROWSER_DELAY = 2.0    # seconds between browser page loads

# Chromium binary path (Playwright looks here first, then PATH, then auto-install)
CHROMIUM_PATH = os.environ.get(
    "CHROMIUM_PATH",
    "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
)

# DND-related keywords in injury-status / reason strings
_INJURY_KW = frozenset(
    {
        "dnd", "did not dress",
        "injury", "injured", "illness", "ill", "sick",
        "knee", "ankle", "foot", "hip", "shoulder", "back",
        "wrist", "hand", "finger", "hamstring", "quad", "quadriceps",
        "calf", "achilles", "concussion", "head", "neck", "groin",
        "shin", "elbow", "toe", "thigh", "rib", "abdomen", "abdominal",
        "conditioning", "rest", "soreness", "sore",
        "strain", "sprain", "fracture", "surgery", "recovery",
        "personal", "non-covid illness", "covid", "health and safety",
        "league excused", "league",
    }
)
# These are explicit *non-injury* inactive reasons → exclude
_NON_INJURY_KW = frozenset(
    {"coach's decision", "coaches decision", "coach decision",
     "dnp - cd", "dnp-cd", "suspended", "trade", "waived", "g league"}
)


def is_dnd_injury(reason: str) -> bool:
    """Return True if reason string indicates injury / illness DND."""
    if not reason:
        return True   # blank reason on inactive list → likely DND
    low = reason.lower()
    for kw in _NON_INJURY_KW:
        if kw in low:
            return False
    for kw in _INJURY_KW:
        if kw in low:
            return True
    return False


# ---------------------------------------------------------------------------
# Strategy 1 – WNBA Stats API  (requests)
# ---------------------------------------------------------------------------
_API_SESSION = requests.Session()
_API_SESSION.headers.update(
    {
        "Accept": "application/json, text/plain, */*",
        "Accept-Encoding": "gzip, deflate, br",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
        "Host": "stats.wnba.com",
        "Origin": "https://www.wnba.com",
        "Referer": "https://www.wnba.com/",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "x-nba-stats-origin": "stats",
        "x-nba-stats-token": "true",
    }
)


def _api_get(url: str, params: dict | None = None, retries: int = 4) -> dict | None:
    for attempt in range(retries):
        try:
            r = _API_SESSION.get(url, params=params, timeout=60)
            if r.status_code == 200:
                return r.json()
            print(f"  [API WARN] HTTP {r.status_code} → {url}", file=sys.stderr)
        except requests.RequestException as exc:
            print(f"  [API ERR] {exc} (attempt {attempt + 1})", file=sys.stderr)
        time.sleep(min(2 ** attempt, 16))
    return None


def api_fetch_schedule() -> list[dict]:
    """Fetch the 2024 regular-season schedule from stats.wnba.com."""
    print("  [API] Fetching schedule …")
    data = _api_get(
        f"{STATS_BASE}/scheduleleaguev2",
        params={"LeagueID": "10", "Season": SEASON},
    )
    if not data:
        return []

    rs = data.get("resultSets", {})
    game_dates: list[dict] = []
    if isinstance(rs, dict):
        game_dates = rs.get("gameDates", [])
    elif isinstance(rs, list):
        # Legacy list-style response
        rs_map = {x["name"]: x for x in rs}
        game_dates = _parse_legacy_schedule(rs_map)

    games = []
    for date_block in game_dates:
        game_date = date_block.get("gameDate", "")
        for g in date_block.get("games", []):
            gid = g.get("gameId", "")
            if not gid.startswith("102"):   # regular season only
                continue
            home = g.get("homeTeam", {})
            away = g.get("awayTeam", {})
            games.append(
                {
                    "gameId": gid,
                    "gameDate": game_date,
                    "gameStatus": g.get("gameStatus", ""),
                    "homeTeamAbbrev": home.get("teamTricode", ""),
                    "awayTeamAbbrev": away.get("teamTricode", ""),
                }
            )
    print(f"  [API] {len(games)} regular-season games found.")
    return games


def _parse_legacy_schedule(rs_map: dict) -> list[dict]:
    gh = rs_map.get("GameHeader")
    if not gh:
        return []
    headers = gh["headers"]
    date_map: dict[str, list[dict]] = {}
    for row in gh["rowSet"]:
        r = dict(zip(headers, row))
        gid = r.get("GAME_ID", "")
        if not gid.startswith("102"):
            continue
        gdate = r.get("GAME_DATE_EST", "")[:10]
        date_map.setdefault(gdate, []).append(
            {
                "gameId": gid,
                "gameStatus": r.get("GAME_STATUS_ID", ""),
                "homeTeam": {"teamTricode": str(r.get("HOME_TEAM_ID", ""))},
                "awayTeam": {"teamTricode": str(r.get("VISITOR_TEAM_ID", ""))},
            }
        )
    return [{"gameDate": d, "games": gs} for d, gs in date_map.items()]


def api_fetch_inactive_players(game_id: str) -> list[dict]:
    """Return inactive-player rows from the box-score summary."""
    data = _api_get(
        f"{STATS_BASE}/boxscoresummaryv2", params={"GameID": game_id}
    )
    if not data:
        return []
    rs_map = {rs["name"]: rs for rs in data.get("resultSets", [])}
    rs = rs_map.get("InactivePlayers")
    if not rs:
        return []
    headers = rs["headers"]
    players = []
    for row in rs["rowSet"]:
        p = dict(zip(headers, row))
        players.append(
            {
                "playerId": str(p.get("PLAYER_ID", "")),
                "playerName": f"{p.get('FIRST_NAME','')} {p.get('LAST_NAME','')}".strip(),
                "teamAbbrev": p.get("TEAM_ABBREVIATION", ""),
                "reason": p.get("REASON", ""),
            }
        )
    return players


def api_fetch_cdn_injury_report(game_id: str, game_date: str) -> list[dict]:
    """Try CDN pre-game injury-report JSON (multiple URL patterns)."""
    date_nd = game_date.replace("-", "").replace("/", "")[:8]
    for url in [
        f"{CDN_BASE}/liveData/injuryReport/injuryReport_{date_nd}_{game_id}_00.json",
        f"{CDN_BASE}/liveData/injuryReport/injuryReport_{game_id}_00.json",
        f"{CDN_BASE}/liveData/injuryReport/injuryReport_{game_id}.json",
    ]:
        data = _api_get(url)
        if data:
            return data.get("injury_report", data.get("InjuryReport", []))
    return []


def api_process_game(game: dict) -> list[dict]:
    """Collect all DND-injury rows for one game via the Stats API."""
    game_id = game["gameId"]
    game_date = game["gameDate"]
    matchup = f"{game['awayTeamAbbrev']} @ {game['homeTeamAbbrev']}"
    rows: list[dict] = []
    seen: set[str] = set()

    # CDN injury report (pre-game)
    for row in api_fetch_cdn_injury_report(game_id, game_date):
        raw_status = str(row.get("playerStatus", row.get("status", ""))).upper()
        reason = str(row.get("reason", row.get("Reason", ""))).strip()
        name = (
            row.get("playerName")
            or f"{row.get('firstName','')} {row.get('lastName','')}".strip()
        )
        team = row.get("teamTricode", row.get("teamAbbreviation", ""))
        pid = str(row.get("personId", row.get("playerId", "")))
        if "DND" in raw_status or is_dnd_injury(reason):
            key = f"{game_id}_{pid or name}"
            seen.add(key)
            rows.append(
                _row(game_id, game_date, matchup, name, pid, team,
                     raw_status or "DND", reason, "CDN_InjuryReport")
            )

    # Box-score inactive list (post-game)
    for p in api_fetch_inactive_players(game_id):
        if is_dnd_injury(p["reason"]):
            key = f"{game_id}_{p['playerId'] or p['playerName']}"
            if key not in seen:
                rows.append(
                    _row(game_id, game_date, matchup,
                         p["playerName"], p["playerId"], p["teamAbbrev"],
                         "DND/INACTIVE", p["reason"] or "Not provided",
                         "BoxScore_InactiveList")
                )
    return rows


# ---------------------------------------------------------------------------
# Strategy 2 – Browser / Playwright
# ---------------------------------------------------------------------------

def _get_playwright_browser(pw):
    """Return a Playwright browser instance, trying several strategies."""
    launch_kwargs = {
        "headless": True,
        "args": [
            "--disable-http2",          # fixes ERR_HTTP2_PROTOCOL_ERROR on wnba.com
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ],
    }

    # Use pre-installed binary if it exists (sandbox env); otherwise let
    # Playwright find/use its own installed Chromium.
    if os.path.exists(CHROMIUM_PATH):
        launch_kwargs["executable_path"] = CHROMIUM_PATH

    return pw.chromium.launch(**launch_kwargs)


def browser_fetch_schedule(pw) -> list[dict]:
    """
    Navigate the WNBA schedule page and intercept the API call that
    returns all game data, or fall back to link scraping.
    """
    print("  [Browser] Fetching schedule …")
    browser = _get_playwright_browser(pw)
    games: list[dict] = []
    intercepted: list[dict] = []

    def on_response(response):
        url = response.url
        if "scheduleleaguev2" in url and response.status == 200:
            try:
                body = response.json()
                rs = body.get("resultSets", {})
                if isinstance(rs, dict):
                    game_dates = rs.get("gameDates", [])
                elif isinstance(rs, list):
                    rs_map = {x["name"]: x for x in rs}
                    game_dates = _parse_legacy_schedule(rs_map)
                else:
                    game_dates = []
                for date_block in game_dates:
                    gdate = date_block.get("gameDate", "")
                    for g in date_block.get("games", []):
                        gid = g.get("gameId", "")
                        if gid.startswith("102"):
                            home = g.get("homeTeam", {})
                            away = g.get("awayTeam", {})
                            intercepted.append({
                                "gameId": gid,
                                "gameDate": gdate,
                                "gameStatus": g.get("gameStatus", ""),
                                "homeTeamAbbrev": home.get("teamTricode", ""),
                                "awayTeamAbbrev": away.get("teamTricode", ""),
                            })
            except Exception:
                pass

    try:
        context = browser.new_context()
        page = context.new_page()
        page.on("response", on_response)
        page.goto(SCHEDULE_PAGE, wait_until="load", timeout=60_000)
        # Give JS time to fire the schedule API call and render games
        time.sleep(5)
        games = intercepted if intercepted else _scrape_schedule_links(page)
    finally:
        browser.close()

    print(f"  [Browser] {len(games)} regular-season games found.")
    return games


def _scrape_schedule_links(page) -> list[dict]:
    """
    Fall-back: parse game links from the rendered schedule page HTML.
    Returns list of game dicts with at minimum gameId and gameDate.
    """
    import re
    games = []
    # Game links look like /game/1022400001/
    hrefs = page.eval_on_selector_all(
        "a[href*='/game/']",
        "els => els.map(el => ({href: el.href, text: el.textContent}))",
    )
    seen_ids = set()
    for item in hrefs:
        href = item.get("href", "")
        m = re.search(r"/game/(\d+)/", href)
        if m:
            gid = m.group(1)
            if gid.startswith("102") and gid not in seen_ids:
                seen_ids.add(gid)
                games.append({
                    "gameId": gid,
                    "gameDate": "",
                    "gameStatus": "",
                    "homeTeamAbbrev": "",
                    "awayTeamAbbrev": "",
                })
    return games


def browser_process_game(game: dict, pw) -> list[dict]:
    """
    Navigate to the game page, intercept the box-score and injury-report
    API calls, and return DND-injury rows.
    """
    game_id = game["gameId"]
    game_date = game["gameDate"]
    matchup = f"{game.get('awayTeamAbbrev','')} @ {game.get('homeTeamAbbrev','')}"
    rows: list[dict] = []
    seen: set[str] = set()
    intercepted_inactive: list[dict] = []
    intercepted_injury: list[dict] = []

    def on_response(response):
        url = response.url
        if response.status != 200:
            return
        try:
            if "boxscoresummaryv2" in url and game_id in url:
                body = response.json()
                rs_map = {rs["name"]: rs for rs in body.get("resultSets", [])}
                rs = rs_map.get("InactivePlayers")
                if rs:
                    hdrs = rs["headers"]
                    for row in rs["rowSet"]:
                        p = dict(zip(hdrs, row))
                        intercepted_inactive.append({
                            "playerId": str(p.get("PLAYER_ID", "")),
                            "playerName": f"{p.get('FIRST_NAME','')} {p.get('LAST_NAME','')}".strip(),
                            "teamAbbrev": p.get("TEAM_ABBREVIATION", ""),
                            "reason": p.get("REASON", ""),
                        })
            elif "injuryReport" in url and game_id in url:
                body = response.json()
                rows_raw = body.get("injury_report", body.get("InjuryReport", []))
                intercepted_injury.extend(rows_raw)
        except Exception:
            pass

    browser = _get_playwright_browser(pw)
    try:
        context = browser.new_context()
        page = context.new_page()
        page.on("response", on_response)
        game_url = GAME_PAGE_TMPL.format(game_id=game_id)
        page.goto(game_url, wait_until="networkidle", timeout=45_000)
        time.sleep(1.5)
    except Exception as exc:
        print(f"  [Browser] Error on {game_id}: {exc}", file=sys.stderr)
    finally:
        browser.close()

    # Process CDN injury-report data
    for row in intercepted_injury:
        raw_status = str(row.get("playerStatus", row.get("status", ""))).upper()
        reason = str(row.get("reason", row.get("Reason", ""))).strip()
        name = (
            row.get("playerName")
            or f"{row.get('firstName','')} {row.get('lastName','')}".strip()
        )
        team = row.get("teamTricode", row.get("teamAbbreviation", ""))
        pid = str(row.get("personId", row.get("playerId", "")))
        if "DND" in raw_status or is_dnd_injury(reason):
            key = f"{game_id}_{pid or name}"
            seen.add(key)
            rows.append(
                _row(game_id, game_date, matchup, name, pid, team,
                     raw_status or "DND", reason, "Browser_InjuryReport")
            )

    # Process box-score inactive players
    for p in intercepted_inactive:
        if is_dnd_injury(p["reason"]):
            key = f"{game_id}_{p['playerId'] or p['playerName']}"
            if key not in seen:
                rows.append(
                    _row(game_id, game_date, matchup,
                         p["playerName"], p["playerId"], p["teamAbbrev"],
                         "DND/INACTIVE", p["reason"] or "Not provided",
                         "Browser_BoxScore")
                )
    return rows


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row(game_id, game_date, matchup, player_name, player_id,
         team, status, reason, source) -> dict:
    return {
        "gameId": game_id,
        "gameDate": game_date,
        "matchup": matchup,
        "playerName": player_name,
        "playerId": player_id,
        "team": team,
        "status": status,
        "reason": reason if reason else "Not provided",
        "source": source,
    }


def save_results(rows: list[dict], json_path: Path, csv_path: Path) -> None:
    with json_path.open("w") as f:
        json.dump(rows, f, indent=2)
    print(f"Saved JSON → {json_path}  ({len(rows)} rows)")

    fieldnames = ["gameDate", "matchup", "gameId", "playerName", "playerId",
                  "team", "status", "reason", "source"]
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"Saved CSV  → {csv_path}  ({len(rows)} rows)")

    if rows:
        player_counts = Counter(
            f"{r['playerName']} ({r['team']})" for r in rows
        )
        game_counts = Counter(r["gameId"] for r in rows)
        print(f"\nGames with ≥1 DND-injury entry : {len(game_counts)}")
        print(f"Total DND player-game entries  : {len(rows)}")
        print("\nTop 25 most-frequently DND players:")
        for player, count in player_counts.most_common(25):
            print(f"  {count:>3}x  {player}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scrape WNBA 2024 DND injury/illness players for every game."
    )
    parser.add_argument("--test", action="store_true",
                        help="Process only the first 5 games (smoke test)")
    parser.add_argument("--resume", metavar="JSON_FILE",
                        help="Skip game IDs already in this output file")
    parser.add_argument("--browser-only", action="store_true",
                        help="Use Playwright browser only (no direct API calls)")
    parser.add_argument("--api-only", action="store_true",
                        help="Use Stats API only (no browser, default if no args)")
    parser.add_argument("--out-json", default="dnd_players_2024.json",
                        help="Output JSON file path (default: dnd_players_2024.json)")
    parser.add_argument("--out-csv", default="dnd_players_2024.csv",
                        help="Output CSV file path (default: dnd_players_2024.csv)")
    args = parser.parse_args()

    use_api = not args.browser_only
    use_browser = args.browser_only  # explicit browser-only flag
    # Default: API first, then optionally browser for games with no data

    # Load resume data
    existing_rows: list[dict] = []
    skip_ids: set[str] = set()
    resume_path = Path(args.resume) if args.resume else None
    if resume_path and resume_path.exists():
        with resume_path.open() as f:
            existing_rows = json.load(f)
        skip_ids = {r["gameId"] for r in existing_rows}
        print(f"Resuming: {len(skip_ids)} games already processed.")

    # --- Get schedule ---
    games: list[dict] = []
    if use_api and not use_browser:
        games = api_fetch_schedule()
        if not games:
            print("API schedule fetch failed – falling back to browser.", file=sys.stderr)
            use_browser = True

    if use_browser or not games:
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                games = browser_fetch_schedule(pw)
        except ImportError:
            print("Playwright not installed.  Run: pip install playwright && playwright install chromium",
                  file=sys.stderr)
            sys.exit(1)

    if not games:
        sys.exit("Could not retrieve game schedule from any source.")

    if args.test:
        games = [g for g in games if g["gameId"] not in skip_ids][:5]
        print(f"TEST MODE – processing {len(games)} games.")

    all_rows: list[dict] = list(existing_rows)
    total = len(games)
    pw_context = None

    if use_browser:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            print("Playwright not installed.", file=sys.stderr)
            sys.exit(1)

    for idx, game in enumerate(games, 1):
        gid = game["gameId"]
        if gid in skip_ids:
            continue

        label = (
            f"[{idx:>3}/{total}] {game.get('gameDate','??')[:10]}  "
            f"{game.get('awayTeamAbbrev','??')} @ {game.get('homeTeamAbbrev','??')}  ({gid})"
        )
        print(label)

        dnd_rows: list[dict] = []

        if use_api:
            dnd_rows = api_process_game(game)
            time.sleep(API_DELAY)

        if use_browser and not dnd_rows:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                dnd_rows = browser_process_game(game, pw)
            time.sleep(BROWSER_DELAY)

        if dnd_rows:
            print(f"  → {len(dnd_rows)} DND-injury player(s)")
            all_rows.extend(dnd_rows)

    # Save outputs
    print(f"\n{'='*60}")
    save_results(all_rows, Path(args.out_json), Path(args.out_csv))


if __name__ == "__main__":
    main()

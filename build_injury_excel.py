#!/usr/bin/env python3
"""
build_injury_excel.py

Fetches 2024 WNBA regular season data from ESPN's public API,
identifies injured players (DND), and produces an Excel workbook
with injury context metrics.
"""

import argparse
import json
import math
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
import openpyxl
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

# ---------------------------------------------------------------------------
# Arena data: (lat, lon, city, state, tz_offset_summer)
# tz_offset = UTC offset during summer (EDT=-4, CDT=-5, PDT/AZ=-7)
# ---------------------------------------------------------------------------
ARENAS = {
    "ATL":  (33.6534,  -84.4671,  "Atlanta",        "Georgia",      -4),
    "CHI":  (41.8827,  -87.6742,  "Chicago",         "Illinois",     -5),
    "CONN": (41.4862,  -72.1098,  "Uncasville",      "Connecticut",  -4),
    "DAL":  (32.7482,  -97.1128,  "Dallas",          "Texas",        -5),
    "IND":  (39.7641,  -86.1556,  "Indianapolis",    "Indiana",      -4),
    "LV":   (36.0905, -115.1750,  "Las Vegas",       "Nevada",       -7),
    "LA":   (34.0430, -118.2673,  "Los Angeles",     "California",   -7),
    "MIN":  (44.9795,  -93.2763,  "Minneapolis",     "Minnesota",    -5),
    "NY":   (40.6826,  -73.9754,  "Brooklyn",        "New York",     -4),
    "PHX":  (33.4457, -112.0712,  "Phoenix",         "Arizona",      -7),
    "SEA":  (47.6225, -122.3542,  "Seattle",         "Washington",   -7),
    "WSH":  (38.8728,  -76.9956,  "Washington D.C.", "D.C.",         -4),
}

# City name → arena key (for fallback lookups)
CITY_TO_ARENA = {v[2].lower(): k for k, v in ARENAS.items()}

# ESPN abbreviation → arena key (they happen to match for WNBA)
ESPN_TO_ARENA = {
    "ATL": "ATL", "CHI": "CHI", "CONN": "CONN", "DAL": "DAL",
    "IND": "IND", "LV": "LV",   "LA": "LA",     "MIN": "MIN",
    "NY": "NY",   "PHX": "PHX", "SEA": "SEA",   "WSH": "WSH",
}

# Lower-extremity injury keywords → include
LOWER_EXTREMITY_KW = [
    "ankle", "knee", "foot", "calf", "hamstring", "achilles",
    "quad", "quadricep", "groin", "shin", "toe", "hip",
    "acl", "mcl", "meniscus", "ligament", "patellar", "plantar",
    "thigh", "leg",
]

# Ligamentous keywords
LIGAMENTOUS_KW = [
    "ankle sprain", "knee sprain", "acl", "mcl", "meniscus",
    "ligament", "sprain",
]

# Soft-tissue keywords
SOFT_TISSUE_KW = [
    "calf", "hamstring", "achilles", "strain", "tendon",
    "quad", "quadricep",
]

# Non-lower-extremity → exclude
UPPER_BODY_KW = [
    "shoulder", "elbow", "wrist", "hand", "finger", "thumb",
    "back", "neck", "concussion", "head", "chest", "rib",
    "illness", "personal", "rest",
]

# ---------------------------------------------------------------------------
# ESPN API endpoints
# ---------------------------------------------------------------------------
ESPN_SCOREBOARD = (
    "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/"
    "scoreboard?dates={date}&limit=50"
)
ESPN_SUMMARY = (
    "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/"
    "summary?event={event_id}"
)
ESPN_ATHLETE = (
    "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/"
    "athletes/{athlete_id}"
)

# Season date range (inclusive)
SEASON_START = date(2024, 5, 14)
SEASON_END   = date(2024, 9, 19)

RATE_LIMIT_DELAY = 0.5  # seconds between uncached requests


# ---------------------------------------------------------------------------
# Caching helpers
# ---------------------------------------------------------------------------

def _cache_path(cache_dir: Path, key: str) -> Path:
    """Return a safe cache file path for the given key."""
    safe = key.replace("/", "_").replace("?", "_").replace("&", "_").replace("=", "_")
    return cache_dir / f"{safe}.json"


def fetch_json(url: str, cache_dir: Path) -> dict:
    """Fetch a URL, returning parsed JSON. Results are cached on disk."""
    cp = _cache_path(cache_dir, url)
    if cp.exists():
        with cp.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    time.sleep(RATE_LIMIT_DELAY)
    resp = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    data = resp.json()
    cp.parent.mkdir(parents=True, exist_ok=True)
    with cp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh)
    return data


# ---------------------------------------------------------------------------
# Haversine distance
# ---------------------------------------------------------------------------

def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return distance in miles between two lat/lon points."""
    R = 3958.8  # Earth radius in miles
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ---------------------------------------------------------------------------
# Minutes parsing
# ---------------------------------------------------------------------------

def parse_minutes(raw: str) -> int:
    """Parse a minutes string like '35:23' or '35' to an integer (rounded)."""
    if not raw or raw.strip() in ("", "--", "0:00"):
        return 0
    raw = raw.strip()
    if ":" in raw:
        parts = raw.split(":")
        try:
            mins = int(parts[0])
            secs = int(parts[1]) if len(parts) > 1 else 0
            return round(mins + secs / 60)
        except ValueError:
            return 0
    try:
        return round(float(raw))
    except ValueError:
        return 0


# ---------------------------------------------------------------------------
# Schedule fetching
# ---------------------------------------------------------------------------

def fetch_schedule(cache_dir: Path, test_mode: bool = False) -> dict:
    """
    Returns team_schedule: {team_abbrev: [sorted list of game dicts]}
    Each game dict: {eventId, date, homeTeam, awayTeam, isHome, arenaKey}
    """
    all_events = []
    current = SEASON_START
    while current <= SEASON_END:
        url = ESPN_SCOREBOARD.format(date=current.strftime("%Y%m%d"))
        try:
            data = fetch_json(url, cache_dir)
        except Exception as exc:
            print(f"  [WARN] scoreboard fetch failed for {current}: {exc}")
            current += timedelta(days=1)
            continue

        for event in data.get("events", []):
            status_type = (
                event.get("status", {}).get("type", {}).get("name", "")
            )
            if status_type not in ("STATUS_FINAL", "STATUS_FINAL_OT"):
                current += timedelta(days=1)
                continue

            event_id = event.get("id", "")
            event_date_str = event.get("date", "")
            # ESPN date is ISO 8601 UTC; we care only about the calendar date
            if event_date_str:
                try:
                    event_date = datetime.fromisoformat(
                        event_date_str.replace("Z", "+00:00")
                    ).date()
                except ValueError:
                    event_date = current
            else:
                event_date = current

            competitions = event.get("competitions", [])
            if not competitions:
                continue
            comp = competitions[0]

            home_team_abbrev = ""
            away_team_abbrev = ""
            for competitor in comp.get("competitors", []):
                abbrev = competitor.get("team", {}).get("abbreviation", "")
                if competitor.get("homeAway", "") == "home":
                    home_team_abbrev = abbrev
                else:
                    away_team_abbrev = abbrev

            # Determine arena key from venue or home team
            venue = comp.get("venue", {})
            venue_city = venue.get("address", {}).get("city", "")
            arena_key = _resolve_arena_key(home_team_abbrev, venue_city)

            if not home_team_abbrev or not away_team_abbrev:
                continue

            all_events.append({
                "eventId": event_id,
                "date": event_date,
                "homeTeam": home_team_abbrev,
                "awayTeam": away_team_abbrev,
                "arenaKey": arena_key,
            })

        current += timedelta(days=1)

    # Deduplicate by eventId
    seen = set()
    unique_events = []
    for ev in all_events:
        if ev["eventId"] not in seen:
            seen.add(ev["eventId"])
            unique_events.append(ev)

    # Sort by date
    unique_events.sort(key=lambda e: e["date"])

    if test_mode:
        unique_events = unique_events[:10]

    # Build per-team schedule
    team_schedule: dict = {}
    for ev in unique_events:
        for team, is_home in [
            (ev["homeTeam"], True),
            (ev["awayTeam"], False),
        ]:
            if team not in team_schedule:
                team_schedule[team] = []
            team_schedule[team].append({
                "eventId": ev["eventId"],
                "date": ev["date"],
                "homeTeam": ev["homeTeam"],
                "awayTeam": ev["awayTeam"],
                "isHome": is_home,
                "arenaKey": ev["arenaKey"],
                "opponent": ev["awayTeam"] if is_home else ev["homeTeam"],
            })

    # Sort each team's schedule by date and assign game_number
    for team in team_schedule:
        team_schedule[team].sort(key=lambda g: g["date"])
        for idx, g in enumerate(team_schedule[team], start=1):
            g["game_number"] = idx

    print(f"[INFO] Loaded {len(unique_events)} unique events across {len(team_schedule)} teams.")
    return team_schedule, unique_events


def _resolve_arena_key(home_team_abbrev: str, venue_city: str) -> str:
    """Resolve arena key from home team abbreviation or venue city."""
    if home_team_abbrev in ESPN_TO_ARENA:
        return ESPN_TO_ARENA[home_team_abbrev]
    if venue_city:
        key = CITY_TO_ARENA.get(venue_city.lower())
        if key:
            return key
    return home_team_abbrev  # fallback


# ---------------------------------------------------------------------------
# Boxscore / game log fetching
# ---------------------------------------------------------------------------

def fetch_game_logs(
    unique_events: list,
    cache_dir: Path,
) -> tuple:
    """
    Fetch boxscores for all events.

    Returns:
        player_log: {player_id: [sorted list of game dicts]}
        dnd_list: [dnd event dicts]
        player_info: {player_id: {name, position, dob}}
    """
    player_log: dict = {}
    dnd_list: list = []
    player_info: dict = {}

    total = len(unique_events)
    for i, ev in enumerate(unique_events, start=1):
        event_id = ev["eventId"]
        print(f"  [INFO] Fetching boxscore {i}/{total}: event {event_id} ({ev['date']})")
        url = ESPN_SUMMARY.format(event_id=event_id)
        try:
            data = fetch_json(url, cache_dir)
        except Exception as exc:
            print(f"  [WARN] summary fetch failed for {event_id}: {exc}")
            continue

        boxscore = data.get("boxscore", {})
        players_sections = boxscore.get("players", [])

        for section in players_sections:
            team_info = section.get("team", {})
            team_abbrev = team_info.get("abbreviation", "")

            statistics = section.get("statistics", [])
            if not statistics:
                continue
            stat_section = statistics[0]

            names = stat_section.get("names", [])
            try:
                min_idx = names.index("MIN")
            except ValueError:
                min_idx = None

            athletes = stat_section.get("athletes", [])
            for athlete_entry in athletes:
                athlete = athlete_entry.get("athlete", {})
                player_id = athlete.get("id", "")
                player_name = athlete.get("displayName", "")
                position = athlete.get("position", {}).get("abbreviation", "")
                did_not_play = athlete_entry.get("didNotPlay", False)
                reason = athlete_entry.get("reason", "")
                active = athlete_entry.get("active", True)

                if not player_id:
                    continue

                # Cache player info
                if player_id not in player_info:
                    player_info[player_id] = {
                        "name": player_name,
                        "position": position,
                        "dob": None,  # fetched lazily
                    }

                if did_not_play:
                    dnd_list.append({
                        "eventId": event_id,
                        "date": ev["date"],
                        "player_id": player_id,
                        "team": team_abbrev,
                        "reason": reason,
                    })
                    continue

                # Parse minutes
                stats = athlete_entry.get("stats", [])
                minutes = 0
                if min_idx is not None and min_idx < len(stats):
                    minutes = parse_minutes(stats[min_idx])

                # Determine arena key for the game
                arena_key = ev["arenaKey"]

                is_home = team_abbrev == ev["homeTeam"]
                opponent = ev["awayTeam"] if is_home else ev["homeTeam"]

                game_entry = {
                    "eventId": event_id,
                    "date": ev["date"],
                    "team": team_abbrev,
                    "minutes": minutes,
                    "arenaKey": arena_key,
                    "isHome": is_home,
                    "opponent": opponent,
                }

                if player_id not in player_log:
                    player_log[player_id] = []
                player_log[player_id].append(game_entry)

    # Sort each player's log by date
    for pid in player_log:
        player_log[pid].sort(key=lambda g: g["date"])

    print(f"[INFO] Built logs for {len(player_log)} players; {len(dnd_list)} DND events.")
    return player_log, dnd_list, player_info


# ---------------------------------------------------------------------------
# Player age fetching
# ---------------------------------------------------------------------------

_dob_cache: dict = {}


def get_player_age_at_date(
    player_id: str,
    target_date: date,
    cache_dir: Path,
) -> float | None:
    """Return player's age (fractional years) at target_date."""
    global _dob_cache
    if player_id not in _dob_cache:
        url = ESPN_ATHLETE.format(athlete_id=player_id)
        try:
            data = fetch_json(url, cache_dir)
        except Exception as exc:
            print(f"  [WARN] athlete fetch failed for {player_id}: {exc}")
            _dob_cache[player_id] = None
            return None

        # Navigate ESPN athlete structure
        athlete = data.get("athlete", data)  # some responses nest under "athlete"
        dob_str = athlete.get("dateOfBirth", "")
        if not dob_str:
            # Try nested structure
            dob_str = data.get("dateOfBirth", "")
        if dob_str:
            try:
                dob = datetime.fromisoformat(dob_str.replace("Z", "+00:00")).date()
                _dob_cache[player_id] = dob
            except ValueError:
                _dob_cache[player_id] = None
        else:
            _dob_cache[player_id] = None

    dob = _dob_cache.get(player_id)
    if dob is None:
        return None

    age_days = (target_date - dob).days
    return round(age_days / 365.25, 2)


# ---------------------------------------------------------------------------
# Injury classification
# ---------------------------------------------------------------------------

def classify_injury(reason: str) -> str:
    """
    Classify injury reason into 'ligamentous', 'soft tissue', or ''.
    """
    if not reason:
        return ""
    lower = reason.lower()
    for kw in LIGAMENTOUS_KW:
        if kw in lower:
            return "ligamentous"
    for kw in SOFT_TISSUE_KW:
        if kw in lower:
            return "soft tissue"
    return ""


def should_include(reason: str) -> bool:
    """
    Return True if the DND event should be included in the output.
    Include if lower extremity or blank/vague; exclude if clearly upper body.
    """
    if not reason:
        return True  # blank → include for manual review
    lower = reason.lower()
    # Check exclusion list first
    for kw in UPPER_BODY_KW:
        if kw in lower:
            return False
    # Check inclusion list
    for kw in LOWER_EXTREMITY_KW:
        if kw in lower:
            return True
    # Vague / unclear → include for manual review
    return True


# ---------------------------------------------------------------------------
# 7-day window helpers
# ---------------------------------------------------------------------------

def games_in_window(
    team: str,
    team_schedule: dict,
    window_start: date,
    window_end: date,
) -> list:
    """Return team games whose dates fall within [window_start, window_end]."""
    schedule = team_schedule.get(team, [])
    return [g for g in schedule if window_start <= g["date"] <= window_end]


def cumulative_minutes_7d(
    player_id: str,
    player_log: dict,
    window_start: date,
    window_end: date,
) -> int:
    """Sum player's minutes in games within [window_start, window_end]."""
    logs = player_log.get(player_id, [])
    return sum(g["minutes"] for g in logs if window_start <= g["date"] <= window_end)


def cumulative_travel_7d(
    team_games_in_window: list,
) -> float:
    """Sum haversine distances between consecutive games in the window."""
    total = 0.0
    sorted_games = sorted(team_games_in_window, key=lambda g: g["date"])
    for i in range(1, len(sorted_games)):
        prev_arena = sorted_games[i - 1].get("arenaKey", "")
        curr_arena = sorted_games[i].get("arenaKey", "")
        if prev_arena in ARENAS and curr_arena in ARENAS:
            pa = ARENAS[prev_arena]
            ca = ARENAS[curr_arena]
            total += haversine(pa[0], pa[1], ca[0], ca[1])
    return round(total, 1)


# ---------------------------------------------------------------------------
# Main processing
# ---------------------------------------------------------------------------

def build_rows(
    team_schedule: dict,
    unique_events: list,
    player_log: dict,
    dnd_list: list,
    player_info: dict,
    cache_dir: Path,
) -> list:
    """
    For each DND event, find the injury game and compute all row metrics.
    Returns list of row dicts.
    """
    # Build a quick lookup: eventId → event metadata
    event_meta: dict = {ev["eventId"]: ev for ev in unique_events}

    # Build per-team game_number lookup: {team: {eventId: game_number}}
    team_game_number: dict = {}
    for team, games in team_schedule.items():
        team_game_number[team] = {g["eventId"]: g["game_number"] for g in games}

    rows = []
    seen_injury_events: set = set()  # (player_id, injury_eventId) to deduplicate

    for dnd_event in dnd_list:
        player_id = dnd_event["player_id"]
        dnd_date = dnd_event["date"]
        team = dnd_event["team"]
        reason = dnd_event["reason"]

        if not should_include(reason):
            continue

        # Find the most recent game the player actually played before the DND date
        logs = player_log.get(player_id, [])
        played_games = [
            g for g in logs
            if g["date"] < dnd_date and g["minutes"] > 0
        ]
        if not played_games:
            continue  # no prior played game found

        played_games_sorted = sorted(played_games, key=lambda g: g["date"])
        injury_game = played_games_sorted[-1]

        dedup_key = (player_id, injury_game["eventId"])
        if dedup_key in seen_injury_events:
            continue
        seen_injury_events.add(dedup_key)

        injury_date = injury_game["date"]
        injury_arena_key = injury_game.get("arenaKey", "")
        injury_team = injury_game.get("team", team)

        # Find the game BEFORE the injury game
        prev_games = [
            g for g in played_games_sorted
            if g["date"] < injury_date and g["minutes"] > 0
        ]
        prev_game = prev_games[-1] if prev_games else None

        # Arena data
        injury_arena = ARENAS.get(injury_arena_key)
        if injury_arena is None:
            # Fallback: use team's home arena
            fallback_key = ESPN_TO_ARENA.get(injury_team, "")
            injury_arena = ARENAS.get(fallback_key)
            if injury_arena is None:
                continue
            injury_arena_key = fallback_key

        # Player info
        pinfo = player_info.get(player_id, {})
        player_name = pinfo.get("name", "Unknown")
        position = pinfo.get("position", "")

        # Age at injury date
        age = get_player_age_at_date(player_id, injury_date, cache_dir)

        # Game ID (sequential in team schedule)
        game_id = team_game_number.get(injury_team, {}).get(injury_game["eventId"], 0)

        # Home/Away
        is_home = injury_game.get("isHome", False)
        home_away = "H" if is_home else "A"

        # Opponent
        opponent = injury_game.get("opponent", "")

        # Game city/state
        game_city = injury_arena[2]
        game_state = injury_arena[3]

        # 7-day window: [injury_date-6, injury_date]
        window_start = injury_date - timedelta(days=6)
        window_end = injury_date
        team_window_games = games_in_window(injury_team, team_schedule, window_start, window_end)
        cumul_min_7d = cumulative_minutes_7d(player_id, player_log, window_start, window_end)
        cumul_games_7d = len(team_window_games)
        cumul_travel_7d = cumulative_travel_7d(team_window_games)

        # Prev game info
        if prev_game:
            prev_date = prev_game["date"]
            prev_arena_key = prev_game.get("arenaKey", "")
            prev_arena = ARENAS.get(prev_arena_key)
            if prev_arena is None:
                prev_team = prev_game.get("team", injury_team)
                fallback_key = ESPN_TO_ARENA.get(prev_team, "")
                prev_arena = ARENAS.get(fallback_key)
                prev_arena_key = fallback_key
            prev_city = prev_arena[2] if prev_arena else ""
            days_rest = (injury_date - prev_date).days - 1
            back_to_back = 1 if days_rest == 0 else 0

            if prev_arena and injury_arena:
                travel_miles = round(
                    haversine(
                        prev_arena[0], prev_arena[1],
                        injury_arena[0], injury_arena[1],
                    ),
                    1,
                )
                tz_diff = abs(injury_arena[4] - prev_arena[4])
            else:
                travel_miles = 0.0
                tz_diff = 0
        else:
            prev_date = None
            prev_city = ""
            days_rest = None
            back_to_back = 0
            travel_miles = 0.0
            tz_diff = 0

        # DND game date (the game where player was listed as DND)
        dnd_game_date = dnd_event["date"]
        new_injury_flag = (
            1 if (dnd_game_date - injury_date).days <= 2 else 0
        )

        injury_type = classify_injury(reason)

        row = {
            "Season": "2024-25",
            "Game_ID": game_id,
            "Game_Date": injury_date,
            "Player": player_name,
            "Age_at_Injury": age,
            "Team": injury_team,
            "Opponent": opponent,
            "Home_Away": home_away,
            "Game_City": game_city,
            "Game_State": game_state,
            "Position": position,
            "Minutes_Played": injury_game["minutes"],
            "Cumulative_Min_7d": cumul_min_7d,
            "Prev_Game_Date": prev_date,
            "Prev_Game_City": prev_city,
            "Days_Rest": days_rest,
            "Back_to_Back": back_to_back,
            "Travel_Miles_Since_Prev": travel_miles,
            "Time_Zones_Crossed": tz_diff,
            "Cumulative_Games_7d": cumul_games_7d,
            "Cumulative_Travel_7d": cumul_travel_7d,
            "New_Injury_0_2d": new_injury_flag,
            "Injury_Type": injury_type,
            "Notes": reason,
        }
        rows.append(row)

    # Sort by game date, then team, then player name
    rows.sort(key=lambda r: (r["Game_Date"], r["Team"], r["Player"]))
    print(f"[INFO] Built {len(rows)} output rows.")
    return rows


# ---------------------------------------------------------------------------
# Excel output
# ---------------------------------------------------------------------------

COLUMNS = [
    "Season",
    "Game_ID",
    "Game_Date",
    "Player",
    "Age_at_Injury",
    "Team",
    "Opponent",
    "Home_Away",
    "Game_City",
    "Game_State",
    "Position",
    "Minutes_Played",
    "Cumulative_Min_7d",
    "Prev_Game_Date",
    "Prev_Game_City",
    "Days_Rest",
    "Back_to_Back",
    "Travel_Miles_Since_Prev",
    "Time_Zones_Crossed",
    "Cumulative_Games_7d",
    "Cumulative_Travel_7d",
    "New_Injury_0_2d",
    "Injury_Type",
    "Notes",
]


def write_excel(rows: list, output_path: str) -> None:
    """Write rows to an Excel file with formatted header."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "WNBA Injuries 2024"

    # Header row
    header_font = Font(bold=True)
    header_fill = PatternFill(start_color="DDEEFF", end_color="DDEEFF", fill_type="solid")

    for col_idx, col_name in enumerate(COLUMNS, start=1):
        cell = ws.cell(row=1, column=col_idx, value=col_name)
        cell.font = header_font
        cell.fill = header_fill

    # Data rows
    for row_idx, row in enumerate(rows, start=2):
        for col_idx, col_name in enumerate(COLUMNS, start=1):
            value = row.get(col_name)
            ws.cell(row=row_idx, column=col_idx, value=value)

    # Freeze top row
    ws.freeze_panes = "A2"

    # Auto-size columns
    for col_idx, col_name in enumerate(COLUMNS, start=1):
        col_letter = get_column_letter(col_idx)
        max_len = len(col_name)
        for row_idx in range(2, ws.max_row + 1):
            cell_val = ws.cell(row=row_idx, column=col_idx).value
            if cell_val is not None:
                max_len = max(max_len, len(str(cell_val)))
        ws.column_dimensions[col_letter].width = min(max_len + 2, 50)

    wb.save(output_path)
    print(f"[INFO] Wrote {len(rows)} rows to {output_path}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build WNBA 2024 injury context Excel from ESPN data."
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Only process the first 10 games in the schedule.",
    )
    parser.add_argument(
        "--out",
        default="wnba_injuries_2024.xlsx",
        help="Output Excel filename (default: wnba_injuries_2024.xlsx).",
    )
    parser.add_argument(
        "--cache",
        default=".cache",
        help="Directory to cache ESPN API responses (default: .cache/).",
    )
    args = parser.parse_args()

    cache_dir = Path(args.cache)
    cache_dir.mkdir(parents=True, exist_ok=True)

    print("[STEP 1] Fetching schedule...")
    team_schedule, unique_events = fetch_schedule(cache_dir, test_mode=args.test)

    print("[STEP 2] Fetching game boxscores...")
    player_log, dnd_list, player_info = fetch_game_logs(unique_events, cache_dir)

    print("[STEP 3] Building rows...")
    rows = build_rows(
        team_schedule,
        unique_events,
        player_log,
        dnd_list,
        player_info,
        cache_dir,
    )

    print("[STEP 4] Writing Excel...")
    write_excel(rows, args.out)

    print("[DONE]")


if __name__ == "__main__":
    main()

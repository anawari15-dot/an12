#!/usr/bin/env python3
"""
verify_data.py
==============
Cross-checks a research Excel file (cases and/or controls) against scraped
ESPN data to flag rows that don't appear in the source-of-truth scrape.

Inputs (on your Mac, run after the scrapers):
  - dnd_players_{YEAR}.csv         (per season; from scraper.py)
  - active_players_{YEAR}.csv      (per season; from scraper_active.py)
  - your research file (.xlsx)     (cases + controls)

Output:
  - verification_report.xlsx       (one sheet per check)

Usage
-----
  python3 verify_data.py path/to/research_file.xlsx
  python3 verify_data.py path/to/research_file.xlsx --sheet "Final_Locked_Dataset"
  python3 verify_data.py path/to/research_file.xlsx --case-col Case_Control_Status
"""

from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from pathlib import Path

import pandas as pd

SEASONS = [2021, 2022, 2023, 2024, 2025]

# Map team display-name variations → ESPN abbreviation
TEAM_NAME_TO_ABBREV = {
    # Atlanta Dream
    "atl": "ATL", "atlanta": "ATL", "atlanta dream": "ATL", "dream": "ATL",
    # Chicago Sky
    "chi": "CHI", "chicago": "CHI", "chicago sky": "CHI", "sky": "CHI",
    # Connecticut Sun
    "con": "CONN", "conn": "CONN", "connecticut": "CONN",
    "connecticut sun": "CONN", "sun": "CONN",
    # Dallas Wings
    "dal": "DAL", "dallas": "DAL", "dallas wings": "DAL", "wings": "DAL",
    # Indiana Fever
    "ind": "IND", "indiana": "IND", "indiana fever": "IND", "fever": "IND",
    # Las Vegas Aces
    "lv": "LV", "las vegas": "LV", "las vegas aces": "LV", "aces": "LV",
    # Los Angeles Sparks
    "la": "LA", "los angeles": "LA", "los angeles sparks": "LA",
    "sparks": "LA",
    # Minnesota Lynx
    "min": "MIN", "minnesota": "MIN", "minnesota lynx": "MIN", "lynx": "MIN",
    # New York Liberty
    "ny": "NY", "new york": "NY", "new york liberty": "NY", "liberty": "NY",
    # Phoenix Mercury
    "phx": "PHX", "phoenix": "PHX", "phoenix mercury": "PHX",
    "mercury": "PHX",
    # Seattle Storm
    "sea": "SEA", "seattle": "SEA", "seattle storm": "SEA", "storm": "SEA",
    # Washington Mystics
    "wsh": "WSH", "wash": "WSH", "washington": "WSH",
    "washington mystics": "WSH", "mystics": "WSH",
    # Golden State Valkyries (2025+)
    "gs": "GS", "gsv": "GS", "golden state": "GS",
    "golden state valkyries": "GS", "valkyries": "GS",
}


# ---------------------------------------------------------------------------
def normalize_name(s) -> str:
    """Lowercase, strip punctuation/accents — for fuzzy player-name matching."""
    if s is None or (isinstance(s, float) and pd.isna(s)):
        return ""
    s = str(s)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return s


def normalize_team(s) -> str:
    """Map any team name variant to ESPN abbreviation."""
    if s is None or (isinstance(s, float) and pd.isna(s)):
        return ""
    key = str(s).strip().lower()
    return TEAM_NAME_TO_ABBREV.get(key, key.upper())


def parse_date(v) -> str:
    """Return ISO date string YYYY-MM-DD, or '' on failure."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    try:
        return pd.to_datetime(v).strftime("%Y-%m-%d")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
def load_scraped(folder: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load all dnd_players_{YEAR}.csv and active_players_{YEAR}.csv files."""
    dnd_frames = []
    act_frames = []
    for yr in SEASONS:
        dp = folder / f"dnd_players_{yr}.csv"
        ap = folder / f"active_players_{yr}.csv"
        if dp.exists():
            f = pd.read_csv(dp)
            f["__season"] = yr
            dnd_frames.append(f)
        else:
            print(f"  [missing] {dp.name}")
        if ap.exists():
            f = pd.read_csv(ap)
            f["__season"] = yr
            act_frames.append(f)
        else:
            print(f"  [missing] {ap.name}")

    dnd = pd.concat(dnd_frames, ignore_index=True) if dnd_frames else pd.DataFrame()
    act = pd.concat(act_frames, ignore_index=True) if act_frames else pd.DataFrame()

    if not dnd.empty:
        dnd["__name"] = dnd["playerName"].map(normalize_name)
        dnd["__team"] = dnd["team"].map(normalize_team)
        dnd["__date"] = dnd["gameDate"].map(parse_date)
    if not act.empty:
        act["__name"] = act["playerName"].map(normalize_name)
        act["__team"] = act["team"].map(normalize_team)
        act["__date"] = act["gameDate"].map(parse_date)

    print(f"Loaded {len(dnd)} DND rows, {len(act)} active rows")
    return dnd, act


# ---------------------------------------------------------------------------
def pick_columns(df: pd.DataFrame) -> dict:
    """Find the player / team / game-date columns by common names."""
    lc = {c.lower(): c for c in df.columns}

    def find(*candidates):
        for cand in candidates:
            if cand.lower() in lc:
                return lc[cand.lower()]
        return None

    return {
        "player": find("Player", "playerName", "Name"),
        "team":   find("Team"),
        "date":   find("InjGame_Date", "Game_Date", "gameDate",
                       "Index_Game_Date_ISO", "Date"),
        "status": find("Case_Control_Status", "Status"),
    }


# ---------------------------------------------------------------------------
def verify_rows(
    research: pd.DataFrame, cols: dict,
    dnd: pd.DataFrame, act: pd.DataFrame,
) -> pd.DataFrame:
    """For each research row, check whether it appears in DND or active scrape."""
    out = research.copy()
    out["__row"] = range(1, len(out) + 1)
    out["__name"] = out[cols["player"]].map(normalize_name)
    out["__team"] = out[cols["team"]].map(normalize_team)
    out["__date"] = out[cols["date"]].map(parse_date)

    # Build lookup sets — (name, team, date) and (name, date) for fuzzy fallback
    dnd_full   = set(zip(dnd["__name"], dnd["__team"], dnd["__date"])) if not dnd.empty else set()
    dnd_pd     = set(zip(dnd["__name"], dnd["__date"]))                if not dnd.empty else set()
    act_full   = set(zip(act["__name"], act["__team"], act["__date"])) if not act.empty else set()
    act_pd     = set(zip(act["__name"], act["__date"]))                if not act.empty else set()

    def check(row):
        k_full = (row["__name"], row["__team"], row["__date"])
        k_pd   = (row["__name"], row["__date"])
        if not row["__name"] or not row["__date"]:
            return "MISSING_KEY"
        if k_full in dnd_full:
            return "OK_DND_exact"
        if k_full in act_full:
            return "OK_ACTIVE_exact"
        if k_pd in dnd_pd:
            return "WARN_DND_team_mismatch"
        if k_pd in act_pd:
            return "WARN_ACTIVE_team_mismatch"
        return "NOT_FOUND"

    out["verification"] = out.apply(check, axis=1)
    return out


# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("excel", help="Path to research Excel file")
    ap.add_argument("--sheet", default=None,
                    help="Sheet name (default: first sheet, "
                         "or 'Final_Locked_Dataset' if present)")
    ap.add_argument("--scrape-dir", default=".",
                    help="Folder containing dnd_players_*.csv and "
                         "active_players_*.csv (default: cwd)")
    ap.add_argument("--out", default="verification_report.xlsx",
                    help="Output Excel report path")
    args = ap.parse_args()

    in_path = Path(args.excel)
    if not in_path.exists():
        sys.exit(f"File not found: {in_path}")

    xl = pd.ExcelFile(in_path)
    sheet = args.sheet
    if sheet is None:
        sheet = "Final_Locked_Dataset" if "Final_Locked_Dataset" in xl.sheet_names \
                else xl.sheet_names[0]
    print(f"Reading sheet: {sheet}")
    df = pd.read_excel(in_path, sheet_name=sheet)
    print(f"  {len(df)} rows, {len(df.columns)} cols")

    cols = pick_columns(df)
    missing = [k for k, v in cols.items() if v is None and k != "status"]
    if missing:
        sys.exit(f"Could not find required columns: {missing}\n"
                 f"Available: {list(df.columns)}")
    print(f"Using columns: player={cols['player']!r}, team={cols['team']!r}, "
          f"date={cols['date']!r}, status={cols['status']!r}")

    print("\nLoading scraped data …")
    dnd, act = load_scraped(Path(args.scrape_dir))
    if dnd.empty and act.empty:
        sys.exit("No scraped CSVs found. Run scraper.py and "
                 "scraper_active.py for each season first.")

    print("\nVerifying …")
    result = verify_rows(df, cols, dnd, act)

    # Summary
    print("\n=== SUMMARY ===")
    print(result["verification"].value_counts())

    if cols["status"]:
        print("\nBy case/control status:")
        print(result.groupby([cols["status"], "verification"]).size()
              .unstack(fill_value=0))

    # Save report
    out_path = Path(args.out)
    with pd.ExcelWriter(out_path, engine="openpyxl") as w:
        result.to_excel(w, sheet_name="All_Rows", index=False)
        result[result["verification"] == "NOT_FOUND"].to_excel(
            w, sheet_name="NOT_FOUND", index=False
        )
        result[result["verification"].str.startswith("WARN")].to_excel(
            w, sheet_name="WARN_team_mismatch", index=False
        )
        result["verification"].value_counts().to_frame("count").to_excel(
            w, sheet_name="Summary"
        )

    print(f"\nReport saved → {out_path}")


if __name__ == "__main__":
    main()

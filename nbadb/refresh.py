"""What to (re)fetch on each run (review item 8).

update  - the last 3 days, plus every scheduled game that should have finished but
          isn't stored yet, plus any game from the last 60 days that failed a check.
audit   - re-pull a longer window (weekly: 14 days; monthly: the whole season) to catch
          late stat corrections. Changed values are saved in `revisions` before overwrite.
"""
from datetime import date, datetime, timedelta, timezone

from . import ingest
from .db import now_utc

RECENT_DAYS = 3
FAILED_LOOKBACK_DAYS = 60
FINISH_BUFFER_HOURS = 4  # a game is expected final 4h after tip-off


def _ranges(dates):
    """Group dates into contiguous (start, end) ranges."""
    dates = sorted(set(dates))
    out = []
    for d in dates:
        if out and (d - out[-1][1]).days <= 1:
            out[-1][1] = d
        else:
            out.append([d, d])
    return [tuple(r) for r in out]


def dates_to_fetch(con, today):
    dates = {today - timedelta(days=k) for k in range(RECENT_DAYS + 1)}
    # games that tipped off 4h+ ago (never later than the end of `today`, so a past `today` works for replays)
    now = min(datetime.now(timezone.utc),
              datetime(today.year, today.month, today.day, 23, 59, 59, tzinfo=timezone.utc))
    cutoff = (now - timedelta(hours=FINISH_BUFFER_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    unresolved = con.execute("""
        SELECT game_date FROM schedule s
        WHERE start_time_utc < ? AND game_id NOT IN (SELECT game_id FROM games)
          AND status NOT LIKE '%PPD%' AND status NOT LIKE '%Postponed%' AND status NOT LIKE '%Cancel%'
          AND game_date >= ?""", (cutoff, (today - timedelta(days=FAILED_LOOKBACK_DAYS)).isoformat())).fetchall()
    failed = con.execute("""
        SELECT DISTINCT g.game_date FROM game_checks c JOIN games g USING (game_id)
        WHERE c.passed = 0 AND c.severity = 'error' AND g.game_date >= ?""",
                         ((today - timedelta(days=FAILED_LOOKBACK_DAYS)).isoformat(),)).fetchall()
    extra = {date.fromisoformat(r[0]) for r in unresolved + failed}
    return sorted(dates | extra), len(unresolved), len(failed)


def fetch_dates(con, run, dates, rosters=True):
    touched = []
    for start, end in _ranges(dates):
        for season in sorted({ingest.season_for(start), ingest.season_for(end)}):
            for st in ingest.SEASON_TYPES:
                touched += ingest.load_games(con, run, season, st, start.strftime("%m/%d/%Y"),
                                             end.strftime("%m/%d/%Y"), rosters=rosters)
    return touched


def update(con, run, today=None):
    today = today or date.today()
    if con.execute("SELECT COUNT(*) FROM games").fetchone()[0] == 0:
        y = int(ingest.season_for(today)[:4])
        print("Database is empty; backfilling the current season first.")
        return backfill(con, run, y, y)
    dates, n_unres, n_failed = dates_to_fetch(con, today)
    print(f"  fetching {len(dates)} dates ({n_unres} unresolved games, {n_failed} dates with failed checks)")
    return fetch_dates(con, run, dates)


def audit(con, run, days=None, season=None, today=None):
    today = today or date.today()
    if season:
        y = int(season[:4])
        start, end = date(y, 9, 1), min(today, date(y + 1, 7, 31))
    else:
        start, end = today - timedelta(days=days or 14), today
    dates = [start + timedelta(days=k) for k in range((end - start).days + 1)]
    before = con.execute("SELECT COUNT(*) FROM revisions").fetchone()[0]
    touched = fetch_dates(con, run, dates, rosters=False)
    changed = con.execute("SELECT COUNT(*) FROM revisions").fetchone()[0] - before
    print(f"  audit {start}..{end}: {len(set(touched))} games re-checked, {changed} values corrected")
    return touched


def backfill(con, run, start_year, end_year, rosters_from=None):
    """Load whole seasons. Rosters (who sat and why: 2 extra calls per game) are
    fetched only for seasons >= rosters_from (default: the last 3 seasons)."""
    rosters_from = rosters_from if rosters_from is not None else end_year - 2
    touched = []
    for y in range(start_year, end_year + 1):
        season = f"{y}-{str(y + 1)[-2:]}"
        ingest.load_schedule(con, run, season)
        for st in ingest.SEASON_TYPES:
            touched += ingest.load_games(con, run, season, st, rosters=y >= rosters_from)
    return touched

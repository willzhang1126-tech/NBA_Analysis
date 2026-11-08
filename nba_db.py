"""NBA stats database: command line.

    python nba_db.py backfill 2021 2026     # whole seasons; rosters (who sat, why) for the last 3
    python nba_db.py daily                  # the daily job (see below)
    python nba_db.py update                 # games: last 3 days + unresolved + failed checks
    python nba_db.py audit --days 14        # re-pull a window to catch late corrections
    python nba_db.py audit --season 2026-27
    python nba_db.py injuries               # capture the injury report now (run every 1-2h on game days)
    python nba_db.py odds                   # capture Polymarket order books now (run every 30-60 min on game days)
    python nba_db.py profile                # rebuild injury-prone scores
    python nba_db.py features GAME_ID       # point-in-time features for one game
    python nba_db.py export                 # nba_stats.xlsx
    python nba_db.py status

The database is one SQLite file: nba.db (set NBA_DB_PATH to change).
"""
import argparse
import json
import sys
from datetime import date

from nbadb import db, export, features, ingest, injury_score, refresh


def daily(con, run, today=None):
    """schedule -> games (+rosters, checks) -> audits on schedule -> injuries -> scores -> odds -> spreadsheet.
    One failing step doesn't stop the others; every step is logged in source_runs."""
    today = today or date.today()
    season = ingest.season_for(today)
    steps = [
        ("schedule", lambda: ingest.load_schedule(con, run, season)),
        ("games", lambda: refresh.update(con, run, today)),
        ("weekly audit", lambda: refresh.audit(con, run, days=14, today=today) if today.weekday() == 0 else None),
        ("monthly audit", lambda: refresh.audit(con, run, season=season, today=today) if today.day == 1 else None),
        ("injury report", lambda: ingest.capture_injuries(con, run)),
        ("injury scores", lambda: injury_score.compute(con)),
        ("polymarket", lambda: ingest.capture_odds(con, run)),
        ("spreadsheet", lambda: export.export(con)),
    ]
    for name, fn in steps:
        print(f"== {name}")
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            print(f"   FAILED: {e!r}")
            run.log(name, "", "error", error=e)


def status(con):
    for t in ["teams", "players", "schedule", "games", "team_game_stats", "player_game_stats",
              "player_game_status", "game_checks", "injury_observations", "player_injury_profile",
              "polymarket_quotes", "model_predictions", "revisions"]:
        print(f"{t:24s} {con.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]:>9}")
    print("latest game:", con.execute("SELECT MAX(game_date) FROM games").fetchone()[0])
    print("failed checks:", con.execute("SELECT COUNT(*) FROM data_check_failures").fetchone()[0])
    for r in con.execute("SELECT run_id, command, started_at, status FROM runs ORDER BY run_id DESC LIMIT 3"):
        print("run:", r)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backfill")
    b.add_argument("start", type=int)
    b.add_argument("end", type=int, nargs="?")
    b.add_argument("--rosters-from", type=int, help="first season (start year) to load rosters for")
    sub.add_parser("daily")
    sub.add_parser("update")
    a = sub.add_parser("audit")
    a.add_argument("--days", type=int, default=14)
    a.add_argument("--season")
    sub.add_parser("injuries")
    sub.add_parser("odds")
    sub.add_parser("profile")
    f = sub.add_parser("features")
    f.add_argument("game_id")
    f.add_argument("--cutoff", help="ISO UTC time; default tip-off")
    e = sub.add_parser("export")
    e.add_argument("--out", default="nba_stats.xlsx")
    e.add_argument("--season")
    sub.add_parser("status")
    args = ap.parse_args(argv)

    con = db.connect()
    if args.cmd == "status":
        return status(con)
    if args.cmd == "features":
        print(json.dumps(features.game_features(con, args.game_id, args.cutoff), indent=2, default=str))
        return
    if args.cmd == "export":
        return export.export(con, args.out, args.season)
    run = db.Run(con, args.cmd)
    try:
        if args.cmd == "backfill":
            end = args.end or int(ingest.season_for(date.today())[:4])
            refresh.backfill(con, run, args.start, end, args.rosters_from)
        elif args.cmd == "daily":
            daily(con, run)
        elif args.cmd == "update":
            ingest.load_schedule(con, run, ingest.season_for(date.today()))
            refresh.update(con, run)
        elif args.cmd == "audit":
            refresh.audit(con, run, days=None if args.season else args.days, season=args.season)
        elif args.cmd == "injuries":
            ingest.capture_injuries(con, run)
        elif args.cmd == "odds":
            ingest.capture_odds(con, run)
        elif args.cmd == "profile":
            injury_score.compute(con)
    finally:
        run.finish()
    bad = con.execute("SELECT source, detail, error FROM source_runs WHERE run_id = ? AND status = 'error'",
                      (run.id,)).fetchall()
    for s in bad:
        print("SOURCE ERROR:", s)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

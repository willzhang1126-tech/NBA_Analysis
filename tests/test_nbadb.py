"""Offline tests with fake data sources (no network).  Run:  python tests/test_nbadb.py
The numbers here are made up; they only exercise the logic."""
import json
import os
import random
import sqlite3
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pandas as pd  # noqa: E402

from nbadb import db, features, ingest, injury_score, predictions, refresh, sources  # noqa: E402
from nbadb import export as export_mod  # noqa: E402

sources.REQUEST_PAUSE = 0
TEAMS = [(1, "BOS", "Boston", "Celtics"), (2, "NYK", "New York", "Knicks"),
         (3, "LAL", "Los Angeles", "Lakers"), (4, "GSW", "Golden State", "Warriors")]
# game_id, date, home idx, away idx, home pts, away pts, start UTC
GAMES = [
    ("0022600001", "2026-10-20", 0, 1, 112, 104),
    ("0022600002", "2026-10-20", 2, 3, 120, 118),
    ("0022600003", "2026-10-22", 1, 2, 99, 101),
    ("0022600004", "2026-10-23", 3, 0, 108, 111),
    ("0022600005", "2026-10-25", 0, 2, 115, 109),
    ("0022600006", "2026-10-26", 1, 3, 102, 97),
    ("0022600007", "2026-10-29", 0, 3, None, None),   # upcoming
    ("0022600008", "2026-10-30", 2, 1, None, None),   # upcoming
]
STATE = {"available": {g[0] for g in GAMES if g[4] is not None}, "broken": set(), "corrections": {}}


def start_of(d):
    return f"{d}T23:30:00Z"


def player_lines(gid, ti, pts):
    rnd = random.Random(f"{gid}-{ti}")
    out, left = [], pts
    for k in range(8):
        tgt = left if k == 7 else min(left, rnd.randint(6, 24))
        f3 = rnd.randint(0, tgt // 3)
        rest = tgt - 3 * f3
        f2, ft = rest // 2, rest - 2 * (rest // 2)
        out.append(dict(PLAYER_ID=TEAMS[ti][0] * 100 + k, PLAYER_NAME=f"{TEAMS[ti][1]} Player{k}",
                        FGM=f2 + f3, FGA=f2 + f3 + 5, FG3M=f3, FG3A=f3 + 3, FTM=ft, FTA=ft + 1,
                        PTS=tgt, MIN=30, OREB=1, DREB=4, REB=5, AST=3, STL=1, BLK=1, TOV=2, PF=2))
        left -= tgt
    return out


def fake_game_log(season, season_type, kind, date_from="", date_to=""):
    if season_type != "Regular Season":
        return pd.DataFrame()
    lo = pd.Timestamp(date_from) if date_from else pd.Timestamp.min
    hi = pd.Timestamp(date_to) if date_to else pd.Timestamp.max
    rows = []
    for gid, d, h, a, hp, ap in GAMES:
        if gid not in STATE["available"] or not (lo <= pd.Timestamp(d) <= hi):
            continue
        for ti, oi, pts, opts, home in ((h, a, hp, ap, 1), (a, h, ap, hp, 0)):
            t, o = TEAMS[ti], TEAMS[oi]
            base = dict(TEAM_ID=t[0], TEAM_ABBREVIATION=t[1], TEAM_NAME=f"{t[2]} {t[3]}", GAME_ID=gid,
                        GAME_DATE=d, MATCHUP=f"{t[1]} vs. {o[1]}" if home else f"{t[1]} @ {o[1]}",
                        WL="W" if pts > opts else "L", PLUS_MINUS=pts - opts)
            lines = player_lines(gid, ti, pts)
            if (gid, ti) in STATE["corrections"]:          # late stat correction to one player
                lines[0]["AST"] += STATE["corrections"][(gid, ti)]
            if kind == "T":
                r = dict(base, MIN=240)
                for c in ["FGM", "FGA", "FG3M", "FG3A", "FTM", "FTA", "OREB", "DREB", "REB", "AST", "STL", "BLK",
                          "TOV", "PF", "PTS"]:
                    r[c] = sum(x[c] for x in lines)
                rows.append(r)
            else:
                for k, x in enumerate(lines):
                    r = dict(base, **x)
                    if (gid, ti) in STATE["broken"] and k == 0:  # player line loses a free throw
                        r["FTM"] -= 1
                        r["PTS"] -= 1
                    rows.append(r)
    return pd.DataFrame(rows)


def fake_schedule(season):
    rows = []
    for gid, d, h, a, hp, ap in GAMES:
        ht, at = TEAMS[h], TEAMS[a]
        rows.append(dict(gameId=gid, gameDateEst=f"{d}T00:00:00Z", gameDateTimeUTC=start_of(d), gameLabel="",
                         gameStatusText="Final" if hp is not None else "7:30 pm ET",
                         homeTeam_teamId=ht[0], homeTeam_teamTricode=ht[1], homeTeam_teamCity=ht[2],
                         homeTeam_teamName=ht[3], awayTeam_teamId=at[0], awayTeam_teamTricode=at[1],
                         awayTeam_teamCity=at[2], awayTeam_teamName=at[3]))
    return pd.DataFrame(rows)


def fake_boxscore_roster(gid):
    g = next(x for x in GAMES if x[0] == gid)
    rows, inactive = [], []
    for ti, pts in ((g[2], g[4]), (g[3], g[5])):
        t = TEAMS[ti]
        for x in player_lines(gid, ti, pts):
            rows.append(dict(teamId=t[0], personId=x["PLAYER_ID"], firstName=t[1], familyName=f"Player{x['PLAYER_ID'] % 100}",
                             comment="", minutes="30:00"))
        # bench player 8 always DNP coach; player 9 out injured (DND) in every BOS game
        rows.append(dict(teamId=t[0], personId=t[0] * 100 + 8, firstName=t[1], familyName="Player8",
                         comment="DNP - Coach's Decision", minutes=""))
        if t[1] == "BOS":
            rows.append(dict(teamId=t[0], personId=t[0] * 100 + 9, firstName=t[1], familyName="Player9",
                             comment="DND - Injury/Illness", minutes=""))
        else:
            inactive.append(dict(teamId=t[0], personId=t[0] * 100 + 9, firstName=t[1], familyName="Player9"))
    return pd.DataFrame(rows), pd.DataFrame(inactive)


INJ = {"rows": []}


def fake_espn():
    return pd.DataFrame(INJ["rows"])


BOOK_FAIL = {"on": False}


def fake_events():
    return [
        {"slug": "nba-gsw-bos-2026-10-29", "markets": [
            {"id": "m-spread", "outcomes": json.dumps(["Celtics -3.5", "Warriors +3.5"]),
             "outcomePrices": json.dumps(["0.5", "0.5"])},
            {"id": "m1", "conditionId": "c1", "question": "Warriors vs. Celtics",
             "outcomes": json.dumps(["Warriors", "Celtics"]), "outcomePrices": json.dumps(["0.40", "0.61"]),
             "clobTokenIds": json.dumps(["tok-gsw", "tok-bos"]), "bestBid": "0.39", "bestAsk": "0.41",
             "volume": "50000", "liquidity": "9000", "lastTradePrice": "0.40"}]},
        {"slug": "nba-champion-2027", "markets": []},
    ]


def fake_book(token):
    if BOOK_FAIL["on"]:
        raise RuntimeError("book down")
    if token == "tok-bos":
        return {"bids": [{"price": "0.60", "size": "500"}], "asks": [{"price": "0.62", "size": "300"},
                                                                       {"price": "0.63", "size": "1000"}]}
    return {"bids": [{"price": "0.38", "size": "400"}], "asks": [{"price": "0.40", "size": "250"}]}


sources.game_log = fake_game_log
sources.schedule = fake_schedule
sources.boxscore_roster = fake_boxscore_roster
sources.espn_injuries = fake_espn
sources.polymarket_nba_events = fake_events
sources.polymarket_book = fake_book


def q(con, sql, *a):
    return con.execute(sql, a).fetchall()


def main():
    tmp = tempfile.mkdtemp()
    con = db.connect(os.path.join(tmp, "t.db"))
    STATE["broken"] = {("0022600003", 1)}         # NYK's player lines in game 3 are 1 point short
    STATE["available"] -= {"0022600006"}           # game 6 not published yet on first load
    run = db.Run(con, "backfill")
    refresh.backfill(con, run, 2026, 2026)
    run.finish()

    # --- loading and checks
    assert q(con, "SELECT COUNT(*) FROM games")[0][0] == 5
    assert q(con, "SELECT COUNT(*) FROM games WHERE start_time_utc IS NULL")[0][0] == 0
    src = dict(q(con, "SELECT team_id, source_used FROM team_game_shooting WHERE game_id = '0022600003'"))
    assert src == {2: "official_fallback", 3: "players"}, src
    fails = {r[0] for r in q(con, "SELECT check_name FROM data_check_failures WHERE game_id = '0022600003'")}
    # the bad line is internally consistent (FTM and PTS both short), so arithmetic passes
    assert fails == {"player_vs_score", "player_vs_team_box"}, fails
    nyk3 = q(con, "SELECT pts, pts_2pt + pts_3pt + pts_ft FROM team_game_scoring WHERE game_id='0022600003' AND team='NYK'")[0]
    assert nyk3[0] == nyk3[1] == 99, nyk3     # fallback numbers still add up to the real score
    print("ok  checks: bad player lines flagged, official fallback used, totals add up")

    # --- absence types
    st = dict(q(con, "SELECT status || '/' || reason, COUNT(*) FROM player_game_status GROUP BY 1"))
    assert st["dnp/coach"] == 10 and st["inactive/injury"] == 3 and st["inactive/unknown"] == 7, st
    print("ok  rosters: played / DNP-coach / inactive-injury / inactive-unknown recorded", st)

    # --- injury observations: transitions only, cleared when removed
    INJ["rows"] = [dict(team_name="Boston Celtics", player_name="BOS Player0", status="Questionable", injury="Ankle",
                        detail="sprain", expected_return="", source_updated_at="")]
    run = db.Run(con, "injuries")
    ingest.capture_injuries(con, run, "2026-10-28T15:00:00Z")
    ingest.capture_injuries(con, run, "2026-10-28T17:00:00Z")          # unchanged -> no new row
    INJ["rows"][0]["status"] = "Out"
    ingest.capture_injuries(con, run, "2026-10-29T21:00:00Z")          # changed -> new row
    INJ["rows"] = []
    ingest.capture_injuries(con, run, "2026-10-31T15:00:00Z")          # removed -> Cleared
    obs = [r[0] for r in q(con, "SELECT status FROM injury_observations ORDER BY captured_at")]
    assert obs == ["Questionable", "Out", "Cleared"], obs
    s1, _ = features.injury_status_asof(con, "2026-10-29T12:00:00Z")
    s2, _ = features.injury_status_asof(con, "2026-10-29T23:00:00Z")
    s3, _ = features.injury_status_asof(con, "2026-11-01T00:00:00Z")
    assert list(s1.status) == ["Questionable"] and list(s2.status) == ["Out"] and s3.empty
    assert q(con, "SELECT player_id FROM injury_observations LIMIT 1")[0][0] == 100
    print("ok  injuries: status history kept, as-of lookups return the status known at that time")

    # --- update: unresolved game outside the 3-day window, a stat correction, idempotence
    STATE["available"].add("0022600006")
    STATE["corrections"][("0022600001", 0)] = 2      # AST correction on Oct 20 game (old)
    run = db.Run(con, "update")
    dates, n_unres, _ = refresh.dates_to_fetch(con, date(2026, 11, 2))
    # game 6 plus the two later games (never published in the fake feed) are all past tip-off on Nov 2
    assert n_unres == 3 and date(2026, 10, 26) in dates
    fin_before = q(con, "SELECT finalized_at FROM games WHERE game_id='0022600001'")[0][0]
    refresh.update(con, run, date(2026, 11, 2))
    assert q(con, "SELECT COUNT(*) FROM games")[0][0] == 6
    assert q(con, "SELECT COUNT(*) FROM revisions")[0][0] == 0      # Oct 20 is outside the update window
    refresh.audit(con, run, days=14, today=date(2026, 11, 2))
    revs = q(con, "SELECT table_name, column, old_value, new_value FROM revisions")
    assert len(revs) == 2 and all(r[1] == "ast" for r in revs), revs   # team row + player row
    assert q(con, "SELECT finalized_at FROM games WHERE game_id='0022600001'")[0][0] == fin_before
    n = q(con, "SELECT COUNT(*) FROM revisions")[0][0]
    refresh.audit(con, run, days=14, today=date(2026, 11, 2))
    assert q(con, "SELECT COUNT(*) FROM revisions")[0][0] == n      # re-run changes nothing
    run.finish()
    print("ok  refresh: unresolved game picked up, old correction caught by audit with history, re-runs are no-ops")

    # --- point-in-time features
    f_before = features.team_form_asof(con, "2026-10-22T00:00:00Z")
    assert f_before.loc[1, "gp"] == 1 and 3 not in f_before.index or f_before.loc[3, "gp"] == 1
    f_all = features.team_form_asof(con, "2026-10-28T00:00:00Z")
    assert f_all.loc[1, "gp"] == 3
    gf = features.game_features(con, "0022600007", cutoff="2026-10-29T22:00:00Z")
    assert gf["home_players_out"] == 1 and gf["home_expected_min_lost"] > 0
    assert gf["home_gp"] == 3.0
    gf_early = features.game_features(con, "0022600007", cutoff="2026-10-29T12:00:00Z")
    assert gf_early["home_players_out"] == 0 and gf_early["home_expected_min_lost"] < gf["home_expected_min_lost"]
    print("ok  features: only games before the cutoff, injury status as of the cutoff")

    # --- injury score
    prof = injury_score.compute(con).set_index("player_id")
    assert prof.loc[109, "absences_injury"] == 3 and prof.loc[108, "absences_injury"] == 0
    assert prof.loc[108, "absences_other"] == 3               # coach's decision isn't injury
    assert prof.loc[109, "injury_rate"] > prof.loc[209, "injury_rate"] > prof.loc[108, "injury_rate"]
    assert prof.injury_prone.dropna().between(0, 1).all() or prof.injury_prone.isna().all()
    print("ok  injury score: injury absences > unknown inactive > coach DNP; 0-1 scale")

    # --- Polymarket quotes and model vs market
    run = db.Run(con, "odds")
    ingest.capture_odds(con, run, "2026-10-29T18:00:00Z")
    qt = {r[0]: r[1:] for r in q(con, "SELECT side, best_bid, best_ask, ask_size, mid, quote_source FROM polymarket_quotes")}
    assert qt["home"] == (0.60, 0.62, 300.0, 0.61, "clob") and qt["away"][1] == 0.40, qt
    mk = q(con, "SELECT home_token_id, away_token_id FROM polymarket_markets")[0]
    assert mk == ("tok-bos", "tok-gsw")
    f = features.game_features(con, "0022600007", cutoff="2026-10-29T18:30:00Z")
    pid = predictions.record(con, "0022600007", 0.61, "test-v0", f, predicted_at="2026-10-29T18:30:00Z")
    mv = pd.read_sql("SELECT * FROM model_vs_market", con).iloc[0]
    assert abs(mv.ev_home - (0.61 - 0.62)) < 1e-9 and mv.action == "NO BET", mv   # the review's example
    assert abs(mv.forecast_gap - (0.61 - 0.61 / (0.61 + 0.39))) < 1e-3
    predictions.record(con, "0022600007", 0.70, "test-v0", f, predicted_at="2026-10-29T19:00:00Z")
    mv = pd.read_sql("SELECT * FROM model_vs_market", con).iloc[0]
    assert mv.action == "BUY HOME" and abs(mv.ev_home - 0.08) < 1e-9
    try:
        con.execute("UPDATE model_predictions SET home_win_prob = 0.9 WHERE prediction_id = ?", (pid,))
        raise AssertionError("prediction was editable")
    except sqlite3.IntegrityError:
        pass
    try:
        predictions.record(con, "0022600007", 0.5, "x", dict(f, cutoff="2026-10-30T00:00:00Z"),
                           predicted_at="2026-10-29T19:00:00Z")
        raise AssertionError("allowed cutoff after prediction time")
    except ValueError:
        pass
    BOOK_FAIL["on"] = True
    ingest.capture_odds(con, run, "2026-10-29T19:30:00Z")
    assert q(con, "SELECT quote_source FROM polymarket_quotes WHERE captured_at = '2026-10-29T19:30:00Z' LIMIT 1")[0][0] == "gamma"
    predictions.record(con, "0022600007", 0.70, "test-v0", f, predicted_at="2026-10-29T19:45:00Z")
    mv = pd.read_sql("SELECT * FROM model_vs_market", con).iloc[0]
    assert mv.action is None                                   # no tradable quote -> no bet signal
    run.finish()
    print("ok  market: bid/ask/size stored, EV vs ask (review example = NO BET), predictions append-only")

    # --- spreadsheet and logs
    out = os.path.join(tmp, "x.xlsx")
    export_mod.export(con, out)
    import openpyxl
    assert "Data checks" in openpyxl.load_workbook(out).sheetnames
    assert q(con, "SELECT COUNT(*) FROM source_runs WHERE source = 'polymarket' AND status = 'error'")[0][0] == 1
    print("ok  spreadsheet written; source log records the order-book failure")
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()

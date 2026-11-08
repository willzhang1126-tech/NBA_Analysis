"""Load games, schedule, rosters, injury reports and Polymarket quotes."""
import json
import re
import unicodedata
from datetime import date, datetime, timedelta

import pandas as pd

from . import checks, sources
from .db import now_utc, upsert

SEASON_TYPES = ["Regular Season", "PlayIn", "Playoffs"]
STAT_COLS = ["fgm", "fga", "fg3m", "fg3a", "ftm", "fta", "oreb", "dreb", "reb",
             "ast", "stl", "blk", "tov", "pf", "pts", "plus_minus"]


def season_for(d):
    y = d.year if d.month >= 9 else d.year - 1
    return f"{y}-{str(y + 1)[-2:]}"


def norm_name(n):
    n = unicodedata.normalize("NFKD", n or "").encode("ascii", "ignore").decode().lower()
    n = re.sub(r"[^a-z0-9 ]", "", n)
    return " ".join(w for w in n.split() if w not in {"jr", "sr", "ii", "iii", "iv"})


# ================================================================ schedule

def load_schedule(con, run, season):
    try:
        df = sources.schedule(season)
    except RuntimeError as e:
        run.log("nba_schedule", season, "error", error=e)
        return 0
    if df.empty:
        run.log("nba_schedule", season, fetched=0)
        return 0
    label = df.get("gameLabel", pd.Series([""] * len(df))).fillna("")
    gid = df["gameId"].astype(str)
    stype = pd.Series("Regular Season", index=df.index)
    stype[gid.str[2] == "1"] = "Pre Season"
    stype[gid.str[2] == "4"] = "Playoffs"
    stype[gid.str[2] == "5"] = "PlayIn"
    stype[label.str.contains("All-Star", case=False)] = "All Star"
    out = pd.DataFrame({
        "game_id": gid, "season": season, "season_type": stype,
        "game_date": pd.to_datetime(df["gameDateEst"]).dt.strftime("%Y-%m-%d"),
        "start_time_utc": pd.to_datetime(df["gameDateTimeUTC"], utc=True).dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "home_team_id": df["homeTeam_teamId"], "away_team_id": df["awayTeam_teamId"],
        "status": df["gameStatusText"], "last_updated": now_utc(),
    })
    out = out[(out.home_team_id > 0) & (out.away_team_id > 0) & out.season_type.isin(SEASON_TYPES)]
    teams = pd.concat([
        pd.DataFrame({"team_id": df[f"{s}Team_teamId"], "abbreviation": df[f"{s}Team_teamTricode"],
                      "name": df[f"{s}Team_teamCity"] + " " + df[f"{s}Team_teamName"]})
        for s in ("home", "away")]).drop_duplicates("team_id")
    with con:
        con.executemany("INSERT OR IGNORE INTO teams VALUES (?,?,?)",
                        [tuple(r) for r in teams[teams.team_id > 0].itertuples(index=False)])
        w, _ = upsert(con, "schedule", out, ["game_id"])
        # copy tip-off times onto stored games
        con.execute("""UPDATE games SET start_time_utc =
                         (SELECT s.start_time_utc FROM schedule s WHERE s.game_id = games.game_id)
                       WHERE start_time_utc IS NULL
                         AND game_id IN (SELECT game_id FROM schedule)""")
    run.log("nba_schedule", season, fetched=len(df), written=w)
    return w


# ================================================================ games & box scores

def load_games(con, run, season, season_type, date_from="", date_to="", rosters=True):
    """Fetch team + player logs for a season (optionally a date range), store them,
    fetch rosters/absences for each game, and run the checks. Returns games touched."""
    detail = f"{season} {season_type} {date_from}..{date_to}"
    try:
        t = sources.game_log(season, season_type, "T", date_from, date_to)
        p = sources.game_log(season, season_type, "P", date_from, date_to) if not t.empty else pd.DataFrame()
    except RuntimeError as e:
        if season_type == "PlayIn":
            run.log("nba_gamelog", detail, "ok", error="no play-in data")
            return []
        run.log("nba_gamelog", detail, "error", error=e)
        return []
    if t.empty:
        run.log("nba_gamelog", detail, fetched=0)
        return []
    t = t.copy()
    t.columns = [c.lower() for c in t.columns]
    t["game_date"] = pd.to_datetime(t["game_date"]).dt.strftime("%Y-%m-%d")
    t["is_home"] = t["matchup"].str.contains("vs.").astype(int)
    complete = (t.groupby("game_id")["team_id"].transform("count") == 2) & t["wl"].notna()
    t = t[complete]
    if t.empty:
        run.log("nba_gamelog", detail, fetched=0)
        return []
    opp = t[["game_id", "team_id"]].rename(columns={"team_id": "opponent_team_id"})
    t = t.merge(opp, on="game_id")
    t = t[t.team_id != t.opponent_team_id]

    home = t[t.is_home == 1].set_index("game_id")
    away = t[t.is_home == 0].set_index("game_id")
    starts = dict(con.execute("SELECT game_id, start_time_utc FROM schedule"))
    ts = now_utc()
    g = pd.DataFrame({
        "season": season, "season_type": season_type, "game_date": home["game_date"],
        "home_team_id": home["team_id"], "away_team_id": away["team_id"],
        "home_pts": home["pts"], "away_pts": away["pts"],
    }).dropna().reset_index()
    g["start_time_utc"] = g["game_id"].map(starts)
    g["home_win"] = (g["home_pts"] > g["away_pts"]).astype(int)
    g["finalized_at"] = ts
    g["last_updated"] = ts
    game_ids = list(g["game_id"])

    with con:
        con.executemany("INSERT OR IGNORE INTO teams VALUES (?,?,?)",
                        list(t[["team_id", "team_abbreviation", "team_name"]].drop_duplicates("team_id")
                             .itertuples(index=False, name=None)))
        before = {r[0] for r in con.execute(
            f"SELECT game_id FROM games WHERE game_id IN ({','.join('?' * len(game_ids))})", game_ids)}
        _, ch_g = upsert(con, "games", g, ["game_id"], run.id, keep_cols=("finalized_at", "last_updated"))
        w_t, ch_t = upsert(con, "team_game_stats",
                           t[["game_id", "team_id", "is_home", "opponent_team_id", "wl", "min"] + STAT_COLS],
                           ["game_id", "team_id"], run.id)
        w_p = ch_p = 0
        if not p.empty:
            p = p.copy()
            p.columns = [c.lower() for c in p.columns]
            p = p[p["game_id"].isin(set(game_ids))]
            con.executemany("INSERT INTO players VALUES (?,?) ON CONFLICT (player_id) DO UPDATE SET name = excluded.name",
                            list(p[["player_id", "player_name"]].drop_duplicates("player_id")
                                 .itertuples(index=False, name=None)))
            w_p, ch_p = upsert(con, "player_game_stats",
                               p[["game_id", "player_id", "team_id", "min"] + STAT_COLS],
                               ["game_id", "player_id"], run.id)
            played = p[["game_id", "player_id", "team_id"]].assign(
                status="played", reason="none", raw_comment="", source="gamelog")
            # don't overwrite richer boxscore rows with plain gamelog rows
            con.executemany("""INSERT OR IGNORE INTO player_game_status VALUES (?,?,?,?,?,?,?)""",
                            list(played.itertuples(index=False, name=None)))
        # last_updated moves only for games where some value changed in this run
        con.execute("""UPDATE games SET last_updated = ? WHERE game_id IN
                         (SELECT DISTINCT substr(row_key, 1, 10) FROM revisions WHERE run_id = ?
                            AND table_name IN ('games', 'team_game_stats', 'player_game_stats'))""",
                    (ts, run.id))
    run.log("nba_gamelog", detail, fetched=len(t) + len(p), written=len(g) + w_t + w_p,
            changed=ch_g + ch_t + ch_p)
    new_games = [x for x in game_ids if x not in before]
    if rosters:
        load_rosters(con, run, new_games)
    checks.run_checks(con, run, game_ids)
    return game_ids


# ================================================================ rosters / absences (review item 5)

def classify(comment, minutes):
    c = (comment or "").lower()
    if not c:
        played = bool(minutes) and str(minutes) not in ("", "0", "00:00", "PT00M00.00S", "None")
        return ("played", "none") if played else ("dnp", "unknown")
    if c.startswith("dnd"):
        status = "inactive"
    elif c.startswith("nwt"):
        status = "not_with_team"
    else:
        status = "dnp"
    if "injur" in c or "illness" in c or "sick" in c:
        reason = "injury"
    elif "coach" in c:
        reason = "coach"
    elif "rest" in c or "load management" in c:
        reason = "rest"
    elif "suspen" in c:
        reason = "suspension"
    elif "personal" in c or "family" in c or "bereave" in c:
        reason = "personal"
    elif "league" in c or "assign" in c or "two-way" in c or "two way" in c:
        reason = "assignment"
    else:
        reason = "unknown"
    return status, reason


def load_rosters(con, run, game_ids):
    """For each game, record every rostered player and why they did or didn't play."""
    ok = fail = 0
    for gid in game_ids:
        try:
            box, inactive = sources.boxscore_roster(gid)
        except RuntimeError as e:
            fail += 1
            run.log("nba_boxscore", gid, "error", error=e)
            continue
        start = con.execute("SELECT COALESCE(start_time_utc, game_date || 'T23:59:59Z') FROM games WHERE game_id = ?",
                            (gid,)).fetchone()
        start = start[0] if start else now_utc()
        rows, names = [], []
        seen = set()
        for r in box.itertuples(index=False):
            status, reason = classify(getattr(r, "comment", ""), getattr(r, "minutes", ""))
            rows.append((gid, int(r.personId), int(r.teamId), status, reason, getattr(r, "comment", "") or "", "boxscore"))
            names.append((int(r.personId), f"{r.firstName} {r.familyName}".strip()))
            seen.add(int(r.personId))
        for r in inactive.itertuples(index=False) if not inactive.empty else []:
            pid = int(r.personId)
            if pid in seen:
                continue
            name = f"{r.firstName} {r.familyName}".strip()
            reason, src = _reason_from_injury_report(con, int(r.teamId), pid, name, start)
            rows.append((gid, pid, int(r.teamId), "inactive", reason, "", src))
            names.append((pid, name))
        with con:
            con.executemany("INSERT OR IGNORE INTO players VALUES (?,?)", names)
            df = pd.DataFrame(rows, columns=["game_id", "player_id", "team_id", "status", "reason",
                                             "raw_comment", "source"])
            upsert(con, "player_game_status", df, ["game_id", "player_id"], run.id)
        ok += 1
    if game_ids:
        run.log("nba_boxscore", f"{len(game_ids)} games", "ok" if not fail else "error",
                fetched=ok, written=ok, error=f"{fail} failed" if fail else None)


def _reason_from_injury_report(con, team_id, player_id, name, start):
    """Reason for an inactive player = latest injury report status before tip-off, if any."""
    r = con.execute("""SELECT status, injury FROM injury_observations
                       WHERE team_id = ? AND (player_id = ? OR player_name = ?) AND captured_at <= ?
                       ORDER BY captured_at DESC LIMIT 1""", (team_id, player_id, name, start)).fetchone()
    if r and r[0] in ("Out", "Doubtful", "Questionable", "Day-To-Day"):
        if "rest" in (r[1] or "").lower():
            return "rest", "injury_report"
        return "injury", "injury_report"
    return "unknown", "inactive_list"


# ================================================================ injury reports (review item 4)

def capture_injuries(con, run, captured_at=None):
    captured_at = captured_at or now_utc()
    try:
        df = sources.espn_injuries()
    except RuntimeError as e:
        run.log("espn_injuries", "", "error", error=e)
        with con:
            con.execute("INSERT OR REPLACE INTO injury_captures VALUES (?,?,?,?)", (captured_at, "espn", 0, "error"))
        return 0
    cols = ["team_name", "player_name", "status", "injury", "detail", "expected_return", "source_updated_at"]
    df = df.reindex(columns=cols) if df.empty else df.copy()
    prev = con.execute("SELECT players_listed FROM injury_captures WHERE source = 'espn' AND status = 'ok' "
                       "ORDER BY captured_at DESC LIMIT 1").fetchone()
    if df.empty and prev and prev[0] > 5:
        # a league-wide empty report is almost certainly a feed glitch: don't clear everyone
        run.log("espn_injuries", "empty report ignored", "error", error="empty report")
        with con:
            con.execute("INSERT OR REPLACE INTO injury_captures VALUES (?,?,?,?)", (captured_at, "espn", 0, "error"))
        return 0
    teams = {norm_name(n): tid for tid, n in con.execute("SELECT team_id, name FROM teams")}
    players = {}
    for pid, n in con.execute("SELECT player_id, name FROM players"):
        players.setdefault(norm_name(n), pid)
    df["team_id"] = df["team_name"].map(lambda n: teams.get(norm_name(n)))
    unmatched_teams = int(df["team_id"].isna().sum())
    df = df[df["team_id"].notna()]
    df["player_id"] = df["player_name"].map(lambda n: players.get(norm_name(n)))

    latest = {(r[0], r[1]): r[2:] for r in con.execute("""
        SELECT team_id, player_name, status, injury, detail, expected_return FROM injury_observations o
        WHERE source = 'espn' AND captured_at = (SELECT MAX(captured_at) FROM injury_observations o2
              WHERE o2.source = o.source AND o2.team_id = o.team_id AND o2.player_name = o.player_name)""")}
    new = []
    listed = set()
    for r in df.itertuples(index=False):
        key = (int(r.team_id), r.player_name)
        listed.add(key)
        vals = (r.status, r.injury, r.detail, r.expected_return)
        if latest.get(key) != vals:
            new.append((captured_at, "espn", int(r.team_id), None if pd.isna(r.player_id) else int(r.player_id),
                        r.player_name, *vals, r.source_updated_at))
    for key, vals in latest.items():
        if key not in listed and vals[0] != "Cleared":
            pid = con.execute("SELECT player_id FROM injury_observations WHERE team_id = ? AND player_name = ? "
                              "AND player_id IS NOT NULL LIMIT 1", key).fetchone()
            new.append((captured_at, "espn", key[0], pid[0] if pid else None, key[1],
                        "Cleared", "", "removed from report", "", ""))
    with con:
        con.executemany("INSERT OR IGNORE INTO injury_observations VALUES (?,?,?,?,?,?,?,?,?,?)", new)
        con.execute("INSERT OR REPLACE INTO injury_captures VALUES (?,?,?,?)", (captured_at, "espn", len(df), "ok"))
    run.log("espn_injuries", f"{unmatched_teams} unmatched team rows" if unmatched_teams else "",
            fetched=len(df), written=len(new))
    return len(new)


# ================================================================ Polymarket (review item 3)

def _json_list(v):
    return json.loads(v) if isinstance(v, str) else (v or [])


def _book_summary(book):
    bids = sorted(((float(b["price"]), float(b["size"])) for b in book.get("bids", [])), reverse=True)
    asks = sorted((float(a["price"]), float(a["size"])) for a in book.get("asks", []))
    best_bid = bids[0][0] if bids else None
    best_ask = asks[0][0] if asks else None
    return {
        "best_bid": best_bid, "best_ask": best_ask,
        "ask_size": asks[0][1] if asks else None,
        "ask_depth_usd": round(sum(p * s for p, s in asks if p <= best_ask + 0.02), 2) if asks else 0.0,
        "bid_depth_usd": round(sum(p * s for p, s in bids if p >= best_bid - 0.02), 2) if bids else 0.0,
        "mid": round((best_bid + best_ask) / 2, 4) if bids and asks else None,
    }


def capture_odds(con, run, captured_at=None):
    captured_at = captured_at or now_utc()
    try:
        events = sources.polymarket_nba_events()
    except RuntimeError as e:
        run.log("polymarket_gamma", "", "error", error=e)
        return 0
    teams = pd.read_sql("SELECT team_id, name FROM teams", con)
    nick = {norm_name(n).split()[-1]: tid for tid, n in zip(teams.team_id, teams.name)}
    sched = pd.read_sql("SELECT game_id, game_date, start_time_utc, home_team_id, away_team_id FROM schedule", con)
    markets, quotes, book_errors = [], [], 0
    for ev in events:
        m = re.match(r"nba-([a-z]+)-([a-z]+)-(\d{4}-\d{2}-\d{2})$", ev.get("slug", ""))
        if not m:
            continue
        d = date.fromisoformat(m.group(3))
        near = {(d + timedelta(days=k)).isoformat() for k in (-1, 0, 1)}
        for mk in ev.get("markets", []):
            outcomes = _json_list(mk.get("outcomes"))
            keys = [norm_name(o).split()[-1] if o else "" for o in outcomes]
            if len(keys) != 2 or not all(k in nick for k in keys):
                continue  # spread / total / prop markets
            ids = [nick[k] for k in keys]
            g = sched[sched.game_date.isin(near) & sched.home_team_id.isin(ids) & sched.away_team_id.isin(ids)]
            if g.empty:
                continue
            g = g.iloc[0]
            tokens = _json_list(mk.get("clobTokenIds"))
            prices = [float(x) for x in _json_list(mk.get("outcomePrices"))]
            home_i = 0 if ids[0] == g.home_team_id else 1
            side_idx = {"home": home_i, "away": 1 - home_i}
            markets.append({"market_id": str(mk.get("id")), "condition_id": mk.get("conditionId"),
                            "game_id": g.game_id, "event_slug": ev["slug"], "question": mk.get("question", ""),
                            "home_token_id": tokens[home_i] if len(tokens) == 2 else None,
                            "away_token_id": tokens[1 - home_i] if len(tokens) == 2 else None,
                            "last_seen": captured_at})
            pre = int(captured_at < str(g.start_time_utc))
            for side, i in side_idx.items():
                q = {"game_id": g.game_id, "market_id": str(mk.get("id")), "side": side,
                     "token_id": tokens[i] if len(tokens) == 2 else None, "captured_at": captured_at,
                     "volume": float(mk.get("volume") or 0), "liquidity": float(mk.get("liquidity") or 0),
                     "last_trade": None, "pre_game": pre}
                summary = None
                if q["token_id"]:
                    try:
                        summary = _book_summary(sources.polymarket_book(q["token_id"]))
                    except RuntimeError:
                        book_errors += 1
                if summary and summary["best_ask"] is not None:
                    q.update(summary, quote_source="clob")
                else:  # indicative only: Gamma's displayed price
                    bb, ba = mk.get("bestBid"), mk.get("bestAsk")
                    if bb is not None and ba is not None and i == 1:
                        bb, ba = 1 - float(ba), 1 - float(bb)
                    q.update({"best_bid": None if bb is None else float(bb), "best_ask": None if ba is None else float(ba),
                              "ask_size": None, "ask_depth_usd": None, "bid_depth_usd": None,
                              "mid": prices[i] if len(prices) == 2 else None, "quote_source": "gamma"})
                if i == 0 and mk.get("lastTradePrice") is not None:
                    q["last_trade"] = float(mk["lastTradePrice"])
                quotes.append(q)
            break
    with con:
        upsert(con, "polymarket_markets", pd.DataFrame(markets), ["market_id"])
        w, _ = upsert(con, "polymarket_quotes", pd.DataFrame(quotes), ["game_id", "side", "captured_at"])
    run.log("polymarket", f"{len(markets)} games, {book_errors} order-book errors",
            "ok" if not book_errors else "error", fetched=len(events), written=w)
    return len(markets)

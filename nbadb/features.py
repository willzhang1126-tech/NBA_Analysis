"""Point-in-time features (review items 1 and 7 of the risk register).

Every function takes a `cutoff` (ISO UTC time) and only uses:
  - games that started before the cutoff,
  - injury observations captured at or before the cutoff,
  - market quotes captured at or before the cutoff.
Use these, not the browsing views, for training and backtesting.

Known limit: stat corrections are applied in place (old values are kept in
`revisions`), so a backtest sees the corrected box score. Corrections are small
and usually land within a day of the game.
"""
import json

import pandas as pd

FEATURE_VERSION = "pit-v1"
STATUS_UNAVAILABLE = {"Out": 0.98, "Doubtful": 0.8, "Questionable": 0.45, "Day-To-Day": 0.4, "Probable": 0.1}

_START = "COALESCE(g.start_time_utc, g.game_date || 'T23:59:59Z')"  # conservative if tip-off unknown


def season_of_cutoff(cutoff):
    y, m = int(cutoff[:4]), int(cutoff[5:7])
    y = y if m >= 9 else y - 1
    return f"{y}-{str(y + 1)[-2:]}"


def team_games_before(con, cutoff, season=None):
    """One row per team per game played before the cutoff, with shooting split."""
    season = season or season_of_cutoff(cutoff)
    return pd.read_sql(f"""
        SELECT g.game_id, {_START} AS start, g.game_date, t.team_id, t.opponent_team_id, t.is_home,
               (t.wl = 'W') AS win, s.final_score AS pts, o.final_score AS opp_pts,
               s.fg2m, s.fg2a, s.fg3m, s.fg3a, s.ftm, s.fta,
               o.fg2m AS opp_fg2m, o.fg2a AS opp_fg2a, o.fg3m AS opp_fg3m, o.fg3a AS opp_fg3a,
               o.ftm AS opp_ftm, o.fta AS opp_fta, s.pts_2pt, s.pts_3pt, s.pts_ft
        FROM games g
        JOIN team_game_stats t ON t.game_id = g.game_id
        JOIN team_game_shooting s ON s.game_id = t.game_id AND s.team_id = t.team_id
        JOIN team_game_shooting o ON o.game_id = t.game_id AND o.team_id = t.opponent_team_id
        WHERE g.season = ? AND {_START} < ?
        ORDER BY start""", con, params=[season, cutoff])


def team_form_asof(con, cutoff, season=None):
    df = team_games_before(con, cutoff, season)
    if df.empty:
        return pd.DataFrame()
    out = []
    for tid, d in df.groupby("team_id"):
        d = d.sort_values("start")
        last10 = d.tail(10)
        last_date = pd.Timestamp(d.game_date.iloc[-1])
        cut_date = pd.Timestamp(cutoff[:10])
        out.append({
            "team_id": tid, "gp": len(d), "win_pct": d.win.mean(),
            "pts_pg": d.pts.mean(), "opp_pts_pg": d.opp_pts.mean(), "margin_pg": (d.pts - d.opp_pts).mean(),
            "last10_win_pct": last10.win.mean(), "last10_margin": (last10.pts - last10.opp_pts).mean(),
            "home_win_pct": d[d.is_home == 1].win.mean() if (d.is_home == 1).any() else None,
            "away_win_pct": d[d.is_home == 0].win.mean() if (d.is_home == 0).any() else None,
            "fg2_pct": d.fg2m.sum() / max(d.fg2a.sum(), 1), "fg3_pct": d.fg3m.sum() / max(d.fg3a.sum(), 1),
            "ft_pct": d.ftm.sum() / max(d.fta.sum(), 1),
            "opp_fg2_pct": d.opp_fg2m.sum() / max(d.opp_fg2a.sum(), 1),
            "opp_fg3_pct": d.opp_fg3m.sum() / max(d.opp_fg3a.sum(), 1),
            "share_3pt": d.pts_3pt.sum() / max(d.pts.sum(), 1),
            "rest_days": int((cut_date - last_date).days) - 1,
            "back_to_back": int((cut_date - last_date).days == 1),
        })
    return pd.DataFrame(out).set_index("team_id")


def player_form_asof(con, cutoff, n=10):
    """Each player's last n games before the cutoff (any season)."""
    df = pd.read_sql(f"""
        SELECT p.player_id, p.team_id, {_START} AS start, p.min, p.pts, p.reb, p.ast, p.stl, p.blk, p.tov, p.plus_minus
        FROM player_game_stats p JOIN games g USING (game_id)
        WHERE {_START} < ?""", con, params=[cutoff])
    if df.empty:
        return pd.DataFrame()
    df = df.sort_values("start").groupby("player_id").tail(n)
    agg = df.groupby("player_id").agg(
        gp=("pts", "count"), last_team=("team_id", "last"), last_game=("start", "max"),
        min_pg=("min", "mean"), pts_pg=("pts", "mean"), reb_pg=("reb", "mean"), ast_pg=("ast", "mean"),
        stl_pg=("stl", "mean"), blk_pg=("blk", "mean"), tov_pg=("tov", "mean"), pm_pg=("plus_minus", "mean"))
    return agg


def injury_status_asof(con, cutoff, source="espn"):
    """Latest injury status per player at the cutoff (Cleared players dropped).
    Also returns how old the newest injury capture is, so stale reports can be spotted."""
    df = pd.read_sql("""
        SELECT o.* FROM injury_observations o
        WHERE o.source = ? AND o.captured_at <= ? AND o.captured_at = (
            SELECT MAX(captured_at) FROM injury_observations o2
            WHERE o2.source = o.source AND o2.team_id = o.team_id AND o2.player_name = o.player_name
              AND o2.captured_at <= ?)""", con, params=[source, cutoff, cutoff])
    last = con.execute("SELECT MAX(captured_at) FROM injury_captures WHERE source = ? AND status = 'ok' "
                       "AND captured_at <= ?", (source, cutoff)).fetchone()[0]
    return df[df.status != "Cleared"], last


def game_features(con, game_id, cutoff=None):
    """Feature dict for one game, using only information before `cutoff`
    (default: the game's tip-off)."""
    g = con.execute("SELECT season, start_time_utc, home_team_id, away_team_id FROM schedule WHERE game_id = ?",
                    (game_id,)).fetchone()
    if g is None:
        raise ValueError(f"{game_id} not in schedule")
    season, start, home, away = g
    cutoff = cutoff or start
    form = team_form_asof(con, cutoff, season)
    players = player_form_asof(con, cutoff)
    inj, last_capture = injury_status_asof(con, cutoff)
    from .injury_score import compute as injury_compute
    prof = injury_compute(con, cutoff=cutoff, write=False)

    f = {"game_id": game_id, "feature_version": FEATURE_VERSION, "cutoff": cutoff,
         "injury_report_as_of": last_capture}
    for side, tid in (("home", home), ("away", away)):
        row = form.loc[tid].to_dict() if tid in form.index else {}
        for k, v in row.items():
            f[f"{side}_{k}"] = None if pd.isna(v) else float(v)
        # expected minutes and points lost to injuries
        ti = inj[inj.team_id == tid]
        lost_min = lost_pts = 0.0
        for r in ti.itertuples(index=False):
            p_out = STATUS_UNAVAILABLE.get(r.status, 0.0)
            if r.player_id is not None and not pd.isna(r.player_id) and int(r.player_id) in players.index:
                pr = players.loc[int(r.player_id)]
                lost_min += p_out * pr.min_pg
                lost_pts += p_out * pr.pts_pg
        f[f"{side}_expected_min_lost"] = round(lost_min, 1)
        f[f"{side}_expected_pts_lost"] = round(lost_pts, 1)
        f[f"{side}_players_out"] = int((ti.status == "Out").sum())
        roster = players[players.last_team == tid].index
        sub = prof[prof.player_id.isin(roster)] if not prof.empty else prof
        f[f"{side}_avg_injury_prone"] = None if sub.empty else float(sub.injury_prone.mean())
    for k in ("win_pct", "margin_pg", "last10_margin", "fg3_pct", "rest_days", "expected_min_lost"):
        h, a = f.get(f"home_{k}"), f.get(f"away_{k}")
        f[f"diff_{k}"] = None if h is None or a is None else h - a
    return f


def features_json(f):
    return json.dumps(f, sort_keys=True, default=str)

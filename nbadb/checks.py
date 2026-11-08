"""Box-score checks (review item 6) and team shooting source choice (item 7).

Per team per game:
  arithmetic         error  each player line: 2*2PM + 3*3PM + FTM = PTS, and the same for the team sum
  player_vs_score    error  sum of player points = the team's final score
  player_vs_team_box error  summed player FGM/FGA/3PM/3PA/FTM/FTA = official team box score
  completeness       error  both teams present, >=5 players, player minutes add up to team minutes
  reb_tov_diff       info   rebounds/turnovers can differ legitimately (team rebounds/turnovers)

Team shooting uses the player-built totals when every error check passes; otherwise
it falls back to the official team box score and is marked unverified.
"""
import pandas as pd

from .db import now_utc

SHOT_COLS = ["fgm", "fga", "fg3m", "fg3a", "ftm", "fta"]


def _minutes(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        if isinstance(v, str) and ":" in v:
            m, s = v.split(":")[:2]
            return int(m) + int(s) / 60
        return 0.0


def run_checks(con, run, game_ids):
    game_ids = list(game_ids)
    if not game_ids:
        return 0
    ph = ",".join("?" * len(game_ids))
    team = pd.read_sql(f"SELECT * FROM team_game_stats WHERE game_id IN ({ph})", con, params=game_ids)
    pl = pd.read_sql(f"SELECT * FROM player_game_stats WHERE game_id IN ({ph})", con, params=game_ids)
    games = pd.read_sql(f"SELECT game_id, home_team_id, away_team_id, home_pts, away_pts FROM games "
                        f"WHERE game_id IN ({ph})", con, params=game_ids).set_index("game_id")
    pl["min_f"] = pl["min"].map(_minutes)
    pl["line_pts"] = 2 * (pl.fgm - pl.fg3m) + 3 * pl.fg3m + pl.ftm
    pl["bad_line"] = (pl.line_pts != pl.pts).astype(int)
    sums = pl.groupby(["game_id", "team_id"]).agg(
        players=("player_id", "count"), players_with_min=("min_f", lambda m: int((m > 0).sum())),
        min_sum=("min_f", "sum"), bad_lines=("bad_line", "sum"), pts=("pts", "sum"),
        reb=("reb", "sum"), tov=("tov", "sum"), **{c: (c, "sum") for c in SHOT_COLS})

    ts = now_utc()
    check_rows, shoot_rows, failures = [], [], 0
    for gid in game_ids:
        if gid not in games.index:
            continue
        gm = games.loc[gid]
        tteams = team[team.game_id == gid]
        both = len(tteams) == 2 and all((gid, tid) in sums.index for tid in tteams.team_id)
        check_rows.append((gid, 0, "completeness", "error", int(both),
                           "both teams have box scores and player lines" if both else
                           f"team rows: {len(tteams)}, teams with player lines: "
                           f"{sum((gid, t) in sums.index for t in tteams.team_id)}", ts))
        for t in tteams.itertuples(index=False):
            final = gm.home_pts if t.team_id == gm.home_team_id else gm.away_pts
            res = {}
            if (gid, t.team_id) in sums.index:
                s = sums.loc[(gid, t.team_id)]
                shot_pts = 2 * (s.fgm - s.fg3m) + 3 * s.fg3m + s.ftm
                res["arithmetic"] = (shot_pts == s.pts and s.bad_lines == 0,
                                     f"shot points {shot_pts}, player points {s.pts}, bad player lines {s.bad_lines}")
                res["player_vs_score"] = (s.pts == final, f"players {s.pts}, final score {final}")
                diffs = [f"{c} players {s[c]} vs team {getattr(t, c)}" for c in SHOT_COLS if s[c] != getattr(t, c)]
                res["player_vs_team_box"] = (not diffs, "; ".join(diffs) or "all shot counts match")
                team_min = _minutes(t.min) if t.min else 240
                comp = s.players_with_min >= 5 and abs(s.min_sum - team_min) <= 6  # player minutes are rounded
                res["completeness"] = (comp, f"{s.players_with_min} players with minutes, "
                                             f"{s.min_sum:.0f} of {team_min:.0f} minutes")
                info = f"reb players {s.reb} vs team {t.reb}; tov players {s.tov} vs team {t.tov}"
            else:
                s = None
                for name in ("arithmetic", "player_vs_score", "player_vs_team_box", "completeness"):
                    res[name] = (False, "no player lines for this team")
                info = "no player lines"
            for name, (ok, detail) in res.items():
                check_rows.append((gid, int(t.team_id), name, "error", int(bool(ok)), detail, ts))
            check_rows.append((gid, int(t.team_id), "reb_tov_diff", "info", None, info, ts))
            verified = all(ok for ok, _ in res.values()) and both
            failures += not verified
            src = s if verified else t
            fg2m, fg2a = int(src.fgm - src.fg3m), int(src.fga - src.fg3a)
            shoot_rows.append((gid, int(t.team_id), "players" if verified else "official_fallback",
                               int(s.players) if s is not None else 0,
                               fg2m, fg2a, int(src.fg3m), int(src.fg3a), int(src.ftm), int(src.fta),
                               2 * fg2m, 3 * int(src.fg3m), int(src.ftm),
                               int(s.pts) if s is not None else None, int(final), int(verified), ts))
    with con:
        con.executemany("INSERT OR REPLACE INTO game_checks VALUES (?,?,?,?,?,?,?)", check_rows)
        con.executemany("INSERT OR REPLACE INTO team_game_shooting VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        shoot_rows)
    run.log("checks", f"{len(game_ids)} games", "ok", fetched=len(shoot_rows), written=len(check_rows),
            changed=failures)
    for gid, tid, name, sev, ok, detail, _ in check_rows:
        if sev == "error" and not ok:
            print(f"    CHECK FAILED {gid} team {tid} {name}: {detail}")
    return failures

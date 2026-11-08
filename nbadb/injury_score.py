"""Injury-prone score, method v2 (review item 5).

For each player, over the last 3 seasons before the cutoff, using only games
whose full roster (who played, who sat and why) has been loaded:
  absences_injury   = DNP or inactive with an injury/illness reason
  absences_unknown  = inactive with no reason given (counted at `injury_unknown_weight`, default 0.5)
  absences_other    = rest, coach's decision, suspension, personal, G League assignment
Seasons are weighted 50/30/20 (most recent first), then the rate is shrunk toward
the league average with 20 games of prior weight, so a player with few games isn't
scored as extreme. A 90% interval is reported. injury_prone is the shrunk rate
min-max scaled to 0-1 across players with at least 20 possible games (top 5% capped).
"""
import math

import numpy as np
import pandas as pd

from .db import now_utc, upsert
from .features import STATUS_UNAVAILABLE, injury_status_asof

METHOD_VERSION = "v2"
SEASON_WEIGHTS = [0.5, 0.3, 0.2]
PRIOR_GAMES = 20
MIN_GAMES = 20


def compute(con, cutoff=None, write=True):
    cutoff = cutoff or now_utc()
    unknown_w = float(con.execute("SELECT value FROM settings WHERE key = 'injury_unknown_weight'").fetchone()[0])
    df = pd.read_sql("""
        SELECT s.player_id, g.season, s.status, s.reason
        FROM player_game_status s JOIN games g USING (game_id)
        WHERE g.season_type = 'Regular Season'
          AND COALESCE(g.start_time_utc, g.game_date || 'T23:59:59Z') < ?
          AND s.game_id IN (SELECT DISTINCT game_id FROM player_game_status WHERE source = 'boxscore')
        """, con, params=[cutoff])
    if df.empty:
        if write:
            print("    injury score: no games with full rosters loaded yet")
        return pd.DataFrame(columns=["player_id", "injury_prone"])
    seasons = sorted(df.season.unique())[-len(SEASON_WEIGHTS):]
    weights = dict(zip(reversed(seasons), SEASON_WEIGHTS))
    df = df[df.season.isin(seasons)].copy()
    df["w"] = df.season.map(weights)
    absent = df.status != "played"
    df["inj"] = (absent & (df.reason == "injury")).astype(float)
    df["unk"] = (absent & (df.reason == "unknown")).astype(float)
    df["oth"] = (absent & ~df.reason.isin(["injury", "unknown"])).astype(float)
    df["events"] = df.inj + unknown_w * df.unk

    g = df.groupby("player_id")
    out = pd.DataFrame({
        "seasons_used": g.season.nunique(),
        "games_possible": g.size(),
        "games_played": g.apply(lambda d: int((d.status == "played").sum()), include_groups=False),
        "absences_injury": g.inj.sum().astype(int),
        "absences_unknown": g.unk.sum().astype(int),
        "absences_other": g.oth.sum().astype(int),
        "w_events": g.apply(lambda d: (d.w * d.events).sum(), include_groups=False),
        "w_games": g.w.sum(),
    })
    out["absences_total"] = out.games_possible - out.games_played
    out["absence_rate"] = (out.absences_total / out.games_possible).round(4)
    # season-weighted rate, expressed back on the real number of games
    n = out.games_possible.astype(float)
    e = out.w_events / out.w_games * n
    p0 = e.sum() / n.sum()
    a, b = e + p0 * PRIOR_GAMES, (n - e) + (1 - p0) * PRIOR_GAMES
    rate = a / (a + b)
    sd = np.sqrt(a * b / ((a + b) ** 2 * (a + b + 1)))
    out["injury_rate"] = rate.round(4)
    out["injury_rate_low"] = (rate - 1.645 * sd).clip(lower=0).round(4)
    out["injury_rate_high"] = (rate + 1.645 * sd).clip(upper=1).round(4)

    inj, _ = injury_status_asof(con, cutoff)
    status = inj.dropna(subset=["player_id"]).astype({"player_id": int}).set_index("player_id")["status"]
    out["current_status"] = out.index.map(status)
    out["next_game_unavailable_prob"] = [
        STATUS_UNAVAILABLE.get(s, r) if isinstance(s, str) else r
        for s, r in zip(out.current_status, out.injury_rate)]
    out["next_game_unavailable_prob"] = out.next_game_unavailable_prob.round(3)

    scored = out.games_possible >= MIN_GAMES
    if scored.any():
        hi = out.loc[scored, "injury_rate"].quantile(0.95)
        lo = out.loc[scored, "injury_rate"].min()
        out["injury_prone"] = np.where(scored, ((out.injury_rate.clip(upper=hi) - lo) / max(hi - lo, 1e-9)).round(3),
                                       np.nan)
    else:
        out["injury_prone"] = np.nan
    out["as_of"] = cutoff
    out["method_version"] = METHOD_VERSION
    out = out.reset_index()
    cols = ["player_id", "as_of", "method_version", "seasons_used", "games_possible", "games_played",
            "absences_total", "absences_injury", "absences_unknown", "absences_other", "absence_rate",
            "injury_rate", "injury_rate_low", "injury_rate_high", "current_status",
            "next_game_unavailable_prob", "injury_prone"]
    if write:
        with con:
            con.execute("DELETE FROM player_injury_profile")
            upsert(con, "player_injury_profile", out[cols], ["player_id"])
    return out[cols]

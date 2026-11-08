"""Immutable, versioned predictions (review item 2).

    from nbadb import predictions, features
    f = features.game_features(con, game_id)                  # cutoff = tip-off by default
    predictions.record(con, game_id, 0.63, "elo-v1", f)

Rows can never be updated or deleted (enforced by database triggers). To change a
forecast, record a new one; model_vs_market uses the latest pre-game prediction.
"""
from .db import now_utc
from .features import features_json


def record(con, game_id, home_win_prob, model_version, features, predicted_at=None):
    if not 0 <= home_win_prob <= 1:
        raise ValueError("home_win_prob must be between 0 and 1")
    predicted_at = predicted_at or now_utc()
    cutoff = features["cutoff"]
    if cutoff > predicted_at:
        raise ValueError(f"feature cutoff {cutoff} is after the prediction time {predicted_at}")
    start = con.execute("SELECT start_time_utc FROM schedule WHERE game_id = ?", (game_id,)).fetchone()
    pre_game = int(start is not None and start[0] is not None and predicted_at < start[0])
    with con:
        cur = con.execute("""INSERT INTO model_predictions (game_id, model_version, feature_version, feature_cutoff,
                               predicted_at, home_win_prob, features_json, pre_game) VALUES (?,?,?,?,?,?,?,?)""",
                          (game_id, model_version, features["feature_version"], cutoff, predicted_at,
                           float(home_win_prob), features_json(features), pre_game))
    return cur.lastrowid

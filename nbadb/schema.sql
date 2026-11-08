-- NBA stats database schema (method/schema version 2).
-- Stored tables hold raw observations. Views are "current" summaries for browsing.
-- Anything used to train or backtest a model must go through nbadb/features.py,
-- which only uses information available before a cutoff time.

------------------------------------------------------------------ reference
CREATE TABLE IF NOT EXISTS teams (
    team_id      INTEGER PRIMARY KEY,
    abbreviation TEXT,
    name         TEXT
);
CREATE TABLE IF NOT EXISTS players (
    player_id INTEGER PRIMARY KEY,
    name      TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT,
    note  TEXT
);
INSERT OR IGNORE INTO settings VALUES
  ('taker_fee_per_share', '0', 'Polymarket fee per $1 share bought, used in trading EV. Check current fee schedule.'),
  ('injury_unknown_weight', '0.5', 'Weight of absences with unknown reason in the injury score (confirmed injury = 1).');

------------------------------------------------------------------ schedule & games
CREATE TABLE IF NOT EXISTS schedule (
    game_id        TEXT PRIMARY KEY,
    season         TEXT,
    season_type    TEXT,
    game_date      TEXT,     -- local (ET) date
    start_time_utc TEXT,     -- ISO, e.g. 2026-10-21T23:30:00Z
    home_team_id   INTEGER,
    away_team_id   INTEGER,
    status         TEXT,     -- Scheduled / Final / Postponed ...
    last_updated   TEXT
);
CREATE TABLE IF NOT EXISTS games (
    game_id        TEXT PRIMARY KEY,
    season         TEXT,
    season_type    TEXT,
    game_date      TEXT,
    start_time_utc TEXT,     -- from schedule; NULL until the schedule is loaded
    home_team_id   INTEGER,
    away_team_id   INTEGER,
    home_pts       INTEGER,
    away_pts       INTEGER,
    home_win       INTEGER,
    finalized_at   TEXT,     -- first time we stored the final score
    last_updated   TEXT      -- last time any value changed
);
CREATE INDEX IF NOT EXISTS ix_games_start ON games(start_time_utc);

-- Official team box score, one row per team per game.
CREATE TABLE IF NOT EXISTS team_game_stats (
    game_id TEXT, team_id INTEGER, is_home INTEGER, opponent_team_id INTEGER, wl TEXT, min INTEGER,
    fgm INTEGER, fga INTEGER, fg3m INTEGER, fg3a INTEGER, ftm INTEGER, fta INTEGER,
    oreb INTEGER, dreb INTEGER, reb INTEGER, ast INTEGER, stl INTEGER, blk INTEGER,
    tov INTEGER, pf INTEGER, pts INTEGER, plus_minus INTEGER,
    PRIMARY KEY (game_id, team_id)
);
-- Player box score lines (players who played).
CREATE TABLE IF NOT EXISTS player_game_stats (
    game_id TEXT, player_id INTEGER, team_id INTEGER, min REAL,
    fgm INTEGER, fga INTEGER, fg3m INTEGER, fg3a INTEGER, ftm INTEGER, fta INTEGER,
    oreb INTEGER, dreb INTEGER, reb INTEGER, ast INTEGER, stl INTEGER, blk INTEGER,
    tov INTEGER, pf INTEGER, pts INTEGER, plus_minus INTEGER,
    PRIMARY KEY (game_id, player_id)
);
CREATE INDEX IF NOT EXISTS ix_pgs_player ON player_game_stats(player_id);

-- Every rostered player for every game, with why they did or didn't play.
CREATE TABLE IF NOT EXISTS player_game_status (
    game_id     TEXT,
    player_id   INTEGER,
    team_id     INTEGER,
    status      TEXT,   -- played / dnp / inactive / not_with_team
    reason      TEXT,   -- injury / rest / coach / suspension / personal / assignment / unknown / none
    raw_comment TEXT,   -- the NBA's text, e.g. "DNP - Coach's Decision"
    source      TEXT,   -- boxscore / inactive_list / injury_report
    PRIMARY KEY (game_id, player_id)
);

------------------------------------------------------------------ checks (review item 6/7)
CREATE TABLE IF NOT EXISTS game_checks (
    game_id    TEXT,
    team_id    INTEGER,           -- 0 for game-level checks
    check_name TEXT,              -- arithmetic / player_vs_score / player_vs_team_box / completeness / reb_tov_diff
    severity   TEXT,              -- error (blocks use of player totals) / info
    passed     INTEGER,
    detail     TEXT,
    checked_at TEXT,
    PRIMARY KEY (game_id, team_id, check_name)
);
-- Team shooting per game. Built from player lines; falls back to the official team
-- box score when an error-level check fails for that team-game.
CREATE TABLE IF NOT EXISTS team_game_shooting (
    game_id TEXT, team_id INTEGER,
    source_used      TEXT,   -- players / official_fallback
    players_used     INTEGER,
    fg2m INTEGER, fg2a INTEGER, fg3m INTEGER, fg3a INTEGER, ftm INTEGER, fta INTEGER,
    pts_2pt INTEGER, pts_3pt INTEGER, pts_ft INTEGER,
    pts_from_players INTEGER,
    final_score      INTEGER,
    verified         INTEGER,  -- 1 if all error-level checks passed
    checked_at       TEXT,
    PRIMARY KEY (game_id, team_id)
);

------------------------------------------------------------------ injuries (review items 4/5)
-- Each time we read an injury report.
CREATE TABLE IF NOT EXISTS injury_captures (
    captured_at TEXT, source TEXT, players_listed INTEGER, status TEXT,
    PRIMARY KEY (captured_at, source)
);
-- Status changes only: a row is written when a player's status or note changes,
-- and a 'Cleared' row when they drop off the report.
CREATE TABLE IF NOT EXISTS injury_observations (
    captured_at       TEXT,
    source            TEXT,
    team_id           INTEGER,
    player_id         INTEGER,   -- NULL if the name couldn't be matched
    player_name       TEXT,
    status            TEXT,      -- Out / Doubtful / Questionable / Probable / Day-To-Day / Cleared
    injury            TEXT,
    detail            TEXT,
    expected_return   TEXT,
    source_updated_at TEXT,      -- when the source says it last changed, if given
    PRIMARY KEY (source, team_id, player_name, captured_at)
);
CREATE TABLE IF NOT EXISTS player_injury_profile (
    player_id                  INTEGER PRIMARY KEY,
    as_of                      TEXT,
    method_version             TEXT,
    seasons_used               INTEGER,
    games_possible             INTEGER,  -- games on the roster
    games_played               INTEGER,
    absences_total             INTEGER,
    absences_injury            INTEGER,  -- confirmed injury/illness
    absences_unknown           INTEGER,  -- inactive, reason not given
    absences_other             INTEGER,  -- rest, coach, suspension, personal, assignment
    absence_rate               REAL,     -- all absences / possible
    injury_rate                REAL,     -- season-weighted, shrunk toward league average
    injury_rate_low            REAL,     -- 90% interval
    injury_rate_high           REAL,
    current_status             TEXT,     -- latest injury report status, if any
    next_game_unavailable_prob REAL,     -- heuristic: current status + injury rate
    injury_prone               REAL      -- injury_rate min-max scaled 0-1 across players
);

------------------------------------------------------------------ Polymarket (review item 3)
CREATE TABLE IF NOT EXISTS polymarket_markets (
    market_id     TEXT PRIMARY KEY,
    condition_id  TEXT,
    game_id       TEXT,
    event_slug    TEXT,
    question      TEXT,
    home_token_id TEXT,
    away_token_id TEXT,
    last_seen     TEXT
);
-- One row per outcome token per capture.
CREATE TABLE IF NOT EXISTS polymarket_quotes (
    game_id        TEXT,
    market_id      TEXT,
    side           TEXT,     -- home / away
    token_id       TEXT,
    captured_at    TEXT,
    quote_source   TEXT,     -- clob (tradable order book) / gamma (indicative)
    best_bid       REAL,
    best_ask       REAL,     -- what you'd pay per $1 share
    ask_size       REAL,     -- shares available at best ask
    ask_depth_usd  REAL,     -- $ available within 2 cents of best ask
    bid_depth_usd  REAL,
    mid            REAL,
    last_trade     REAL,
    volume         REAL,
    liquidity      REAL,
    pre_game       INTEGER,
    PRIMARY KEY (game_id, side, captured_at)
);

------------------------------------------------------------------ predictions (review item 2)
CREATE TABLE IF NOT EXISTS model_predictions (
    prediction_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id         TEXT NOT NULL,
    model_version   TEXT NOT NULL,
    feature_version TEXT NOT NULL,
    feature_cutoff  TEXT NOT NULL,   -- features use only info before this time
    predicted_at    TEXT NOT NULL,
    home_win_prob   REAL NOT NULL,
    features_json   TEXT,            -- the exact inputs used
    pre_game        INTEGER          -- 1 if made before tip-off
);
CREATE TRIGGER IF NOT EXISTS predictions_no_update BEFORE UPDATE ON model_predictions
BEGIN SELECT RAISE(ABORT, 'model_predictions is append-only'); END;
CREATE TRIGGER IF NOT EXISTS predictions_no_delete BEFORE DELETE ON model_predictions
BEGIN SELECT RAISE(ABORT, 'model_predictions is append-only'); END;

------------------------------------------------------------------ history & logging (review item 8)
CREATE TABLE IF NOT EXISTS revisions (
    run_id     INTEGER,
    changed_at TEXT,
    table_name TEXT,
    row_key    TEXT,
    column     TEXT,
    old_value  TEXT,
    new_value  TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    command     TEXT,
    started_at  TEXT,
    finished_at TEXT,
    status      TEXT
);
CREATE TABLE IF NOT EXISTS source_runs (
    run_id       INTEGER,
    source       TEXT,     -- nba_gamelog / nba_schedule / nba_boxscore / espn_injuries / polymarket ...
    detail       TEXT,
    status       TEXT,     -- ok / error
    rows_fetched INTEGER,
    rows_written INTEGER,
    rows_changed INTEGER,
    error        TEXT,
    at           TEXT
);

------------------------------------------------------------------ browsing views (current, NOT point-in-time)
CREATE VIEW games_readable AS
SELECT g.game_date, g.start_time_utc, g.season, g.season_type, h.abbreviation AS home, a.abbreviation AS away,
       g.home_pts, g.away_pts, g.home_win, g.game_id
FROM games g JOIN teams h ON h.team_id = g.home_team_id JOIN teams a ON a.team_id = g.away_team_id;

CREATE VIEW team_game_scoring AS
SELECT g.game_id, g.game_date, g.season, g.season_type,
       tm.abbreviation AS team, op.abbreviation AS opponent, t.is_home, t.wl,
       s.final_score AS pts,
       s.fg2m, s.fg2a, ROUND(1.0 * s.fg2m / NULLIF(s.fg2a, 0), 3) AS fg2_pct,
       s.fg3m, s.fg3a, ROUND(1.0 * s.fg3m / NULLIF(s.fg3a, 0), 3) AS fg3_pct,
       s.ftm,  s.fta,  ROUND(1.0 * s.ftm  / NULLIF(s.fta, 0), 3)  AS ft_pct,
       s.pts_2pt, s.pts_3pt, s.pts_ft,
       ROUND(1.0 * s.pts_2pt / NULLIF(s.final_score, 0), 3) AS share_2pt,
       ROUND(1.0 * s.pts_3pt / NULLIF(s.final_score, 0), 3) AS share_3pt,
       ROUND(1.0 * s.pts_ft  / NULLIF(s.final_score, 0), 3) AS share_ft,
       o.final_score AS opp_pts,
       o.pts_2pt AS opp_pts_2pt, o.pts_3pt AS opp_pts_3pt, o.pts_ft AS opp_pts_ft,
       o.fg2m AS opp_fg2m, o.fg2a AS opp_fg2a, o.fg3m AS opp_fg3m, o.fg3a AS opp_fg3a,
       o.ftm AS opp_ftm, o.fta AS opp_fta,
       ROUND(1.0 * o.fg2m / NULLIF(o.fg2a, 0), 3) AS opp_fg2_pct,
       ROUND(1.0 * o.fg3m / NULLIF(o.fg3a, 0), 3) AS opp_fg3_pct,
       ROUND(1.0 * o.ftm  / NULLIF(o.fta, 0), 3)  AS opp_ft_pct,
       s.source_used, s.verified AND o.verified AS verified
FROM team_game_stats t
JOIN games g ON g.game_id = t.game_id
JOIN team_game_shooting s ON s.game_id = t.game_id AND s.team_id = t.team_id
JOIN team_game_shooting o ON o.game_id = t.game_id AND o.team_id = t.opponent_team_id
JOIN teams tm ON tm.team_id = t.team_id
JOIN teams op ON op.team_id = t.opponent_team_id;

CREATE VIEW team_season_stats AS
SELECT season, season_type, team, COUNT(*) AS gp,
       SUM(wl = 'W') AS w, SUM(wl = 'L') AS l, ROUND(AVG(wl = 'W'), 3) AS win_pct,
       ROUND(AVG(pts), 1) AS pts_pg, ROUND(AVG(opp_pts), 1) AS opp_pts_pg,
       ROUND(AVG(pts - opp_pts), 1) AS margin_pg,
       SUM(is_home AND wl = 'W') || '-' || SUM(is_home AND wl = 'L')         AS home_record,
       SUM(NOT is_home AND wl = 'W') || '-' || SUM(NOT is_home AND wl = 'L') AS away_record
FROM team_game_scoring GROUP BY season, season_type, team;

CREATE VIEW team_shooting_season AS
SELECT season, season_type, team, COUNT(*) AS gp,
       ROUND(AVG(pts), 1) AS pts_pg,
       ROUND(AVG(fg2m), 1) AS fg2m_pg, ROUND(AVG(fg2a), 1) AS fg2a_pg,
       ROUND(1.0 * SUM(fg2m) / NULLIF(SUM(fg2a), 0), 3) AS fg2_pct,
       ROUND(AVG(fg3m), 1) AS fg3m_pg, ROUND(AVG(fg3a), 1) AS fg3a_pg,
       ROUND(1.0 * SUM(fg3m) / NULLIF(SUM(fg3a), 0), 3) AS fg3_pct,
       ROUND(AVG(ftm), 1) AS ftm_pg, ROUND(AVG(fta), 1) AS fta_pg,
       ROUND(1.0 * SUM(ftm) / NULLIF(SUM(fta), 0), 3) AS ft_pct,
       ROUND(AVG(pts_2pt), 1) AS pts_2pt_pg, ROUND(AVG(pts_3pt), 1) AS pts_3pt_pg,
       ROUND(AVG(pts_ft), 1) AS pts_ft_pg,
       ROUND(1.0 * SUM(pts_2pt) / SUM(pts), 3) AS share_2pt,
       ROUND(1.0 * SUM(pts_3pt) / SUM(pts), 3) AS share_3pt,
       ROUND(1.0 * SUM(pts_ft) / SUM(pts), 3)  AS share_ft,
       ROUND(AVG(opp_pts), 1) AS opp_pts_pg,
       ROUND(AVG(opp_pts_2pt), 1) AS opp_pts_2pt_pg, ROUND(AVG(opp_pts_3pt), 1) AS opp_pts_3pt_pg,
       ROUND(AVG(opp_pts_ft), 1) AS opp_pts_ft_pg,
       ROUND(1.0 * SUM(opp_fg2m) / NULLIF(SUM(opp_fg2a), 0), 3) AS opp_fg2_pct,
       ROUND(1.0 * SUM(opp_fg3m) / NULLIF(SUM(opp_fg3a), 0), 3) AS opp_fg3_pct,
       ROUND(1.0 * SUM(opp_ftm) / NULLIF(SUM(opp_fta), 0), 3)   AS opp_ft_pct,
       SUM(NOT verified) AS games_unverified
FROM team_game_scoring GROUP BY season, season_type, team;

-- Directional: one row per team vs each opponent per season and season type.
CREATE VIEW matchups AS
SELECT season, season_type, team, opponent, COUNT(*) AS games,
       SUM(wl = 'W') AS wins, SUM(wl = 'L') AS losses,
       ROUND(AVG(pts), 1) AS pts_pg, ROUND(AVG(opp_pts), 1) AS opp_pts_pg,
       ROUND(AVG(pts - opp_pts), 1) AS avg_margin,
       ROUND(AVG(pts_2pt), 1) AS pts_2pt_pg, ROUND(AVG(pts_3pt), 1) AS pts_3pt_pg, ROUND(AVG(pts_ft), 1) AS pts_ft_pg,
       ROUND(AVG(opp_pts_2pt), 1) AS opp_pts_2pt_pg, ROUND(AVG(opp_pts_3pt), 1) AS opp_pts_3pt_pg,
       ROUND(AVG(opp_pts_ft), 1) AS opp_pts_ft_pg,
       ROUND(1.0 * SUM(fg3m) / NULLIF(SUM(fg3a), 0), 3) AS fg3_pct,
       ROUND(1.0 * SUM(opp_fg3m) / NULLIF(SUM(opp_fg3a), 0), 3) AS opp_fg3_pct,
       MAX(game_date) AS last_meeting
FROM team_game_scoring GROUP BY season, season_type, team, opponent;

-- Grain: player + season + season type, all teams combined.
CREATE VIEW player_season_stats AS
SELECT g.season, g.season_type, p.player_id, pl.name, COUNT(*) AS gp,
       (SELECT t.abbreviation FROM player_game_stats x JOIN games gx USING (game_id) JOIN teams t ON t.team_id = x.team_id
         WHERE x.player_id = p.player_id AND gx.season = g.season ORDER BY gx.game_date DESC LIMIT 1) AS current_team,
       ROUND(AVG(p.min), 1) AS min_pg, ROUND(AVG(p.pts), 1) AS pts_pg, ROUND(AVG(p.reb), 1) AS reb_pg,
       ROUND(AVG(p.ast), 1) AS ast_pg, ROUND(AVG(p.stl), 1) AS stl_pg, ROUND(AVG(p.blk), 1) AS blk_pg,
       ROUND(AVG(p.tov), 1) AS tov_pg, ROUND(AVG(p.plus_minus), 1) AS plus_minus_pg,
       ROUND(1.0 * SUM(p.fgm) / NULLIF(SUM(p.fga), 0), 3) AS fg_pct,
       ROUND(1.0 * SUM(p.fg3m) / NULLIF(SUM(p.fg3a), 0), 3) AS fg3_pct,
       ROUND(1.0 * SUM(p.ftm) / NULLIF(SUM(p.fta), 0), 3) AS ft_pct,
       SUM(p.pts) AS pts, SUM(p.reb) AS reb, SUM(p.ast) AS ast, SUM(p.stl) AS stl, SUM(p.blk) AS blk, SUM(p.tov) AS tov
FROM player_game_stats p JOIN games g USING (game_id) JOIN players pl USING (player_id)
GROUP BY g.season, g.season_type, p.player_id;

-- Grain: player + team + season (one row per stint, for traded players).
CREATE VIEW player_team_season_stats AS
SELECT g.season, g.season_type, p.player_id, pl.name, t.abbreviation AS team, COUNT(*) AS gp,
       MIN(g.game_date) AS first_game, MAX(g.game_date) AS last_game,
       ROUND(AVG(p.min), 1) AS min_pg, ROUND(AVG(p.pts), 1) AS pts_pg, ROUND(AVG(p.reb), 1) AS reb_pg,
       ROUND(AVG(p.ast), 1) AS ast_pg, ROUND(AVG(p.stl), 1) AS stl_pg, ROUND(AVG(p.blk), 1) AS blk_pg,
       ROUND(AVG(p.tov), 1) AS tov_pg
FROM player_game_stats p JOIN games g USING (game_id) JOIN players pl USING (player_id)
JOIN teams t ON t.team_id = p.team_id
GROUP BY g.season, g.season_type, p.player_id, p.team_id;

-- Latest injury status per player (current; for history use features.injury_status_asof).
CREATE VIEW injury_report_current AS
SELECT t.abbreviation AS team, o.player_name, o.status, o.injury, o.detail, o.expected_return,
       o.captured_at, ip.injury_prone
FROM injury_observations o
JOIN teams t ON t.team_id = o.team_id
LEFT JOIN player_injury_profile ip ON ip.player_id = o.player_id
WHERE o.captured_at = (SELECT MAX(captured_at) FROM injury_observations o2
                       WHERE o2.source = o.source AND o2.team_id = o.team_id AND o2.player_name = o.player_name)
  AND o.status <> 'Cleared';

CREATE VIEW data_check_failures AS
SELECT g.game_date, c.game_id, t.abbreviation AS team, c.check_name, c.severity, c.detail, c.checked_at
FROM game_checks c JOIN games g USING (game_id) LEFT JOIN teams t ON t.team_id = c.team_id
WHERE c.passed = 0;

-- Model vs market.
--   forecast_gap: model minus the market's fair probability at close (normalized mids), for scoring forecasts.
--   ev_home / ev_away: model chance minus the ask you could actually buy at when the prediction was made,
--   minus fees; positive means a +EV purchase of a $1 share.
CREATE VIEW model_vs_market AS
WITH pred AS (
    SELECT p.* FROM model_predictions p
    WHERE p.pre_game = 1 AND p.prediction_id = (
        SELECT MAX(prediction_id) FROM model_predictions p2
        WHERE p2.game_id = p.game_id AND p2.model_version = p.model_version AND p2.pre_game = 1)),
fee AS (SELECT CAST(value AS REAL) AS f FROM settings WHERE key = 'taker_fee_per_share')
SELECT pred.prediction_id, pred.game_id, g.game_date, h.abbreviation AS home, a.abbreviation AS away,
       pred.model_version, pred.predicted_at,
       ROUND(pred.home_win_prob, 3) AS model_home_prob,
       ROUND(ch.mid / NULLIF(ch.mid + ca.mid, 0), 3) AS market_close_prob,
       ROUND(pred.home_win_prob - ch.mid / NULLIF(ch.mid + ca.mid, 0), 3) AS forecast_gap,
       qh.best_ask AS home_ask, qh.ask_size AS home_ask_size,
       qa.best_ask AS away_ask, qa.ask_size AS away_ask_size,
       qh.quote_source,
       ROUND(pred.home_win_prob - qh.best_ask - fee.f, 3)       AS ev_home,
       ROUND((1 - pred.home_win_prob) - qa.best_ask - fee.f, 3) AS ev_away,
       CASE WHEN qh.quote_source <> 'clob' THEN NULL
            WHEN pred.home_win_prob - qh.best_ask - fee.f > 0 THEN 'BUY HOME'
            WHEN (1 - pred.home_win_prob) - qa.best_ask - fee.f > 0 THEN 'BUY AWAY'
            ELSE 'NO BET' END AS action,
       gm.home_win,
       ROUND((pred.home_win_prob - gm.home_win) * (pred.home_win_prob - gm.home_win), 4) AS model_brier,
       ROUND((ch.mid / NULLIF(ch.mid + ca.mid, 0) - gm.home_win) * (ch.mid / NULLIF(ch.mid + ca.mid, 0) - gm.home_win), 4) AS market_brier
FROM pred
CROSS JOIN fee
JOIN schedule g ON g.game_id = pred.game_id
JOIN teams h ON h.team_id = g.home_team_id
JOIN teams a ON a.team_id = g.away_team_id
-- quote available when the prediction was made
LEFT JOIN polymarket_quotes qh ON qh.game_id = pred.game_id AND qh.side = 'home' AND qh.captured_at =
     (SELECT MAX(captured_at) FROM polymarket_quotes q WHERE q.game_id = pred.game_id AND q.side = 'home' AND q.captured_at <= pred.predicted_at)
LEFT JOIN polymarket_quotes qa ON qa.game_id = pred.game_id AND qa.side = 'away' AND qa.captured_at = qh.captured_at
-- closing quote (last before tip-off)
LEFT JOIN polymarket_quotes ch ON ch.game_id = pred.game_id AND ch.side = 'home' AND ch.captured_at =
     (SELECT MAX(captured_at) FROM polymarket_quotes q WHERE q.game_id = pred.game_id AND q.side = 'home' AND q.pre_game = 1)
LEFT JOIN polymarket_quotes ca ON ca.game_id = pred.game_id AND ca.side = 'away' AND ca.captured_at = ch.captured_at
LEFT JOIN games gm ON gm.game_id = pred.game_id;

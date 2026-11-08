# NBA stats database: data format (v2)

Example numbers below are placeholders.

One SQLite file (`nba.db`). Raw observations go in tables. Browsing summaries are views. Anything used to train or backtest a model goes through `nbadb/features.py`, which only uses information that existed before a chosen cutoff time.

## Stored tables

### Games and box scores
| table | one row per | key columns |
|---|---|---|
| `schedule` | game, including upcoming | game_id, season, season_type, game_date, **start_time_utc**, home/away team, status, last_updated |
| `games` | finished game | game_id, season, season_type, game_date, start_time_utc, home/away team, home_pts, away_pts, home_win, **finalized_at** (first stored), **last_updated** (last change) |
| `team_game_stats` | team per game (official team box score) | game_id + team_id, is_home, opponent, W/L, min, FGM/FGA, 3PM/3PA, FTM/FTA, OREB/DREB/REB, AST, STL, BLK, TOV, PF, PTS, +/- |
| `player_game_stats` | player per game played | game_id + player_id, team_id, min, same stat columns as above |
| `player_game_status` | **every rostered player** per game | game_id + player_id, status (`played` / `dnp` / `inactive` / `not_with_team`), reason (`injury` / `rest` / `coach` / `suspension` / `personal` / `assignment` / `unknown`), the NBA's raw comment, source |

Season, date and other game details are no longer copied onto each team and player row; they come from `games` (review 3).

### Checks and team shooting (review 2.4, your #7)
`game_checks`, one row per team per game per check:

| check | level | passes when |
|---|---|---|
| arithmetic | error | every player line: 2×2PM + 3×3PM + FTM = PTS, and the same for the team sum |
| player_vs_score | error | sum of player points = the team's final score |
| player_vs_team_box | error | summed player FGM, FGA, 3PM, 3PA, FTM, FTA = the official team box score |
| completeness | error | both teams present, at least 5 players with minutes, player minutes ≈ team minutes |
| reb_tov_diff | info | only reported: rebounds and turnovers can legitimately differ (team rebounds/turnovers) |

`team_game_shooting`, one row per team per game: 2PM/2PA, 3PM/3PA, FTM/FTA, points from 2s/3s/FTs, player points, final score, `verified`, and **`source_used`**:
- `players`: built from player lines (all error checks passed). This is the normal case.
- `official_fallback`: a check failed, so the official team box score is used and the game is flagged.

### Injuries (review 2.2, 2.5)
| table | one row per | columns |
|---|---|---|
| `injury_captures` | each time we read the report | captured_at, source, players listed, ok/error |
| `injury_observations` | **status change** per player | captured_at, source, team, player, status (Out / Doubtful / Questionable / Probable / Day-To-Day / **Cleared**), injury, detail, expected return, source's own update time |
| `player_injury_profile` | player (rebuilt each run) | see below |

A row is written only when something changes, so morning Questionable → evening Out is two rows with times. A player who drops off the report gets a `Cleared` row. An empty league-wide report is treated as a feed glitch and ignored.

**Injury-prone score, method v2**, built from the last 3 seasons of games where the full roster has been loaded:

| column | meaning |
|---|---|
| games_possible / games_played | games on the roster / games played |
| absences_injury | DNP or inactive for injury/illness |
| absences_unknown | inactive with no reason given (counts 0.5 toward the injury score; this weight can be changed in `settings`) |
| absences_other | rest, coach's decision, suspension, personal, G League |
| absence_rate | all absences ÷ possible (kept separate from injury) |
| injury_rate | seasons weighted 50/30/20, then pulled toward the league average by 20 games' worth of data, so small samples aren't extreme |
| injury_rate_low / _high | 90% range |
| current_status | latest injury report status |
| next_game_unavailable_prob | rule of thumb: Out 0.98, Doubtful 0.8, Questionable 0.45, Day-To-Day 0.4, Probable 0.1, otherwise injury_rate |
| injury_prone | injury_rate scaled 0–1 across players with 20+ possible games (top 5% capped at 1) |
| method_version, as_of | for reproducibility |

### Polymarket (review 2.3)
| table | one row per | columns |
|---|---|---|
| `polymarket_markets` | moneyline market | market_id, condition_id, game_id, event slug, question, **home_token_id, away_token_id** |
| `polymarket_quotes` | outcome per capture | game, side (home/away), token_id, captured_at, **quote_source** (`clob` = tradable order book, `gamma` = indicative only), **best_bid, best_ask, ask_size**, ask/bid depth in $ within 2¢, mid, last trade, volume, liquidity, pre_game |

### Predictions (review 2.1, 3)
`model_predictions`: prediction_id, game_id, model_version, **feature_version, feature_cutoff**, predicted_at, home_win_prob, **features_json** (the exact inputs), pre_game. The database refuses edits and deletes, and a prediction whose feature cutoff is after its own timestamp is rejected.

### History and logging (review 2.6)
| table | purpose |
|---|---|
| `revisions` | every value changed in games / team / player / status rows: old value, new value, when, which run |
| `runs` | each command run with start, finish, status |
| `source_runs` | per source per run: rows fetched / written / changed, ok or error, error text |
| `settings` | `taker_fee_per_share` (default 0) and `injury_unknown_weight` (default 0.5) |

## Point-in-time features (review 2.1, 2.7)
`features.game_features(con, game_id, cutoff)` returns one row of inputs for a game, using **only** games that started before the cutoff, injury reports captured by then, and the injury score as of then. Default cutoff is tip-off. It includes, for home and away and the difference:
- record, win %, points for/against, margin, last-10 win % and margin, home/away win %
- 2P%, 3P%, FT% and what opponents shoot, 3-point share of points
- rest days and back-to-back flag
- players out, expected minutes and points lost to injuries (status × recent minutes)
- average injury-prone score of the roster

Known limit: stat corrections overwrite in place (old values stay in `revisions`), so backtests use corrected box scores. Corrections are small and usually come within a day.

## Browsing views (current numbers, not for backtesting)
| view | one row per | what |
|---|---|---|
| `team_game_scoring` | team, game | 2/3/FT made, attempted, %, points and share, same for opponent, source_used, verified |
| `team_shooting_season` | team, season, season type | shooting by type and allowed by type, games unverified |
| `team_season_stats` | team, season, season type | W-L, win %, points for/against, margin, home/away record |
| `matchups` | team vs opponent, season, season type | W-L, points each way, 2/3/FT split each way, 3P% each way, last meeting |
| `player_season_stats` | player, season, season type (all teams) | per-game averages, shooting %, totals, current team |
| `player_team_season_stats` | player, team, season (stints) | same, split by team for traded players |
| `injury_report_current` | listed player | latest status with injury-prone score |
| `model_vs_market` | game, model version | see below |
| `data_check_failures` | failed check | what failed and why |
| `games_readable` | game | with team abbreviations |

`model_vs_market` uses the latest pre-game prediction:

| column | meaning |
|---|---|
| model_home_prob | the model's home win chance |
| market_close_prob | last pre-game home mid ÷ (home mid + away mid), the market's fair chance |
| forecast_gap | model − market_close_prob, for **scoring forecasts** only |
| home_ask / away_ask, ask sizes | what you could buy at **when the prediction was made**, and how many shares |
| ev_home / ev_away | model chance − ask − fee per share (per $1 share) |
| action | BUY HOME / BUY AWAY / NO BET; left blank when only an indicative quote exists |
| home_win, model_brier, market_brier | result and accuracy |

The review's example (model 0.61, home ask 0.62) shows ev_home = −0.01, so the action is NO BET.

## Refresh schedule (review 2.6)
| when | what |
|---|---|
| daily | schedule; games for the last 3 days **plus** every game that should be final but isn't stored **plus** any game from the last 60 days with a failed check; rosters for new games; checks; injury report; injury scores; Polymarket; spreadsheet |
| Mondays | also re-pull the last 14 days |
| 1st of the month | also re-pull the whole season |
| game days, every 1–2 h | `injuries` |
| game days, every 30–60 min | `odds` |

Example cron (times ET):
```
0 10 * * *            cd ~/nba_stats_db && python nba_db.py daily
0 12-23/2 * * *       cd ~/nba_stats_db && python nba_db.py injuries
*/30 15-23 * * *      cd ~/nba_stats_db && python nba_db.py odds
```

## Spreadsheet (`nba_stats.xlsx`)
Team shooting · Standings · Matchups · Game scoring · Players · Injury report · Injury-prone · Model vs market · Data checks · Update log

## Load cost
Backfilling 6 seasons of games takes a few minutes. Rosters (who sat and why) take 2 calls per game, so loading them for the last 3 seasons (about 3,900 games) takes roughly 4 hours, once. After that, the daily run takes a few minutes.

## Sources (no API keys)
stats.nba.com (nba_api) · site.api.espn.com · gamma-api.polymarket.com · clob.polymarket.com

## Not built yet (review items 9 and 10, coming with the model)
Possessions/pace, offensive/defensive rating, opponent-strength adjustment, rolling 5/10/20-game form, expected rotation minutes, Elo rating; evaluation with Brier score, log loss, calibration and simulated returns.

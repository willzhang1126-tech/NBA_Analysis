# Response to "NBA Analytics Database: Logic Review"

I checked each finding against the current code (`nba_db.py`). Most of them hold. Two are already handled, and one conflicts with your earlier instruction, so that one is your call.

## 1. Verification of each finding

| # | Finding | Verdict | What the code does today |
|---|---|---|---|
| 2.1 | Views leak future games into past predictions | **Valid** | `team_season_stats`, `player_last10` etc. always use every stored game. Fine for looking at stats; wrong for backtesting a model. Nothing is affected yet because no model exists. |
| 2.2 | Injury-prone score counts every absence as injury | **Valid** (already noted as a caveat) | Missed games = rest, suspension, DNP, injury alike. Only blends in injury reports after 30 days. Raw counts are kept (games_possible, games_missed), but there is no methodology version and no uncertainty. |
| 2.3 | Normalized price ≠ purchase price for EV | **Partly valid** | `value_side` already compares the model to the **raw** price (0.62), not the normalized one, so the review's $0.01 example would correctly show no bet. But the `edge` column uses the normalized price, which looks like a betting edge and isn't. The Gamma API also gives last/mid prices, not the ask you'd actually pay, and no order-book depth or token IDs are stored. |
| 2.4 | Player sums can legitimately differ from team totals | **Partly valid** | For **points**, NBA player lines always add up to the team score, so a mismatch really does mean bad or incomplete data. For rebounds and turnovers, team totals include "team" rebounds and turnovers that belong to no player, but we don't check those. We also don't compare made shots and attempts with the official team box score. |
| 2.5 | One injury snapshot per day hides status changes | **Valid** | The key is (date, team, player), so a second run on the same day overwrites the morning status. |
| 2.6 | 3-day re-pull misses older corrections; overwrites lose history | **Valid** | Corrections older than 3 days are missed. Postponed games do get picked up, because the re-pull goes by date and the make-up game has a new date. Overwrites keep no previous values. |
| 2.7 | Last-10 needs a cutoff date | **Valid** | Same problem as 2.1. |

Schema recommendations (section 3):

| Item | Verdict |
|---|---|
| games/schedule: start UTC, status, last updated | Partly done: `schedule` has start UTC and status; `games` lacks last-updated. |
| team_game_stats duplicates game metadata | True but harmless. Keeping it makes queries simpler. **No change recommended.** |
| player_game_stats: played vs DNP vs inactive | **Valid.** The game log only lists players who played, so DNPs and inactive players are invisible. This also weakens the injury-prone score. |
| player_season_stats grain | It's player-season (all teams combined) plus current team. Can add a player-team-season view. |
| injury_snapshots keyed by timestamp | Valid (same as 2.5). |
| polymarket_odds: market/token IDs, bid/ask | Valid (same as 2.3). |
| model_predictions: prediction_id, feature cutoff, version | Valid. |
| team_game_shooting as a view | Disagree. It's a table so that each check result is recorded with the time it ran. **No change recommended.** |
| matchups directional + season filters | **Already done**: one row per team vs opponent, per season and season type. |

## 2. Proposed adjustments for you to approve

Reply with the numbers you approve (e.g. "approve 1–6, 8; skip 7").

| # | Priority | Adjustment | My recommendation |
|---|---|---|---|
| 1 | P0 | **Point-in-time features**: add `as_of` versions of team stats, recent form and injury scores that only use games that started before a given time. Keep the current views for browsing. | Approve |
| 2 | P0 | **Immutable predictions**: `model_predictions` gets prediction_id, feature_cutoff time, model_version, feature_version; rows are never overwritten. | Approve |
| 3 | P0 | **Market EV**: store best bid, best ask, token IDs, quote time and order-book depth from Polymarket's order book (CLOB API, needs `clob.polymarket.com` allowed). `model_vs_market` then shows **forecast gap** (vs normalized price, for scoring) and **trading EV** (model − ask − fees) separately. | Approve |
| 4 | P1 | **Injury observations**: one row per player per capture time; keep every status change. Capture every 1–2 hours on game days. | Approve |
| 5 | P1 | **Separate absence types**: record DNP / inactive / injury-listed per player per game (from the official box score and game summary, one or two extra calls per game). The injury-prone score is then built from injury-listed absences only. Keep total absence rate as a separate column, plus raw counts, sample size and method version. | Approve |
| 6 | P1 | **Four-level checks**: (a) shot arithmetic, (b) player points = final score, (c) player made/attempted shots = official team box score, (d) completeness (both teams present, ≥5 players each). Each is stored and flagged separately. | Approve |
| 7 | P1 | **Which numbers feed the shooting stats**: you asked for team shooting built from player lines. The review prefers official team totals. With check 6(c), the two only differ when something is wrong. | **Your call.** I'd keep player-derived numbers as you asked, and fall back to the official totals for any game that fails a check. |
| 8 | P1 | **Refresh and history**: re-check every unfinished or flagged game from the schedule every run; re-pull the last 14 days weekly and the full season monthly; save the old values to a `revisions` table before overwriting; log row counts and status per source. | Approve |
| 9 | P2 | **More features**: possessions/pace, offensive/defensive rating, rest days and back-to-backs, rolling 5/10/20-game form, home/away splits, expected available rotation minutes, Elo rating. | Approve, but build after 1–8 |
| 10 | P2 | **Evaluation**: Brier score, log loss, calibration table and simulated betting returns, reported separately. | Approve, with the model |

Network: items 3 and 5 add `clob.polymarket.com` to the domains needed (with `stats.nba.com`, `site.api.espn.com`, `gamma-api.polymarket.com`).

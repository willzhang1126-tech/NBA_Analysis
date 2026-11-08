# NBA Analysis

A self-updating NBA statistics database for analysis and for comparing a win-probability model against Polymarket prices.

It stores game results, team and player box scores, who sat out and why, daily injury reports, an injury-prone score for each player, and Polymarket order-book prices. Every update checks that player stats add up to the final score, and keeps the old value whenever the NBA corrects a stat.

## Quick start

```bash
pip install -r requirements.txt

python nba_db.py backfill 2021 2026   # load seasons 2021-22 to 2026-27 (rosters for the last 3)
python nba_db.py daily                # the daily update
python nba_db.py export               # writes nba_stats.xlsx
python nba_db.py status
```

Data goes into one SQLite file, `nba.db`. Set `NBA_DB_PATH` to put it elsewhere. No API keys are needed.

The first backfill takes a few minutes for games. Loading who sat out and why for 3 seasons takes about 4 hours, because it needs two requests per game.

## Commands

| command | what it does |
|---|---|
| `backfill START [END]` | load whole seasons (start years, e.g. `2021 2026`) |
| `daily` | schedule, new games, checks, injury report, injury scores, Polymarket, spreadsheet; weekly and monthly re-checks |
| `update` | games only: last 3 days, plus missing games, plus games that failed a check |
| `audit --days 14` / `audit --season 2026-27` | re-pull a window to catch late stat corrections |
| `injuries` | save the injury report now (run every 1–2 h on game days) |
| `odds` | save Polymarket order books now (run every 30–60 min on game days) |
| `profile` | rebuild injury-prone scores |
| `features GAME_ID [--cutoff ISO_TIME]` | model inputs for one game, using only information from before the cutoff |
| `export [--season 2026-27]` | analysis spreadsheet |
| `status` | row counts, failed checks, recent runs |

Example cron (ET):

```
0 10 * * *         cd ~/NBA_Analysis && python nba_db.py daily
0 12-23/2 * * *    cd ~/NBA_Analysis && python nba_db.py injuries
*/30 15-23 * * *   cd ~/NBA_Analysis && python nba_db.py odds
```

## Data sources

| data | source |
|---|---|
| games, box scores, schedule, rosters | stats.nba.com via [nba_api](https://github.com/swar/nba_api) |
| injury reports | ESPN public injuries feed |
| prices | Polymarket Gamma API (markets) and CLOB API (order books) |

stats.nba.com sometimes blocks requests from cloud servers. If it does, run the database from a home machine.

## Layout

```
nba_db.py              command line
nbadb/
  schema.sql           tables and views
  sources.py           every network call (swap these out in tests)
  ingest.py            load games, schedule, rosters, injuries, Polymarket
  checks.py            box-score checks; player-built team shooting with official fallback
  refresh.py           what to re-fetch each run; audits
  features.py          point-in-time model inputs (no look-ahead)
  injury_score.py      injury-prone score (method v2)
  predictions.py       append-only, versioned predictions
  export.py            spreadsheet
tests/test_nbadb.py    offline tests with fake data
docs/SCHEMA.md         full data format
docs/REVIEW_RESPONSE.md  logic review and the changes made
```

## Design rules

- **No look-ahead.** Training and backtesting use only `nbadb/features.py`, which reads games, injury reports and prices from before a cutoff time (tip-off by default). The SQL views show current numbers and are for browsing only.
- **Predictions can't be edited.** `model_predictions` is append-only and stores the exact inputs and cutoff used.
- **Market value uses the price you can actually pay.** Expected value is the model's probability minus the best ask minus fees (`settings.taker_fee_per_share`). The comparison against the market's fair price is a separate column, used to score forecasts.
- **Checks on every game.** Team shooting is built from player lines and falls back to the official team box score when a check fails. Failures show in the `data_check_failures` view and on the spreadsheet.
- **History is kept.** Corrected values are saved in `revisions` before they're overwritten.

## Tests

```bash
python tests/test_nbadb.py
```

These run offline against fake data sources.

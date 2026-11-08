"""Every network call lives here, so tests can swap them for fakes.
No API keys are needed for any of these sources."""
import time

import pandas as pd

REQUEST_PAUSE = 1.5  # stats.nba.com throttles fast clients
RETRIES = 4


def _retry(fn, what):
    for attempt in range(RETRIES):
        try:
            out = fn()
            time.sleep(REQUEST_PAUSE)
            return out
        except Exception as e:  # noqa: BLE001 - flaky upstreams
            if attempt == RETRIES - 1:
                raise RuntimeError(f"{what}: {e!r}") from e
            time.sleep(5 * (attempt + 1))


# ---------------------------------------------------------------- stats.nba.com

def game_log(season, season_type, kind, date_from="", date_to=""):
    """kind 'T' = team box scores, 'P' = player box scores (players who played)."""
    from nba_api.stats.endpoints import leaguegamelog
    return _retry(lambda: leaguegamelog.LeagueGameLog(
        season=season, season_type_all_star=season_type, player_or_team_abbreviation=kind,
        date_from_nullable=date_from, date_to_nullable=date_to, timeout=60).get_data_frames()[0],
        f"gamelog {season} {season_type} {kind}")


def schedule(season):
    from nba_api.stats.endpoints import scheduleleaguev2
    return _retry(lambda: scheduleleaguev2.ScheduleLeagueV2(season=season, timeout=60).get_data_frames()[0],
                  f"schedule {season}")


def boxscore_roster(game_id):
    """All players listed in the official box score, with the NBA's comment
    (e.g. "DNP - Coach's Decision"), plus the game's inactive list.
    Returns (players_df[gameId, teamId, personId, firstName, familyName, comment, minutes],
             inactive_df[gameId, teamId, personId, firstName, familyName])."""
    from nba_api.stats.endpoints import boxscoresummaryv3, boxscoretraditionalv3
    box = _retry(lambda: boxscoretraditionalv3.BoxScoreTraditionalV3(game_id=game_id, timeout=60)
                 .get_data_frames()[0], f"boxscore {game_id}")
    try:
        frames = _retry(lambda: boxscoresummaryv3.BoxScoreSummaryV3(game_id=game_id, timeout=60)
                        .get_data_frames(), f"summary {game_id}")
        names = boxscoresummaryv3.BoxScoreSummaryV3.expected_data.keys()
        inactive = dict(zip(names, frames)).get("InactivePlayers", pd.DataFrame())
    except RuntimeError:
        inactive = pd.DataFrame()
    return box, inactive


# ---------------------------------------------------------------- ESPN

ESPN_INJURIES = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries"


def espn_injuries():
    """Current league injury report as rows."""
    import requests

    def get():
        r = requests.get(ESPN_INJURIES, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        return r.json()
    data = _retry(get, "espn injuries")
    rows = []
    for team in data.get("injuries", []):
        for inj in team.get("injuries", []):
            ath = inj.get("athlete", {}) or {}
            det = inj.get("details", {}) or {}
            rows.append({
                "team_name": team.get("displayName", ""),
                "player_name": ath.get("displayName", ""),
                "status": inj.get("status", ""),
                "injury": det.get("type") or "",
                "detail": inj.get("shortComment", ""),
                "expected_return": det.get("returnDate", ""),
                "source_updated_at": inj.get("date", ""),
            })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- Polymarket

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"


def polymarket_nba_events():
    """Open NBA events (game markets plus futures; the caller filters)."""
    import requests
    events, offset = [], 0
    while True:
        def get():
            r = requests.get(f"{GAMMA}/events", timeout=30, params={
                "tag_slug": "nba", "closed": "false", "limit": 100, "offset": offset})
            r.raise_for_status()
            return r.json()
        batch = _retry(get, "polymarket events")
        events += batch
        if len(batch) < 100:
            return events
        offset += 100


def polymarket_book(token_id):
    """Order book for one outcome token: {'bids': [{price, size}], 'asks': [...]}."""
    import requests

    def get():
        r = requests.get(f"{CLOB}/book", params={"token_id": token_id}, timeout=30)
        r.raise_for_status()
        return r.json()
    return _retry(get, f"clob book {token_id[:10]}")

"""Analysis spreadsheet."""
import pandas as pd


def export(con, path="nba_stats.xlsx", season=None):
    season = season or con.execute("SELECT MAX(season) FROM games").fetchone()[0]
    rs = "AND season_type = 'Regular Season'"
    sheets = {
        "Team shooting": (f"SELECT * FROM team_shooting_season WHERE season = ? {rs} ORDER BY pts_pg DESC", [season]),
        "Standings": (f"SELECT * FROM team_season_stats WHERE season = ? {rs} ORDER BY win_pct DESC", [season]),
        "Matchups": ("SELECT * FROM matchups WHERE season = ? ORDER BY team, opponent", [season]),
        "Game scoring": ("SELECT * FROM team_game_scoring WHERE season = ? ORDER BY game_date, game_id", [season]),
        "Players": (f"SELECT * FROM player_season_stats WHERE season = ? {rs} ORDER BY pts_pg DESC", [season]),
        "Injury report": ("SELECT * FROM injury_report_current ORDER BY team, player_name", []),
        "Injury-prone": ("SELECT pl.name, ip.* FROM player_injury_profile ip JOIN players pl USING (player_id) "
                         "ORDER BY injury_prone DESC", []),
        "Model vs market": ("SELECT * FROM model_vs_market ORDER BY game_date", []),
        "Data checks": ("SELECT * FROM data_check_failures ORDER BY game_date", []),
        "Update log": ("SELECT r.run_id, r.command, s.source, s.detail, s.status, s.rows_fetched, s.rows_written, "
                       "s.rows_changed, s.error, s.at FROM source_runs s JOIN runs r USING (run_id) "
                       "ORDER BY s.at DESC LIMIT 500", []),
    }
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        for name, (q, args) in sheets.items():
            pd.read_sql(q, con, params=args).to_excel(xw, sheet_name=name, index=False)
            ws = xw.sheets[name]
            ws.freeze_panes = "B2"
            for col in ws.columns:
                ws.column_dimensions[col[0].column_letter].width = max(9, min(28, len(str(col[0].value)) + 2))
    print(f"Wrote {path} for season {season}")

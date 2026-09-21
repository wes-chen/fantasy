# API notes for the trade board engine

## Sleeper (public, no auth)

- League: `GET https://api.sleeper.app/v1/league/{league_id}`
  - `roster_positions`: slot list (`QB`, `RB`, `WR`, `TE`, `FLEX`, `SUPER_FLEX`, `BN`, `IR`…)
  - `scoring_settings.rec`: 1.0 full PPR, 0.5 half
- Rosters: `GET /v1/league/{id}/rosters` → `players[]` (Sleeper player IDs),
  `reserve[]` (IR), `settings.wins/losses`, `settings.waiver_position`,
  `owner_id`, `roster_id`
- Users: `GET /v1/league/{id}/users` → `user_id` → `display_name`
- Transactions: `GET /v1/league/{id}/transactions/{round}` (rounds 1–18).
  `type == "trade"` + `status == "complete"`. `adds`: player_id → receiving
  roster_id. `drops` mirrors it.
- Players DB: `GET https://api.sleeper.app/v1/players/nfl` (~15MB).
  Cache it (Sleeper asks you to); refresh weekly. Keys are Sleeper IDs.
- Trending: `GET /v1/players/nfl/trending/add?lookback_hours=24&limit=25`

Wesley: username `weslchen`, user_id `1264143735290081280`.

## FantasyCalc (public, no auth) — objective value baseline

`GET https://api.fantasycalc.com/values/current?isDynasty=false&numQbs={1|2}&numTeams={n}&ppr={0|0.5|1.0}`

Match to league settings: `numQbs=2` for Superflex (this is what prices
the QB premium), `numTeams` = league size, `ppr` from scoring settings.
Redraft: `isDynasty=false`.

Entry shape: `{"player": {"sleeperId", "name", "position", ...},
"value": <int>, "positionRank": <int>, "overallRank": <int>, "trend30Day": ...}`.
Note: `positionRank`/`overallRank` are top-level entry keys, NOT inside
`player`. Join to Sleeper data on `str(player.sleeperId)`.

Startable cutoffs used by the engine (positional rank that fills league
starting slots + flex depth): QB = teams×2.2 (SF) or ×1.3 (1QB),
RB = teams×2.6, WR = teams×3.0, TE = teams×1.5. Tune if they drift.

## RotoTrade / calculators

Wesley has used rototrade-style calculators. They underweight roster
context (the hole a trade creates) and league-specific markets — that is
exactly what the persona's judgment layers add on top.

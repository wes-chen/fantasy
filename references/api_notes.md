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

## FantasyPros rest-of-season ECR + bye weeks (scraped, no auth)

`GET https://www.fantasypros.com/nfl/rankings/{ppr|half}-ppr-cheatsheets.php`
(use `ppr` for full-PPR leagues, `half` for half-PPR). The page embeds
`var ecrData = {...}` JSON: `players[]` with `player_name`,
`player_bye_week`, `rank_ecr`, `pos_rank`, `tier`, `player_owned_avg`.

Notes (verified 2026-09-21):
- No API key needed, but the anonymous `partners.api.fantasypros.com`
  endpoint is unreachable from this network — page scrape is the working
  path. Send a browser User-Agent.
- `ecrData.week` is 0 on the cheatsheet (rest-of-season, not weekly) —
  that is the right signal for trades; label it honestly.
- Join to Sleeper IDs on normalized names (lowercase, strip periods /
  apostrophes / suffixes Jr/Sr/II/III/IV/V). On collisions prefer
  `active` players with the lowest `search_rank` (e.g. "Kenneth Walker"
  collides with a stale WR entry; the real one is the active KC RB).
- Bye weeks power the engine's bye audit: clusters of 3+ on one bye are
  flagged, and swaps that stack onto a 2+ bye week get `[BYE-STACK]`.
- `rank_ecr` vs FantasyCalc `overallRank`: |diff| >= 12 spots is flagged
  as a buy-low/hold (ECR higher) or sell-high (calc higher) candidate.

## Sleeper news trigger (zero-auth, for the interrupt-driven news scan)

The players DB carries `news_updated` (ms epoch) per player. Diff the
live `GET https://api.sleeper.app/v1/players/nfl` against the cached
copy for Wesley's rostered players: a newer `news_updated` means fresh
news on his guy — then use web search to find what it actually is.
402 players had news in the preceding 48h on 2026-09-21, including his
own (Stevenson, Daniel Jones). Headline text itself needs a FantasyPros
v2 API key (not yet obtained) — search is the fallback.

## Sleeper season clock

`GET https://api.sleeper.app/v1/state/nfl` → `week` (also `season`).
League `settings.trade_deadline` (both his leagues: 11). The engine maps
week → posture: W1-4 ACQUIRE TALENT, W5-8 BALANCED, W9-11 WIN NOW,
past deadline: waivers only.

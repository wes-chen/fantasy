# API notes for the trade board engine

## Sleeper (public, no auth)

- League: `GET https://api.sleeper.app/v1/league/{league_id}`
  - `roster_positions`: slot list (`QB`, `RB`, `WR`, `TE`, `FLEX`, `SUPER_FLEX`, `BN`, `IR`…)
  - `scoring_settings.rec`: 1.0 full PPR, 0.5 half
- Rosters: `GET /v1/league/{id}/rosters` → `players[]` (Sleeper player IDs),
  `reserve[]` (IR), `settings.wins/losses`, `settings.waiver_position`,
  `owner_id`, `roster_id`
  - `settings.waiver_position` is LIVE post-spend order, not the weekly
    order: after a manager spends claims it drops to the back until the
    weekly reset (snapusa showed 14/14 on 2026-10-27 while reverse
    standings put Wesley 11th). Both leagues run `waiver_type=1`
    (reverse standings, reset weekly); `reset_waiver_priority()` derives
    the reset order (worst-first by wins, then points-for) and the board
    prints it in WAIVER PRIORITY COST whenever it differs from the live
    slot — Monday claims price off the reset order.
  - IR slots are NOT in `roster_positions` — they live in
    `settings.reserve_slots` (league-level count) + each roster's `reserve[]`
    (occupants). A player in `players[]` may already be in `reserve[]`
    (i.e. in the IR slot, not on the bench) — always check `reserve[]`
    before recommending any IR move. (Learned 2026-09-23: both of Wesley's
    leagues show no IR in roster_positions but have `reserve_slots: 1`.)
- Users: `GET /v1/league/{id}/users` → `user_id` → `display_name`
- Transactions: `GET /v1/league/{id}/transactions/{round}` (rounds 1–18).
  `type == "trade"` + `status == "complete"`. `adds`: player_id → receiving
  roster_id. `drops` mirrors it.
- Players DB: `GET https://api.sleeper.app/v1/players/nfl` (~15MB).
  Cache it (Sleeper asks you to); refresh weekly. Keys are Sleeper IDs.
- Trending: `GET /v1/players/nfl/trending/add?lookback_hours=24&limit=25`
  (Sleeper-WIDE add counts across all leagues, not league-specific —
  a proxy for waiver urgency, never presented as his league's adds).
  G3 velocity = this scan's 24h adds vs the 24h adds ending at the
  previous scan (both trailing-24h windows, so the ratio is clean;
  a baseline less than an hour old is too overlapping to use).
  Snapshots persisted at `hidden_files/trending_snapshots.json` (kept
  after every successful scan). No usable baseline, or player absent
  from it (or at zero there) → NEW (never faked). **Exactly one
  trending call per scan** (standing rule).

## nflverse (bulk CSVs, no auth — verified 2026-09-22)

Release-tagged flat files (nflverse/nflverse-data on GitHub). The engine
caches them 24h (`nflverse_paths()`); every consumer degrades to a
marked stub when the fetch fails.

- Player weekly stats: release `player_stats`, file
  `stats_player_week_2026.csv.gz` — `target_share`, `air_yards_share`,
  `receiving_air_yards`, `receiving_epa`, `targets`, fantasy points.
  Powers G1 (usage-gap radar) and G5 (defensive strength allowed by
  position).
- Snap counts: release `snap_counts`, file `snap_counts_2026.csv` —
  `offense_snaps`, `offense_pct`. G1 snap proxy + G6 INFERRED fallback.
- Depth charts: release `depth_charts`, file `depth_charts_2026.csv`
  (~52MB) — daily snapshots; dedupe by latest `dt` PER GSIS ID, not per
  name (names change between snapshots; name-keying leaves phantom
  duplicates). Powers G6 handcuff map.
- Schedules: release `schedules`, file `games.csv.gz` (NOT
  `sched_2026.csv` — that path 404s). Carries ESPN event IDs per game.
  Powers G5 (W15-17 opponents) and G10 (weekly event IDs).
  TWO GOTCHAS (both were real bugs): the regular-season column is
  `game_type`, not `season_type` (player_stats uses `season_type`);
  and the file spans 1999-present, so every reader filters
  `season == "2026"` — unfiltered reads blend 28 years of slates.
  Bye detection uses the fixed 32-team NFL set; a playoff week with no
  schedule rows is UNSUPPORTED (marked neutral), never 32 phantom byes.

HARD LIMIT: route participation is in NONE of the free sources
(nflverse, PFR, FTN). Never synthesize it — target share + air-yards
share + snap% are the route proxies and are labeled as such everywhere
they appear.

Join to Sleeper IDs on normalized (name, team, pos): ~95% of his
skill-position roster matched on 2026-09-22 (Sleeper's own GSIS
coverage is only ~31%, so name+team+pos is the working join).

## ESPN odds (core API — verified 2026-09-22, re-verified 2026-09-26)

- The public scoreboard endpoint
  (`site.api.espn.com/apis/site/v2/sports/nfl/scoreboard`) returns 403
  from this network despite a browser User-Agent. Do not use it.
- Working path:
  `https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/events/{eventId}/competitions/{eventId}/odds`
  (no `?limit` needed). Game IDs come from nflverse `games.csv.gz` (ESPN
  event IDs). Parse the DraftKings provider entry (`provider.name ==
  "DraftKings"`; the code matches `"draft"` case-insensitively and falls
  back to any item carrying a `spread`). Re-verified 2026-09-26 on NFL
  Week 3: 16/16 games priced, zero errors.
- GAME-LEVEL ONLY: spread, total, moneylines. No player props exist in
  free data. G10 turns these into start/sit script + shootout signals
  (line >= 7.0 → positive script for the favorite's pieces; total >=
  48.0 → shootout watch; total <= 41.5 → grind fade; underdog catching
  >= 7.0 → negative script; total moved >= 2.0 from open → market-moved
  note).
- Degraded path: keep the last good `hidden_files/game-odds.md`, exit
  nonzero, print "No pricing data available today".

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

`GET https://www.fantasypros.com/nfl/rankings/ros-ppr-overall.php`
(full-PPR leagues) or
`GET https://www.fantasypros.com/nfl/rankings/ros-half-point-ppr-overall.php`
(half-PPR). The page embeds
`var ecrData = {...}` JSON: `players[]` with `player_name`,
`player_bye_week`, `rank_ecr`, `pos_rank`, `tier`, `player_owned_avg`.

Notes (verified 2026-09-28):
- No API key needed, but the anonymous `partners.api.fantasypros.com`
  endpoint is unreachable from this network — page scrape is the working
  path. Send a browser User-Agent.
- Do NOT use the `{ppr|half}-ppr-cheatsheets.php` URLs (gh #24): they
  302 into the PRESEASON draft cheatsheet (`ranking_type_name: "draft"`,
  title "2026 Fantasy Football Draft Rankings") — comparing live
  FantasyCalc values to August draft ranks makes every buy-low/sell-high
  verdict stale by construction. The true ROS pages carry
  `ranking_type_name: "ros"` and `type` "ROS PPR"/"ROS Half PPR".
- `ecrData.week` is 0 on the ROS pages too (FantasyPros leaves it unset),
  so `week` is NOT a staleness signal — `last_updated` (e.g. "9/28") is.
  The engine rejects any non-`ros` page and degrades to "unavailable
  this run" rather than printing draft ranks as ROS ECR.
- Join to Sleeper IDs on normalized names (lowercase, strip periods /
  apostrophes / suffixes Jr/Sr/II/III/IV/V). On collisions prefer
  `active` players with the lowest `search_rank` (e.g. "Kenneth Walker"
  collides with a stale WR entry; the real one is the active KC RB).
- Bye weeks power the engine's bye audit: clusters of 3+ on one bye are
  flagged, and swaps that stack onto a 2+ bye week get `[BYE-STACK]`.
- `rank_ecr` vs FantasyCalc `overallRank`: |diff| >= 12 spots is flagged
  as a buy-low/hold (ECR higher) or sell-high (calc higher) candidate.

### FantasyPros ROS ranks for K/DST (gh #20)

FantasyCalc publishes no K/DEF values, so the waiver engine prices K/DEF
by FantasyPros ROS rank instead of the value gate (verified the source
2026-10-04: `ros-k.php` 38 kickers, `ros-dst.php` 32 defenses, both
`ranking_type_name: "ros"`). `get_fp_kdef()` scrapes both pages (same
`var ecrData` embed, same browser UA, same ros-type guard as the overall
feed) and joins to Sleeper IDs on normalized names — DEFs via
`"first_name last_name"` since Sleeper stores them with `full_name=None`
(player_id is the team abbrev, e.g. `HOU`). Suggestion gates
(`KDEF_STREAM_TOP=12`, `KDEF_RANK_GAP=8`): only name a swap for a top-12
ROS unit at least 8 ranks better than his current one — the 1.4x churn
spirit, no rank-25-vs-rank-33 churn on the 14-team wire. A hurt current
unit is always a forced replacement (top healthy FA, any rank). Degrades
to forced-replacement-only when the FP fetch fails.

### K/DEF bye-week forced replacement (#30, 2026-10-25)

The same `ros-k.php`/`ros-dst.php` pages carry `player_bye_week` (a
string; a few kickers omit it — reads None, never a phantom week).
`get_fp_kdef()` now returns `{sid: (rank, bye_week)}`; the board derives
`kdef_byes` from it. Sleeper's players DB carries no bye_week for K/DEF
(verified 2026-10-25: 0/32 DEFs, 0/192 Ks), so FantasyPros is the only
bye source here. A rostered K/DEF whose bye is the current NFL week
scores zero but is neither hurt nor a decisive-upgrade case, so the old
code stayed silent (the Oct 23 JAX Week-7 bye was handled by hand):
now the roster line carries `[ON BYE W..]` and the waiver branch names a
forced replacement — the best-ranked healthy FA who is NOT on bye this
week, any rank (`kdef_bye_replacement()`). No fall-through to the
upgrade gate: a bye-week FA scores zero too. When every healthy FA is
also on bye, no swap is named — the tag still warns.

### Teamless players never surface on waiver lines (2026-11-11)

Sleeper keeps cut players `active: true` with `team: null` (verified
2026-11-11: Younghoe Koo, cut by the Jets 2026-06-03, still active and
teamless — and the Nov 10 board recommended him as a K add; he sat #2
on the top-FA K line as "(unpriced)" on Nov 11). The old FA-pool filter
checked `active` only, so teamless players flowed into the top-FA lines,
the K/DEF bye replacement, and the upgrade/suggestion gates. A player
with no NFL team cannot score fantasy points, so the pool build now
also requires a truthy `team` (DEFs carry their team abbrev, so the
filter does not touch them), and `ir_stash_candidates()` applies the
same guard — a teamless IR/Out player has no return to stash for. The
golden suite pins the invariant live (no teamless name on any top-FA
line or suggested ADD, either league) plus a unit check on the stash
filter. Note the trending-velocity line is a raw Sleeper-adds signal,
not a recommendation surface, and is not filtered.

### Top-FA CLAIM tag (2026-11-12)

The mirror case ADV-FF-18 left open: the suggested-move lines already
carried `[CLAIM — clears ..., burns #N]` for waiver-locked players, but
the top-FA lines — the first surface a reader scans — printed them as
plain free agents. Live case Nov 10: Eddy Pineiro, dropped Mon 23:34
PT and locked until Wed 11:34pm PT, sat on the top-FA K line with no
indication, and chat called him "still free league-wide" (the Sleeper
rosters API shows unrostered, not waiver status). The `_path` /
`_pathtag` helpers are now hoisted above the top-FA loop, and a single
`_claimtag()` helper renders the CLAIM text for both surfaces, so the
ETA can never drift between them; the top-FA formatter appends it
when `acquisition_path()` says claim, and FA NOW players stay untagged
(the default). Tag only, never a veto or a reselection. The tag reads
`[CLAIM — <eta>; burns #N]` — semicolon, not comma, before "burns":
the top-FA lines are comma-joined and the golden pins split segments
on ", ", so a comma inside the tag fragments the segment (the first
run broke the P-tag pin exactly this way). Live cases
2026-11-12: snapusa McGowan / Harrison Jr. / Worthy / Brian Thomas
Jr. / Smack, WW Jeanty / Likely. The golden suite pins the
biconditional live per league (tag iff the recent-drop recompute says
claim) plus a source-count pin on the shared helper. Note the
trending-velocity line stays untagged — same reasoning as the teamless
note above, it is a signal line, not a recommendation surface.

### Suggested-move bye-week tag (2026-10-26)

The mirror case #30 left open: the RECOMMENDED player on bye, not his
own unit. The WAIVER WIRE top-FA lines already showed each FA's bye via
`_btag`, but the suggested-move lines — the lines Wesley acts on — did
not, so a Monday claim for a player who can't start Sunday hid the one
fact that changes the claim decision. `on_bye_this_week_tag()` now
appends `[ON BYE W.. — scores zero this week]` to every suggested ADD
(skill positions via the FP overall feed's `player_bye_week`, K/DEF via
`kdef_byes`) whose bye is the current NFL week. Tag only, never a
veto or a reselection: unlike the K/DEF replacement case, a bye-week
skill player can still be the right claim for rest-of-season value —
the tag prices the week, it doesn't kill the move.

### Swap-row bye-week tag (2026-11-02)

Same gap on the trade side: CANDIDATE SWAPS and 2-FOR-1 rows printed
`[bye N]` for the incoming player via `_btag`, but only the explicit
tag says he scores zero THIS week — a bare bye number needs the reader
to know the current NFL week. `on_bye_this_week_tag()` (FP overall
feed's `player_bye_week` vs current week) now appends
`[ON BYE W.. — scores zero this week]` to the incoming player on both
proposal row types. Tag only, never a veto or a reselection — same
reasoning as the waiver tag. Live case: the top snapusa row on
2026-11-02 sent Okonkwo for Chris Olave, bye 8 in NFL Week 8.

### Swap-row injury tag (2026-11-03)

Same gap, different axis: proposal rows printed clean names even for
an injured player — the top 2026-11-03 snapusa row offered Michael
Wilson (Questionable) and row 3 offered Carnell Tate (Questionable)
with no indication, while top-FA and trending lines already carry
`[!STATUS]`. `injury_tag()` (module-level pure helper; `_itag()`
closure in `main()` resolves Sleeper `injury_status` from the players
cache) now appends `[!STATUS]` to every side of every 1-for-1 and
2-for-1 row, right after that player's values. Tag only, never a veto
or reselection: Questionable is playable, and Out/IR/Doubtful/
Suspended are already E5-discounted in the lineup delta — the tag
names why the delta moved, and flags the game-time risk on the player
the row says "starts".

### Roster + hole-line injury tag (2026-11-04)

Same gap, remaining surfaces: YOUR ROSTER lines and HOLE-CREATING
lines still printed clean names — the at-a-glance view and the
hole-pricing surface, which the persona reads before any proposal row.
The same `_itag()` closure now appends `[!STATUS]` to every roster
line (all six positions, K/DEF included) and every hole-creating line.
Tag only, never a veto or reselection, same as the swap-row tag. Live
cases on the 2026-11-04 snapusa board: Joe Fagnano `[!Out]` and
Davante Adams `[!IR]` on the roster lines; the hole line (Dak
Prescott, healthy) correctly untagged.

### Waiver P-tag (2026-11-05)

Same gap, waiver side: the G5 `[P+ soft]`/`[P- brutal]` tag lived only
on YOUR ROSTER lines and inside swap deltas, while the waiver lines —
the Monday-claim surface — showed value alone. On Nov 4 Wesley had to
ask for a manual rerun to rank wire options by playoff schedule; under
that lens Brian Thomas Jr. jumped to #1 over Chris Bell on
Jacksonville's soft W15-17 WR slate. The existing `ptag_of()` closure
now appends the tag to every top-FA segment (`_fa_fmt`) and to the ADD
segment of every skill-position suggested move (K/DEF lines are a
no-op: G5 never tags those positions). Tag only, never a veto or a
reselection, and it never moves the suggestion ranking — the tag
thresholds (narrow band [P+] >= 1.05 / [P-] <= 0.95; wide band
>= 1.10 / <= 0.90) only fire on extreme slates, so a mildly soft slate
like BTJ's 1.026 stays untagged. Live cases on the 2026-11-05 WW board
(wide band): Trevor Lawrence, Dak Prescott, Matthew Golden, Dalton
Kincaid `[P+ soft]`, Josh Jacobs `[P- brutal]`; the snapusa wire was
all-neutral that day. The legend line now says waiver ADDs carry the
same tag.

### Radar injury tag (2026-11-06)

Same gap, radar surface: the G1 usage-gap lines (HOLD/SHOP/TARGET/
WIRE) printed bare names even for a Questionable player — the veto
already excludes Out/IR/Doubtful/Suspended from the radar, so a tag
here can only be Questionable, but a buy-the-usage line on a player
who may not play reads very differently untagged. The `_u1`
formatter now appends `injury_tag()` (Sleeper `injury_status` via
`sid_of_nkey`) right after the `(POS,TEAM)` parenthetical, plus a
legend line in the radar header. Tag only, never a veto or a
reselection — Questionable is playable, and the usage signal is still
real; the tag names the game-time risk on the buy-low. Live cases on
the 2026-11-06 boards: A.J. Brown `[!Questionable]` (missed Week 8,
DNP Wed, TBD vs GB Sun) on both boards, Dontayvion Wicks
`[!Questionable]` on snapusa.

### G8 clog-audit + divergence injury tag (2026-11-07)

Same gap, last two bare-name surfaces: the G8 roster-clog lines
(incl. DROP CANDIDATES) and the value-divergence lines still printed
clean names — the drop surface and the sell-high surface, which the
persona reads before any proposal row. The G8 render appends
`injury_tag()` (designation already carried in the ranked dicts'
`"inj"` field) after the contingent-value parenthetical, and the
DROP CANDIDATES line rebuilds its names from the ranked dicts so the
tag follows there too; `value_divergences()` appends the tag right
after the player's name, plus a legend line under the section header.
Tag only, never a veto or a reselection. The divergence veto already
excludes Out/IR/Doubtful/Suspended, so a tag there is Questionable;
a G8 line can show Out (an Out bench player is droppable, but the
tag names why he is cheap). Live cases on the 2026-11-07 boards: A.J.
Brown `[!Out]` on the WW clog audit (and on the DROP CANDIDATES line
— he was Questionable when the radar tagged him yesterday; the knee
flare-up has since ruled him out); the snapusa divergence lines were
all-healthy today, with 1 injured player excluded by the veto.

### Pending-offer STALE flag (2026-11-08)

The PENDING OFFERS section printed every open offer the same way,
with no age — a dead offer kept blocking trades silently. Registry
history: offers resolve in 0-2 days normally (Sep 20-29 rejections
within 2 days, Nov 5 feeler countered same day); the longest-lived
resolved offer, the Oct 28 Harrison counter, resolved on day 7. So
`offer_is_stale()` (with `offer_age_days()`, both fail-open on
unparsable/future dates) flags an offer open 7+ days, and the render
appends `<-- STALE (open Nd — verify still open in-app; only chat
resolves the row)` to its line. Display only — the ON-BLOCK stays,
same as the pending-claims STALE pattern; only chat edits the
registry, never auto-clear. Live case: the Nov 5 Jeanty+Vele→Lawrence
counter died at its 5:15pm lock that evening but was still `pending`
on Nov 8, keeping Jeanty on-block on the snapusa board; the flag
would have surfaced it for resolution on day 7 (Nov 12) at the
latest, and the Tuesday worker now sees the age on every open offer.

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
past deadline: waivers only. Past deadline the CANDIDATE SWAPS and
2-FOR-1 sections print only a suppression note (headers kept for the
Tuesday parser); the gate is `tb.proposals_suppressed()` on the ADV-FF-14
boundary (legal through Week N, locked at N+1), same boundary as the
posture line.

## Superflex QB coverage gap (2026-10-31)

`fi.coverage_gaps(..., is_sf=True)` flags a Superflex roster with no QB
beyond the QB + SUPER_FLEX slots — the Drew Lock lesson position. A QB
scratch there drops SUPER_FLEX to a flex player (a lineup hit, not a
forced zero like the TE gap), and the line carries `[EXPOSED:
<designation>]` when a QB starter is Questionable or worse. A waiver QB
add that closes it is tagged `[closes QB coverage gap]` via the same
`tb.closes_coverage_gap()` predicate as TE. Live in snapusa from the
Cousins trade on (Dak + Jones only, no QB3); skipped in wire-rich
(<=6-team) leagues like the rest of the audit.

## Partner-fit mirror on 1-for-1 swaps (2026-11-13)

The old 1-for-1 gate was the coarse `need_score` thinness flag: the
partner had to be thin (fewer startable players than slots+1) at the
sent player's position. Thin is not fit, in both directions. The live
cases from the Nov 10 board: the Olave holder's RB need read 0 (Kaelon Black
patched their room) yet Derrick Henry would still start for them — the
engine printed "(no clean 1-for-1 fits)" while the persona overrode it
with Henry→Olave; and the Nov 13 board's top 12 rows were all Chig
Okonkwo (133) for 583–1380 players at 77–90% UNFAIR gaps — +986 for
Wesley, deeply negative for the partner, who would never say yes.

The gate is now the partner's own lineup-points delta: `swap_delta`
with the roles reversed (partner sends `theirs`, receives `mine`) on
the same injury-discounted, G5 playoff-weighted basis, against the
partner's discounted projected lineup (computed once per partner).
Rows require it > 0 and print it as `[their lineup +N]` right after
the `[lineup ...]` bracket. Rows failing it are dropped with a count
note mirroring the lateral-drop note ("sent player couldn't crack
their projected starting lineup"). The partner's bye-cluster veto is
NOT applied: the 20% 3-starter penalty is already inside their delta,
and their bye week is their problem, not a reason to hide the row.
The 2-for-1 section keeps its own need gate (both pieces must be a
position they need) — extending the mirror there is a follow-up, not
this run. Golden tests: synthetic role-reversed `swap_delta` checks
(deep-but-weak partner still gains; the one-sided trap loses 35) plus
live pins that every 1-for-1 row carries a positive tag and the
partner-dropped note matches the mirror format.

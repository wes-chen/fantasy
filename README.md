# fantasy

Fantasy football trade research tools.

## fantasy-trade-analyst (agent skill)

A research persona + engine for recommending Sleeper fantasy football trades:

- **`SKILL.md`** — the analyst persona: prices the roster hole (not just the players), requires both-sides motivation, uses league trade history as market comps, treats FantasyCalc as baseline not verdict.
- **`bin/trade_board.py`** — the engine: live Sleeper rosters + settings-matched FantasyCalc values + completed league trades -> needs/surplus board and candidate swaps by value gap.
- **`references/api_notes.md`** — Sleeper + FantasyCalc API details and join keys.

Usage:

```bash
python3 bin/trade_board.py --league 1320161122837368832 --me 1264143735290081280
```

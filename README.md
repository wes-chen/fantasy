# fantasy

Fantasy football trade research tools.

## fantasy-trade-analyst (agent skill)

A research persona + engine for recommending Sleeper fantasy football trades:

- **`SKILL.md`** — the analyst persona: prices the roster hole (not just the players), requires both-sides motivation grounded in each team's record and possible points, uses league trade history as market comps, treats FantasyCalc as a baseline not a verdict (daily values lag breaking news — always check the 30-day trend and injury flags), and plays 4-team and 14-team leagues as different sports (waiver ocean vs waiver desert).
- **`bin/trade_board.py`** — the engine: live Sleeper rosters + settings-matched FantasyCalc values + completed league trades -> team needs with record/PF/possible-points context, candidate swaps by value gap, and a waiver-wire section (top free agents by position with 30-day value trends and injury flags, trending adds, add/drop suggestions vs your bench).
- **`references/api_notes.md`** — Sleeper + FantasyCalc API details and join keys.

Usage:

```bash
python3 bin/trade_board.py --league 1320161122837368832 --me 1264143735290081280
```

#!/usr/bin/env python3
"""Golden regression tests for trade_board.py (ADV-FF-11).

The daily self-improvement job mutates this engine unsupervised. Exit code
alone can't catch a neutered fit veto or a broken slot model, so these tests
assert FUNCTIONAL invariants, not just exit 0:

  unit: slot math, injury discounts, churn threshold, K/DEF coverage,
        pending-offer parsing
  integration: both league boards run clean AND print the structures the
        findings require (2-flex model, K/DEF waiver lines, ON-BLOCK flags,
        delta-sorted swap header, trade-lock line, stable section headers
        the Tuesday worker parses).

Run: python3 tests/test_golden.py   (from the skill dir; exit != 0 on failure)
"""
import os
import re
import subprocess
import sys
import tempfile
import time

SKILL = os.path.expanduser("~/workspace/skills/fantasy-trade-analyst")
sys.path.insert(0, os.path.join(SKILL, "bin"))
import trade_board as tb  # noqa: E402  (import runs no network)
import fantasy_insights as fi  # noqa: E402

SNAPUSA = "1320161122837368832"
WW = "1379714328738955264"
ME = "1264143735290081280"

failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {detail}")
        failures.append(name)


# ---------------- unit ----------------
print("== unit ==")
check("flex: snapusa-like slots -> 2",
      tb.count_flex_slots(["QB", "RB", "RB", "WR", "WR", "TE",
                           "FLEX", "FLEX", "SUPER_FLEX", "K", "DEF"]) == 2)
check("flex: WW-like slots -> 2",
      tb.count_flex_slots(["QB", "RB", "RB", "WR", "WR", "TE",
                           "FLEX", "FLEX", "K", "DEF"]) == 2)
check("flex: SUPER_FLEX alone is not a flex slot",
      tb.count_flex_slots(["QB", "SUPER_FLEX"]) == 0)

check("injury discount Out=0.5", tb.injury_discount("Out") == 0.5)
check("injury discount IR=0.5", tb.injury_discount("IR") == 0.5)
check("injury discount Doubtful=0.7", tb.injury_discount("Doubtful") == 0.7)
check("injury discount Questionable=0.85",
      tb.injury_discount("Questionable") == 0.85)
check("injury discount healthy=1.0",
      tb.injury_discount("") == 1.0 and tb.injury_discount(None) == 1.0
      and tb.injury_discount("Probable") == 1.0)

# ---------------- E5: season-timing injury discount ----------------
_approx = lambda got, want: abs(got - want) < 1e-9
check("E5: Out wk3 -> 0.5 (no timing decay)",
      _approx(tb.injury_discount("Out", 3), 0.5))
check("E5: Out wk8 -> 0.5*0.85=0.425",
      _approx(tb.injury_discount("Out", 8), 0.425))
check("E5: Out wk14 -> 0.5*0.6=0.3",
      _approx(tb.injury_discount("Out", 14), 0.3))
check("E5: IR decays with season progress",
      _approx(tb.injury_discount("IR", 2), 0.5)
      and _approx(tb.injury_discount("IR", 10), 0.425))
check("E5: Doubtful wk7 -> 0.7*0.85=0.595",
      _approx(tb.injury_discount("Doubtful", 7), 0.595))
check("E5: Questionable stays static at 0.85 in every week",
      tb.injury_discount("Questionable", 3) == 0.85
      and tb.injury_discount("Questionable", 14) == 0.85)
check("E5: Suspended stays static at 0.5",
      tb.injury_discount("Suspended", 14) == 0.5)
check("E5: week unknown -> current static behavior",
      tb.injury_discount("Out") == 0.5
      and tb.injury_discount("IR", None) == 0.5
      and tb.injury_discount("Doubtful", None) == 0.7)
check("E5: healthy stays 1.0 in every week",
      tb.injury_discount("", 14) == 1.0
      and tb.injury_discount("Probable", 14) == 1.0)
check("E5: week bracket boundaries",
      tb.injury_week_bracket(6) == 1.0
      and tb.injury_week_bracket(7) == 0.85
      and tb.injury_week_bracket(12) == 0.85
      and tb.injury_week_bracket(13) == 0.6
      and tb.injury_week_bracket(None) == 1.0)


check("churn threshold is 1.4 (persona 40% veto)",
      tb.CHURN_THRESHOLD == 1.4, f"got {tb.CHURN_THRESHOLD}")
check("WPOS covers K/DEF", "K" in tb.WPOS and "DEF" in tb.WPOS,
      f"got {tb.WPOS}")

# ---------------- E6: fairness bands ----------------
check("E6: band <10% -> EXCELLENT", tb.fairness_band(0.09) == "EXCELLENT")
check("E6: band 10% -> FAIR (boundary)", tb.fairness_band(0.10) == "FAIR")
check("E6: band 20% -> FAIR (boundary)", tb.fairness_band(0.20) == "FAIR")
check("E6: band 21% -> STRETCH", tb.fairness_band(0.21) == "STRETCH")
check("E6: band 35% -> STRETCH (boundary)",
      tb.fairness_band(0.35) == "STRETCH")
check("E6: band 36% -> UNFAIR", tb.fairness_band(0.36) == "UNFAIR")

# ---------------- E7: value ranges + low-signal flag ----------------
check("E7: range half-width = 50% of |trend30d|",
      tb.value_range(4000, 800) == (3600, 4400))
check("E7: range symmetric for negative trends",
      tb.value_range(4000, -800) == (3600, 4400))
check("E7: zero trend -> point range",
      tb.value_range(4000, 0) == (4000, 4000))
check("E7: lo clamped at 0", tb.value_range(100, -600) == (0, 400))
check("E7: flat trends + small delta -> LOW-SIGNAL LATERAL flag",
      "LOW-SIGNAL LATERAL" in
      tb.low_signal_lateral(False, False, 50, 4000, 4000))
check("E7: a moving side clears the flag",
      tb.low_signal_lateral(True, False, 50, 4000, 4000) == ""
      and tb.low_signal_lateral(False, True, 50, 4000, 4000) == "")
check("E7: big delta clears the flag",
      tb.low_signal_lateral(False, False, 900, 4000, 4000) == "")


with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
    fh.write("# comment\n\n")
    fh.write("6819 | Michael Pittman | ydai | 2026-09-20 | pending | x\n")
    fh.write("1234 | Some Guy | mate | 2026-09-01 | rejected | y\n")
    tmp = fh.name
parsed = tb.load_pending_offers(tmp)
os.unlink(tmp)
check("pending offers: pending parsed, rejected ignored",
      set(parsed) == {"6819"} and parsed["6819"]["partner"] == "ydai",
      f"got {parsed}")
check("pending offers: missing file -> empty, no crash",
      tb.load_pending_offers("/nonexistent/path.md") == {})

check("ir_capacity: 1 slot, 1 used -> (1, 0)",
      tb.ir_capacity(1, 1) == (1, 0))
check("ir_capacity: 1 slot, empty -> (1, 1)",
      tb.ir_capacity(1, 0) == (1, 1))
check("ir_capacity: no slots -> (0, 0)",
      tb.ir_capacity(None, 0) == (0, 0))
check("ir_capacity: over-full clamps at 0 open",
      tb.ir_capacity(2, 5) == (2, 0))
check("ir_capacity: string slots parse",
      tb.ir_capacity("1", 1) == (1, 0))
check("ir_line: full IR warns",
      tb.ir_line(["Isiah Pacheco"], 1, 1) ==
      "  IR: Isiah Pacheco (1/1 used, 0 open) — IR FULL: the next injury "
      "costs an ACTIVE roster spot; injured stashes can't hide here")
check("ir_line: open IR shows dash names, no warning",
      tb.ir_line([], 1, 0) == "  IR: \u2014 (0/1 used, 1 open)")
check("ir_line: no IR slots -> no FULL warning",
      "IR FULL" not in tb.ir_line([], None, 0))

# ---------------- ADV-FF-18 / E14: acquisition path + slot arithmetic ---
_NOW = 1790203000000  # fixed "now" (ms) for deterministic tests
check("acquisition_path: dropped 1 day ago (2-day lock) -> claim",
      tb.acquisition_path("p1", {"p1": _NOW - 1 * 86400 * 1000}, 2, _NOW)
      == "claim")
check("acquisition_path: dropped 10 days ago -> fa",
      tb.acquisition_path("p1", {"p1": _NOW - 10 * 86400 * 1000}, 2, _NOW)
      == "fa")
check("acquisition_path: exactly at the 2-day boundary -> fa (lock expired)",
      tb.acquisition_path("p1", {"p1": _NOW - 2 * 86400 * 1000}, 2, _NOW)
      == "fa")
check("acquisition_path: never dropped -> fa",
      tb.acquisition_path("p9", {}, 2, _NOW) == "fa")
check("acquisition_path: bad waiver_clear_days falls back to 2",
      tb.acquisition_path("p1", {"p1": _NOW - 1 * 86400 * 1000},
                          None, _NOW) == "claim")
check("clear_label: waiver_day_of_week 1 -> Tuesday midnight (verified)",
      tb.clear_label({"waiver_day_of_week": 1}) == "clears Tue 12:00am PT")
check("clear_label: unverified day is flagged, not asserted",
      "unverified" in tb.clear_label({"waiver_day_of_week": 3}))

# Sleeper double-lists IR occupants in `players`: active must subtract
# the reserve overlap (the 9/23 Pacheco case: 17 listed, 1 in reserve,
# 16 real roster slots).
_R = {"players": ["a", "b", "c", "8205"], "reserve": ["8205"]}
_sm = tb.slot_math(_R, ["QB", "RB", "BN"], 1)
check("slot_math: active excludes reserve overlap",
      _sm["active"] == 3 and _sm["reserve_ids"] == ["8205"],
      f"got {_sm}")
check("slot_math: full roster -> every ADD needs a DROP",
      _sm["bench_max"] == 3 and _sm["drop_needed"] is True)
check("slot_math: open bench -> no drop required",
      (lambda s: s["active"] == 2 and s["drop_needed"] is False)(
          tb.slot_math({"players": ["a", "b"], "reserve": []},
                       ["QB", "RB", "BN"], 1)))
check("slot_math: IR capacity from reserve_slots",
      tb.slot_math(_R, ["QB", "RB", "BN"], 1)["ir_open"] == 0
      and tb.slot_math({"players": ["a"], "reserve": []},
                       ["QB"], 1)["ir_open"] == 1)
check("slot_math: None roster degrades to zeros, no crash",
      tb.slot_math(None, ["QB"], 1)["active"] == 0)

check("ir_move_valid: player already in reserve -> no-op",
      tb.ir_move_valid("8205", ["8205"], 0)
      == (False, "already on IR — the move is a no-op"))
check("ir_move_valid: IR full -> costs an active spot",
      tb.ir_move_valid("999", ["8205"], 0)[0] is False
      and "costs an active roster spot" in tb.ir_move_valid("999",
                                                            ["8205"], 0)[1])
check("ir_move_valid: open slot -> ok",
      tb.ir_move_valid("999", [], 1)[0] is True)
check("ir_move_valid: int pid matches str reserve entry",
      tb.ir_move_valid(8205, ["8205"], 1)[0] is False)

# ---------------- E14 follow-up: FULL roster drops must be active -------
_bench = [{"id": "8205", "name": "Isiah Pacheco", "pos": "RB", "val": 0},
          {"id": "9221", "name": "Jahmyr Gibbs", "pos": "RB", "val": 8500},
          {"id": "6819", "name": "Michael Pittman", "pos": "WR", "val": 402}]
check("legal_drops: FULL roster excludes reserve occupants",
      [b["name"] for b in tb.legal_drops(_bench, ["8205"],
                                         drop_needed=True)]
      == ["Jahmyr Gibbs", "Michael Pittman"])
check("legal_drops: FULL roster with int reserve ids also excluded",
      tb.legal_drops(_bench, [8205], drop_needed=True)[0]["name"]
      != "Isiah Pacheco")
check("legal_drops: open bench allows reserve occupants as drops",
      len(tb.legal_drops(_bench, ["8205"], drop_needed=False)) == 3)
check("legal_drops: FULL roster, nothing in reserve -> unchanged",
      tb.legal_drops(_bench, [], drop_needed=True) == _bench)
check("legal_drops: FULL roster, all bench in reserve -> no legal drop",
      tb.legal_drops([_bench[0]], ["8205"], drop_needed=True) == [])

# ---------------- NF-01: waiver priority cost model ----------------
_fake_rosters = [
    {"owner_id": "aaa", "roster_id": 1,
     "settings": {"waiver_position": 2}},
    {"owner_id": "bbb", "roster_id": 2,
     "settings": {"waiver_position": 1}},
    {"owner_id": "ccc", "roster_id": 3, "settings": {}},
]
check("NF-01: waiver_position read from settings (verified field)",
      tb.waiver_slot(_fake_rosters, "bbb") == 1
      and tb.waiver_slot(_fake_rosters, "aaa") == 2)
check("NF-01: waiver_position absent -> roster_id display-order fallback",
      tb.waiver_slot(_fake_rosters, "ccc") == 3)
check("NF-01: unknown owner -> None",
      tb.waiver_slot(_fake_rosters, "zzz") is None)

check("NF-01: slot scarcity #1 of 4 = 1.0, #7 of 14 ~= 0.571",
      tb.slot_scarcity(1, 4) == 1.0
      and abs(tb.slot_scarcity(7, 14) - 0.571) < 0.01)
check("NF-01: wire richness: 4-team=1.0, 8-team=0.6, 14-team=0.3",
      tb.wire_richness(4) == 1.0 and tb.wire_richness(8) == 0.6
      and tb.wire_richness(14) == 0.3)
check("NF-01: hold value = P x typical edge",
      (lambda p, hv: abs(p - 0.6) < 1e-9 and abs(hv - 60.0) < 1e-9)(
          *tb.hold_value(1, 4, 100.0)))

thin = tb.slot_verdict(2, 10, 1, 4)  # 20% edge on the #1 slot
check("NF-01: thin edge on top-3 slot flagged HOLD",
      thin is not None and "BURNS #1" in thin and "HOLD" in thin,
      f"got {thin}")
check("NF-01: big edge on top-3 slot not flagged",
      tb.slot_verdict(9, 10, 1, 4) is None)  # 90% upgrade clears the bar
check("NF-01: thin edge on non-scarce slot (#7 of 14) not flagged",
      tb.slot_verdict(2, 10, 7, 14) is None)
check("NF-01: forced replacement (hurt starter) never flagged",
      tb.slot_verdict(0, 10, 1, 4, forced=True) is None)
check("NF-01: scarce cutoff is top-3",
      tb.SCARCE_SLOT_CUTOFF == 3
      and tb.slot_verdict(2, 10, 3, 12) is not None
      and tb.slot_verdict(2, 10, 4, 12) is None)

real = tb.load_pending_offers(os.path.join(SKILL, "pending_offers.md"))
check("pending_offers.md: registry parses; on-block set is dict rows only",
      isinstance(real, dict)
      and all(isinstance(v, dict) and "partner" in v for v in real.values()),
      f"got {real}")

# ---------------- G1-G10 ----------------
print("== G1-G10 unit ==")

# --- G1: usage gaps ---
_U = {
    ("josh downs", "IND", "WR"): {"name": "Josh Downs", "pos": "WR",
        "team": "IND", "games": 2, "targets_pg": 9.0, "target_share": 0.28,
        "air_yards_share": 0.30, "wopr": 0.63, "carries_pg": 0.5,
        "tds_pg": 0.0, "ppg": 8.0, "offense_pct": 0.85, "racr": 0.60},
    ("td merchant", "KC", "WR"): {"name": "TD Merchant", "pos": "WR",
        "team": "KC", "games": 2, "targets_pg": 3.0, "target_share": 0.12,
        "air_yards_share": 0.10, "wopr": 0.25, "carries_pg": 0.2,
        "tds_pg": 1.5, "ppg": 16.0, "offense_pct": 0.50, "racr": 1.40},
    ("one gamer", "BUF", "RB"): {"name": "One Gamer", "pos": "RB",
        "team": "BUF", "games": 1, "targets_pg": 8.0, "target_share": 0.25,
        "air_yards_share": 0.20, "wopr": 0.60, "carries_pg": 2.0,
        "tds_pg": 0.0, "ppg": 4.0, "offense_pct": 0.80, "racr": 0.50},
}
_buys, _sells = fi.usage_gaps(_U)
check("G1: buy-low on elite usage + poor output",
      any(b["name"] == "Josh Downs" for b in _buys))
check("G1: sell-high on TD-inflated thin usage",
      any(s["name"] == "TD Merchant" for s in _sells))
check("G1: min-games gate keeps the 1-game sample out",
      not any(b["name"] == "One Gamer" for b in _buys))
check("G1: yards-behind-air-yards note present",
      any("yards lag air yards" in b["note"] for b in _buys))
check("G1: expected output rises with usage",
      fi.expected_ppg({"pos": "WR", "wopr": 0.6})
      > fi.expected_ppg({"pos": "WR", "wopr": 0.2}))
check("G1: transparent ppr_points formula",
      abs(fi.ppr_points({"passing_yards": 250, "passing_tds": 2,
                         "receptions": 5, "receiving_yards": 50,
                         "receiving_tds": 1,
                         "rushing_fumbles_lost": 1}) - 32.0) < 0.01)

# --- G2: trade profiles ---
_TRADES = [
    {"round": 2,
     "adds": {"p1": "10", "p2": "20"}, "drops": {"p1": "20", "p2": "10"}},
    {"round": 7, "adds": {"p3": "10"}, "drops": {"p3": "30"}},
]
_PPOS = {"p1": "QB", "p2": "WR", "p3": "RB"}.get
_PVAL = {"p1": 5000, "p2": 4000, "p3": 3000}.get
_NAMES = {"10": "MgrA", "20": "MgrB", "30": "MgrC"}
_PROFS = fi.trade_profiles(_TRADES, _PPOS, _PVAL, _NAMES)
check("G2: per-manager trade counts",
      _PROFS["10"]["n_trades"] == 2 and _PROFS["20"]["n_trades"] == 1)
check("G2: acquired/sent positions mined",
      _PROFS["10"]["acquired_pos"].get("QB") == 1
      and _PROFS["10"]["sent_pos"].get("WR") == 1)
check("G2: first/last trade timing",
      _PROFS["10"]["first_round"] == 2 and _PROFS["10"]["last_round"] == 7)
check("G2: net FantasyCalc value signed correctly",
      _PROFS["10"]["net_value"] == 4000
      and _PROFS["20"]["net_value"] == -1000)
check("G2: profile_line names the manager and a position",
      "MgrA" in fi.profile_line(_PROFS["10"])
      and "QB" in fi.profile_line(_PROFS["10"]))

# --- G3: trending velocity (ONE call per scan + snapshot history) ---
_PL3 = {"1": {"full_name": "Hot Hand", "position": "RB"},
        "2": {"full_name": "Cold Case", "position": "WR"},
        "3": {"full_name": "Rostered Guy", "position": "TE"}}
_NOW3 = time.time()
_PREV3 = {"ts": _NOW3 - 24 * 3600, "counts": {"1": 2400, "2": 4800}}
_CUR3 = {"1": 4800, "2": 600, "3": 9000}
_V3 = fi.trending_velocity(_CUR3, _PREV3, _PL3, {"3"})
check("G3: accelerating adds tag HEATING",
      any(r["sid"] == "1" and r["tag"] == "HEATING" for r in _V3))
check("G3: decelerating adds tag COOLING",
      any(r["sid"] == "2" and r["tag"] == "COOLING" for r in _V3))
check("G3: rostered players excluded",
      all(r["sid"] != "3" for r in _V3))
check("G3: velocity sorted by acceleration",
      _V3[0]["accel"] >= _V3[1]["accel"])
check("G3: accel = 24h rate vs previous-gap rate",
      any(r["sid"] == "1" and r["accel"] == 2.0 for r in _V3),
      f"got {[ (r['sid'], r['accel']) for r in _V3 ]}")
_V3F = fi.trending_velocity(
    {"1": 4800}, {"ts": _NOW3 - 600, "counts": {"1": 4800}},
    {"1": {"full_name": "Fresh Base", "position": "RB"}}, set())
check("G3: baseline less than an hour old -> NEW (too overlapping)",
      _V3F[0]["tag"] == "NEW" and _V3F[0]["accel"] is None,
      f"got {_V3F[0]}")
_V3Z = fi.trending_velocity(
    {"1": 0}, _PREV3,
    {"1": {"full_name": "Dried Up", "position": "RB"}}, set())
check("G3: adds dried up to zero -> COOLING at 0.0",
      _V3Z[0]["tag"] == "COOLING" and _V3Z[0]["accel"] == 0.0,
      f"got {_V3Z[0]}")
_V3N = fi.trending_velocity(
    {"9": 2400}, None,
    {"9": {"full_name": "New Guy", "position": "RB"}}, set())
check("G3: no prior snapshot -> NEW, never faked",
      _V3N[0]["tag"] == "NEW" and _V3N[0]["accel"] is None)
_V3N2 = fi.trending_velocity(
    {"9": 2400}, _PREV3,
    {"9": {"full_name": "New Guy", "position": "RB"}}, set())
check("G3: absent from previous snapshot -> NEW, never faked",
      _V3N2[0]["tag"] == "NEW" and _V3N2[0]["accel"] is None)

# one-call-per-scan regression: count Sleeper calls through a scan
_CALLS3 = []
_ORIG_FETCH3 = fi.fetch_json


def _counting_fetch3(url, *a, **k):
    _CALLS3.append(url)
    if "trending" in url:
        return [{"player_id": "1", "count": 4800}]
    return _ORIG_FETCH3(url, *a, **k)


fi.fetch_json = _counting_fetch3
try:
    with tempfile.TemporaryDirectory() as _td3:
        _SP3 = os.path.join(_td3, "snaps.json")
        _rows3 = fi.fetch_trending()
        _counts3 = {str(t.get("player_id")): t.get("count") or 0
                    for t in _rows3}
        fi.save_trend_snapshot(_counts3, ts=_NOW3 - 24 * 3600, path=_SP3)
        _snaps3 = fi.load_trend_snapshots(path=_SP3)
        _v3b = fi.trending_velocity(_counts3, _snaps3[-1], _PL3, set())
finally:
    fi.fetch_json = _ORIG_FETCH3
check("G3: exactly one Sleeper call per scan",
      len(_CALLS3) == 1 and "lookback_hours=24" in _CALLS3[0],
      f"calls={_CALLS3}")
check("G3: snapshot round-trips through the history file",
      len(_snaps3) == 1 and _snaps3[0]["counts"] == {"1": 4800},
      f"got {_snaps3}")
check("G3: second scan derives velocity from snapshot history",
      _v3b[0]["sid"] == "1" and _v3b[0]["accel"] == 1.0
      and _v3b[0]["tag"] == "STEADY",
      f"got {_v3b[0] if _v3b else None}")

# --- G4: playoff simulation ---
_REC4 = {"a": (2, 0, 0), "b": (1, 1, 0), "c": (0, 2, 0), "d": (1, 1, 0)}
_SCH4 = {"a": ["b", "c"], "b": ["a", "d"],
         "c": ["a", "d"], "d": ["b", "c"]}
_P4A = fi.simulate_playoffs(_REC4, _SCH4, 2, sims=500, seed=7)
_P4B = fi.simulate_playoffs(_REC4, _SCH4, 2, sims=500, seed=7)
check("G4: simulation is deterministic under a seed", _P4A == _P4B)
check("G4: best record gets the highest probability",
      _P4A["a"][0] > _P4A["d"][0] > _P4A["c"][0])
check("G4: probabilities bounded in [0,1]",
      all(0.0 <= v[0] <= 1.0 for v in _P4A.values()))
check("G4: posture thresholds",
      fi.posture_for(0.7).startswith("CONTENDER")
      and fi.posture_for(0.4).startswith("BUBBLE")
      and fi.posture_for(0.1).startswith("LONGSHOT"))

# --- G4: PF/G double-division regression (the real bug class) ---
_MW4 = {1: [{"roster_id": 1, "points": 200.0},
            {"roster_id": 2, "points": 180.0}],
        2: [{"roster_id": 1, "points": 100.0},
            {"roster_id": 2, "points": 120.0}],
        3: [{"roster_id": 1, "matchup_id": 1},
            {"roster_id": 2, "matchup_id": 1}]}
_ORIG_FETCH4 = fi.fetch_json


def _fake_fetch4(url, *a, **k):
    m = re.search(r"/matchups/(\d+)$", url)
    if m:
        return _MW4.get(int(m.group(1)), [])
    return _ORIG_FETCH4(url, *a, **k)


fi.fetch_json = _fake_fetch4
try:
    _SCHED4, _PFPG4 = fi.fetch_remaining_schedule("L", 3)
finally:
    fi.fetch_json = _ORIG_FETCH4
check("G4: fetch_remaining_schedule returns points PER GAME",
      _PFPG4.get("1") == 150.0 and _PFPG4.get("2") == 150.0,
      f"got {_PFPG4}")
check("G4: remaining schedule pairs the future matchups",
      _SCHED4.get("1") == ["2"] and _SCHED4.get("2") == ["1"],
      f"got {_SCHED4}")

# CLI-level: cmd_playoff_odds must NOT divide pfpg again. Capture what the
# simulator receives; the old bug halved it (100 -> 50 at week 3).
_CAP4 = {}
_ORIG_FRS4 = fi.fetch_remaining_schedule
_ORIG_SIM4 = fi.simulate_playoffs
_ORIG_FETCH4B = fi.fetch_json


def _fake_fetch4b(url, *a, **k):
    if url.endswith("/state/nfl"):
        return {"week": 3}
    if re.search(r"/league/L$", url):
        return {"name": "T",
                "settings": {"playoff_week_start": 15, "playoff_teams": 2}}
    if url.endswith("/rosters"):
        return [{"roster_id": 1, "owner_id": "me",
                 "settings": {"wins": 2, "losses": 0, "ties": 0}}]
    if url.endswith("/users"):
        return [{"user_id": "me", "display_name": "Wesley"}]
    if "/matchups/" in url:
        return []
    return _ORIG_FETCH4B(url, *a, **k)


def _fake_frs4(lid, wk, last_week=18):
    return {}, {"1": 100.0}


def _fake_sim4(records, sched, pteams, pfpg=None, sims=20000, seed=42):
    _CAP4["pfpg"] = dict(pfpg or {})
    return {"1": (0.5, 9.0)}


fi.fetch_json = _fake_fetch4b
fi.fetch_remaining_schedule = _fake_frs4
fi.simulate_playoffs = _fake_sim4
try:
    with tempfile.TemporaryDirectory() as _td4:
        _a4 = type("A", (), {"league": ["L"], "me": "me",
                             "out": os.path.join(_td4, "odds.md")})()
        fi.cmd_playoff_odds(_a4)
        _OUT4 = open(_a4.out).read()
finally:
    fi.fetch_json = _ORIG_FETCH4B
    fi.fetch_remaining_schedule = _ORIG_FRS4
    fi.simulate_playoffs = _ORIG_SIM4
check("G4: playoff CLI passes PF/G through undivided (no second /weeks)",
      _CAP4.get("pfpg") == {"1": 100.0},
      f"simulator got pfpg={_CAP4.get('pfpg')}")
check("G4: playoff CLI prints the true PF/G in the table",
      "| 100.0 |" in _OUT4,
      f"table line: {[l for l in _OUT4.splitlines() if 'Wesley' in l][:2]}")

# --- G5: playoff-schedule weighting ---
with tempfile.NamedTemporaryFile("w", suffix=".csv",
                                 delete=False) as _fh:
    _fh.write("season,season_type,week,away_team,home_team\n"
              "2026,REG,15,KC,DEN\n2026,REG,16,KC,JAX\n2026,REG,17,KC,TEN\n"
              "2026,REG,15,BUF,NE\n2026,REG,16,BUF,PIT\n2026,REG,17,BUF,BAL\n")
    _GF = _fh.name
with tempfile.NamedTemporaryFile("w", suffix=".csv",
                                 delete=False) as _fh:
    _fh.write("season_type,position,opponent_team,game_id,"
              "rushing_yards,rushing_tds\n"
              "REG,RB,DEN,g1,150,2\nREG,RB,JAX,g2,130,2\n"
              "REG,RB,TEN,g3,110,2\nREG,RB,NE,g4,30,0\n"
              "REG,RB,PIT,g5,50,0\nREG,RB,BAL,g6,60,0\n")
    _SF = _fh.name
_PM = fi.playoff_multipliers(
    _GF, _SF,
    {("soft back", "KC", "RB"): ("KC", "RB"),
     ("tough back", "BUF", "RB"): ("BUF", "RB")},
    (15, 16, 17))
os.unlink(_GF)
os.unlink(_SF)
check("G5: soft playoff path boosts value (>1.0)",
      _PM[("soft back", "KC", "RB")][0] > 1.0)
check("G5: brutal playoff path discounts value (<1.0)",
      _PM[("tough back", "BUF", "RB")][0] < 1.0)
check("G5: multipliers clamped to 0.90-1.10",
      all(0.90 <= v[0] <= 1.10 for v in _PM.values()))
check("G5: tags label the schedule",
      _PM[("soft back", "KC", "RB")][1] == "[P+ soft]"
      and _PM[("tough back", "BUF", "RB")][1] == "[P- brutal]")
with tempfile.NamedTemporaryFile("w", suffix=".csv",
                                 delete=False) as _fh:
    # A genuine bye: W16 HAS games, KC just isn't in one. (A file with no
    # W16 rows at all is an unsupported week, not a bye.)
    _fh.write("season,season_type,week,away_team,home_team\n"
              "2026,REG,15,KC,DEN\n2026,REG,17,KC,TEN\n"
              "2026,REG,16,DEN,JAX\n2026,REG,16,BUF,NE\n")
    _GB = _fh.name
_PB = fi.playoff_multipliers(
    _GB, _GB, {("bye back", "KC", "RB"): ("KC", "RB")}, (15, 16, 17))
os.unlink(_GB)
check("G5: playoff bye flattens the multiplier",
      _PB[("bye back", "KC", "RB")] == (0.85, "[PLAYOFF BYE W16]"))

# --- G5: team universe + games.csv column layout (the real bug class) ---
# The live games.csv uses `game_type`, not `season_type` — the reader
# must see the schedule in the real file, not just the old fixtures.
with tempfile.NamedTemporaryFile("w", suffix=".csv",
                                 delete=False) as _fh:
    _fh.write("season,game_type,week,away_team,home_team\n"
              "2026,REG,15,KC,DEN\n2026,REG,16,KC,JAX\n2026,REG,17,KC,TEN\n")
    _GG5 = _fh.name
_OPPS5, _MISS5 = fi.load_playoff_opponents(_GG5, (15, 16, 17))
os.unlink(_GG5)
check("G5: reads the live games.csv game_type column",
      "KC" in _OPPS5 and any(w == 15 and o == "DEN"
                             for w, o in _OPPS5["KC"]),
      f"KC rows: {_OPPS5.get('KC')}")
check("G5: fully-covered weeks report nothing missing",
      _MISS5 == [], f"got {_MISS5}")
check("G5: team universe is the fixed 32-team NFL set",
      len(fi.NFL_TEAMS) == 32 and "KC" in fi.NFL_TEAMS)
check("G5: team with no rows is idle (bye), not a crash",
      _OPPS5.get("ARI") and all(o is None for _, o in _OPPS5["ARI"]),
      f"ARI rows: {_OPPS5.get('ARI')}")

# A playoff week with NO schedule rows is unsupported — it must not
# mint 32 phantom byes.
with tempfile.NamedTemporaryFile("w", suffix=".csv",
                                 delete=False) as _fh:
    _fh.write("season,game_type,week,away_team,home_team\n"
              "2026,REG,15,KC,DEN\n2026,REG,17,KC,TEN\n")
    _GM5 = _fh.name
_OM5, _MISSM5 = fi.load_playoff_opponents(_GM5, (15, 16, 17))
os.unlink(_GM5)
check("G5: week with no rows is unsupported, not 32 phantom byes",
      _MISSM5 == [16]
      and not any(o is None for w, o in _OM5.get("KC", []) if w == 16)
      and not any(o is None for w, o in _OM5.get("ARI", []) if w == 16),
      f"missing={_MISSM5} KC={_OM5.get('KC')}")
with tempfile.NamedTemporaryFile("w", suffix=".csv",
                                 delete=False) as _fh:
    _fh.write("season,game_type,week,away_team,home_team\n"
              "2026,REG,15,KC,DEN\n2026,REG,17,KC,TEN\n")
    _GMW5 = _fh.name
with tempfile.NamedTemporaryFile("w", suffix=".csv",
                                 delete=False) as _fh:
    _fh.write("season_type,position,opponent_team,game_id,"
              "rushing_yards,rushing_tds\n"
              "REG,RB,DEN,g1,150,2\nREG,RB,TEN,g3,110,2\n")
    _SMW5 = _fh.name
_PMW5 = fi.playoff_multipliers(
    _GMW5, _SMW5, {("bye back", "KC", "RB"): ("KC", "RB")}, (15, 16, 17))
os.unlink(_GMW5)
os.unlink(_SMW5)
check("G5: unsupported playoff week -> marked neutral, never faked",
      _PMW5[("bye back", "KC", "RB")][0] == 1.0
      and "unavailable" in _PMW5[("bye back", "KC", "RB")][1]
      and "W16" in _PMW5[("bye back", "KC", "RB")][1],
      f"got {_PMW5[('bye back', 'KC', 'RB')]}")

# Multi-season file isolation: rows from other seasons must not blend
# into this season's playoff slate (the cached games.csv spans 1999+).
with tempfile.NamedTemporaryFile("w", suffix=".csv",
                                 delete=False) as _fh:
    _fh.write("season,game_type,week,away_team,home_team\n"
              "2025,REG,15,KC,DEN\n"
              "2026,REG,15,KC,JAX\n")
    _GS5 = _fh.name
_OS5, _MS5 = fi.load_playoff_opponents(_GS5, (15,))
os.unlink(_GS5)
check("G5: only the requested season's slate is used",
      _OS5.get("KC") == [(15, "JAX")] and _MS5 == [],
      f"KC rows: {_OS5.get('KC')}, missing={_MS5}")

# --- G6: handcuff map ---
with tempfile.NamedTemporaryFile("w", suffix=".csv",
                                 delete=False) as _fh:
    _fh.write("dt,team,player_name,gsis_id,pos_abb,pos_rank\n"
              "2026-09-01T00:00:00Z,KC,Starter Back,gsis1,RB,1\n"
              "2026-09-21T00:00:00Z,KC,Starter Back,gsis1,RB,1\n"
              "2026-09-01T00:00:00Z,KC,Ex Backup,gsis2,RB,3\n"
              "2026-09-21T00:00:00Z,KC,Backup Back,gsis2,RB,2\n")
    _DC = _fh.name
_DEP = fi.parse_depth_charts(_DC)
os.unlink(_DC)
check("G6: latest snapshot wins for the same player",
      [n for _, n, _ in _DEP["KC"]["RB"]]
      == ["Starter Back", "Backup Back"])
_PL6 = {"s1": {"full_name": "Starter Back", "position": "RB",
               "team": "KC", "gsis_id": "gsis1"},
        "s2": {"full_name": "Backup Back", "position": "RB",
               "team": "KC", "gsis_id": "gsis2"},
        "s3": {"full_name": "Other Guy", "position": "RB",
               "team": "KC", "gsis_id": "gsis9"}}
_HM = fi.handcuff_map(_DEP, ["s1"], {"9": ["s1"]}, _PL6,
                      {"9": "Me", "10": "Opp"}, "9")
check("G6: lead RB with a free backup flagged",
      _HM[0]["lead"] is True
      and _HM[0]["backups"][0]["status"] == "free")
_HM2 = fi.handcuff_map(_DEP, ["s1"], {"9": ["s1"], "10": ["s2"]}, _PL6,
                       {"9": "Me", "10": "Opp"}, "9")
check("G6: backup held by an opponent labeled",
      _HM2[0]["backups"][0]["status"] == "opp:Opp")
_HM3 = fi.handcuff_map(_DEP, ["s3"], {"9": ["s1", "s3"]}, _PL6,
                       {"9": "Me"}, "9")
check("G6: my RB who is not the lead is flagged",
      _HM3[0]["lead"] is False)

# --- G6: UNPROTECTED flag logic (E8: free backup -> uninsured starter) ---
_HR = fi.render_handcuff_lines([
    {"starter": "Lead Back", "team": "KC", "lead": True,
     "backups": [{"sid": "s2", "name": "Backup Back", "status": "free"}]},
    {"starter": "Covered Back", "team": "SF", "lead": True,
     "backups": [{"sid": "s3", "name": "Held Back", "status": "opp:Opp"}]},
    {"starter": "My Cuff Back", "team": "DET", "lead": True,
     "backups": [{"sid": "s4", "name": "Own Back", "status": "mine"}]},
    {"starter": "Mystery Back", "team": "BUF", "lead": True,
     "backups": [{"sid": None, "name": "Ghost", "status": "unknown"}]},
    {"starter": "Lone Back", "team": "NE", "lead": True, "backups": []},
    {"starter": "Committee Back", "team": "BAL", "lead": False,
     "note": "not the lead back (rank 3)"},
])
check("G6: free backup -> UNPROTECTED flag (uninsured starter)",
      any("Lead Back" in ln and "<-- UNPROTECTED" in ln for ln in _HR),
      f"got {_HR}")
check("G6: opp-held / mine / unknown backups never flag",
      not any("<-- UNPROTECTED" in ln for ln in _HR
              if "Covered Back" in ln or "My Cuff Back" in ln
              or "Mystery Back" in ln),
      f"got {_HR}")
check("G6: no listed backup prints the stub, no flag",
      any("Lone Back" in ln and "(no listed backup)" in ln
          and "<-- UNPROTECTED" not in ln for ln in _HR),
      f"got {_HR}")
check("G6: non-lead prints the note line",
      any("Committee Back: not the lead back (rank 3)" in ln
          for ln in _HR),
      f"got {_HR}")
check("G6: co-backup tie with one free still flags",
      any("<-- UNPROTECTED" in ln for ln in fi.render_handcuff_lines([
          {"starter": "Tie Back", "team": "KC", "lead": True,
           "backups": [{"sid": "a", "name": "A", "status": "opp:Opp"},
                       {"sid": "b", "name": "B", "status": "free"}]}])))

# --- G7: schedule luck ---
_LUCK = fi.schedule_luck(
    {1: [("a", 120), ("b", 100), ("c", 90), ("d", 110)],
     2: [("a", 95), ("b", 115), ("c", 105), ("d", 85)]},
    {"a": (2, 0, 0), "b": (1, 1, 0), "c": (0, 2, 0), "d": (1, 1, 0)})
check("G7: all-play expected wins summed over weeks (0-2 scale)",
      abs(_LUCK["a"]["expected"] - 1.33) < 0.01
      and abs(_LUCK["c"]["expected"] - 0.67) < 0.01)
check("G7: wins, expected and weeks all reported",
      set(_LUCK["a"]) == {"wins", "expected", "weeks"}
      and _LUCK["a"]["wins"] == 2)

# --- G8: roster clog audit ---
_R8, _D8 = fi.clog_audit(
    [{"sid": "1", "name": "Clogger", "pos": "WR", "val": 1000, "inj": ""},
     {"sid": "2", "name": "Cuff", "pos": "RB", "val": 1000, "inj": ""},
     {"sid": "3", "name": "Blocked", "pos": "TE", "val": 10, "inj": ""}],
    handcuff_of={"2": True}, onblock={"3"})
check("G8: contingent-value handcuff outranks the clogger",
      _R8[-1]["name"] == "Cuff" and _R8[0]["name"] == "Blocked")
check("G8: drops name the clogger, never on-block or handcuffs",
      _D8 == ["Clogger"])

# --- G9: bye craters ---
_CR = fi.bye_craters({"1": 7, "2": 7, "3": 8}, ["1", "2", "3"],
                      ["1", "2"], 6)
check("G9: 2+ starters on bye flags a crater",
      _CR[7]["crater"] is True and _CR[7]["total"] == 2)
check("G9: no starters on bye is not a crater",
      _CR[8]["crater"] is False)
check("G9: forecast window is the next 4 weeks",
      sorted(_CR) == [7, 8, 9, 10])

# --- G10: betting odds ---
_ITEM = {"provider": {"name": "Draft Kings"}, "details": "GB -6",
         "overUnder": 44.5, "spread": -6.0,
         "homeTeamOdds": {"favorite": True, "moneyLine": -290},
         "awayTeamOdds": {"favorite": False, "moneyLine": 235},
         "open": {"total": {"alternateDisplayValue": "46.5"}}}
_G10 = fi.parse_odds_item(_ITEM, "ATL", "GB")
check("G10: DraftKings game odds parsed",
      _G10["fav"] == "GB" and _G10["line"] == 6.0
      and _G10["total"] == 44.5 and _G10["fav_impl"] == 25.2)
check("G10: unusable odds item returns None",
      fi.parse_odds_item({"provider": {"name": "X"}}, "A", "B") is None)
_SIG = fi.start_sit_signals([dict(_G10, line=7.5, total=49.0)],
                            {"p1": ("My RB", "RB", "GB")})
check("G10: big-favorite shootout signals fire",
      any("positive script" in s for s in _SIG)
      and any("shootout" in s for s in _SIG))
check("G10: no rostered pieces in a game means no signals",
      fi.start_sit_signals([_G10], {"p9": ("Other", "WR", "NE")}) == [])

# --- G10 extras: edge cases (synthetic, no network) ---
_PK = fi.parse_odds_item({"details": "PK", "overUnder": 40.0, "spread": 0.0,
                          "homeTeamOdds": {"favorite": False,
                                           "moneyLine": 100},
                          "awayTeamOdds": {"favorite": False,
                                           "moneyLine": -110}},
                         "NE", "BUF")
check("G10: PK details -> line 0.0, away team the favorite",
      _PK is not None and _PK["line"] == 0.0 and _PK["fav"] == "NE")
check("G10: non-dict odds item -> None",
      fi.parse_odds_item(None, "A", "B") is None)
_GAME = {"away": "ATL", "home": "GB", "fav": "GB", "dog": "ATL",
         "line": 8.0, "total": 38.0, "open_total": 44.5,
         "fav_impl": 23.0, "dog_impl": 15.0}
_SIG2 = fi.start_sit_signals([_GAME], {"p1": ("My WR", "WR", "ATL")})
check("G10: underdog pieces get the negative-script note",
      any("negative script" in s for s in _SIG2))
check("G10: grind total (<= 41.5) flags the fringe-FLEX fade",
      any("grind total" in s for s in _SIG2))
check("G10: total moved >= 2.0 from open prints the market-moved line",
      any("market moved total down" in s for s in _SIG2))
check("G10: cross-check line carries spread direction + implied totals",
      any("GB -8.0 O/U 38.0 (impl GB 23.0, ATL 15.0)" in s
          for s in _SIG2))

# --- G10 live: ESPN core API odds (guarded; SKIP offline, never fail) ---
try:
    _wk = int((fi.fetch_json(f"{fi.SLEEPER}/state/nfl") or {})
              .get("week") or 0)
    _gp = fi.nflverse_paths(keys=("games",))
    _priced, _errs = fi.fetch_week_odds(_gp["games"], _wk, season="2026")
    _sane = (len(_priced) >= 10 and all(
        all(k in g for k in ("away", "home", "fav", "dog", "line", "total",
                             "fav_impl", "dog_impl"))
        and "draft" in str(g.get("provider") or "").lower()
        for g in _priced))
    check(f"G10 live: ESPN core API priced {len(_priced)} games (wk {_wk})",
          _sane, f"unpriced: {_errs[:3]}")
except Exception as _e:  # noqa: BLE001 - degraded path: SKIP, not advice
    print(f"  SKIP G10 live fetch_week_odds (offline/degraded): {_e}")

# ---------------- integration ----------------
print("== integration ==")


def run_board(league):
    p = subprocess.run(
        [sys.executable, os.path.join(SKILL, "bin", "trade_board.py"),
         "--league", league, "--me", ME],
        capture_output=True, text=True, timeout=240)
    return p


for league, tag in ((SNAPUSA, "snapusa"), (WW, "weekend-warriors")):
    p = run_board(league)
    out = p.stdout
    check(f"{tag}: exit 0", p.returncode == 0, p.stderr[-500:])
    check(f"{tag}: 2-flex model (ADV-FF-09)", "(+2 flex)" in out)
    check(f"{tag}: K waiver line (ADV-FF-06)", "top FA K:" in out)
    check(f"{tag}: DEF waiver line (ADV-FF-06)", "top FA DEF:" in out)
    check(f"{tag}: swaps sorted by delta (ADV-FF-15)",
          "=== CANDIDATE SWAPS (sorted by lineup-points delta) ===" in out)
    check(f"{tag}: trade-lock boundary line (ADV-FF-14)",
          "Trade lock: trades legal through NFL Week" in out)
    for hdr in ("=== LEAGUE TRADE HISTORY (market comps) ===",
                "=== MANAGER TRADE PROFILES (G2",
                "=== TEAM NEEDS (startable vs effective slots) ===",
                "=== PLAYOFF ODDS + SCHEDULE LUCK (G4/G7",
                "=== YOUR ROSTER (by value) ===",
                "=== BYE-CRATER FORECAST (G9",
                "=== USAGE-GAP RADAR (G1",
                "=== HOLE-CREATING OPTIONS (persona must price the roster cost) ===",
                "=== HANDCUFF LEVERAGE MAP (G6",
                "=== WAIVER WIRE ===",
                "=== WAIVER PRIORITY COST ==="):
        check(f"{tag}: header stable: {hdr[:30]}...", hdr in out)
    check(f"{tag}: hold-vs-spend line present",
          "hold-vs-spend" in out and "P(better target emerges" in out)
    check(f"{tag}: G3 trending velocity line",
          "trending velocity (Sleeper-wide adds" in out)
    check(f"{tag}: G1 routes-unavailable label",
          "routes are unavailable" in out)
    check(f"{tag}: G8 roster-clog audit line",
          "roster-clog audit (G8" in out)
    check(f"{tag}: IR audit line with capacity (E13)",
          re.search(r"  IR: .+ \(\d+/\d+ used, \d+ open\)", out) is not None)
    # ADV-FF-18/E14: every suggested ADD carries an acquisition-path tag,
    # and a slot-check line validates live roster math before emitting.
    check(f"{tag}: slot-check line with live roster math (E14)",
          re.search(r"  slot check: active \d+/\d+ — "
                    r"(FULL: every ADD needs a DROP|open bench slot\(s\))",
                    out) is not None)
    _moves = [ln for ln in out.splitlines()
              if ln.startswith("  ADD ") and " / DROP " in ln]
    # A league can legitimately suggest no moves (rich wire, high churn
    # bar) — then the tag rule is vacuously satisfied.
    _untagged = [ln for ln in _moves
                 if not re.search(r"\[(FA NOW|CLAIM) —", ln)]
    check(f"{tag}: every suggested move has an FA NOW or CLAIM tag (ADV-FF-18)",
          not _untagged, f"untagged moves: {_untagged}")
    check(f"{tag}: CLAIM tag names clear time and slot (ADV-FF-18)",
          not any("[CLAIM" in ln for ln in _moves)
          or all("clears Tue 12:00am PT" in ln and "burns #" in ln
                 for ln in _moves if "[CLAIM" in ln))
    # E14 follow-up: on a FULL roster the DROP in every suggested move must
    # be an active player — dropping an IR/reserve occupant frees no active
    # bench slot, so the ADD would have nowhere to go.
    _full = re.search(r"  slot check: active \d+/\d+ — FULL: every ADD needs a DROP"
                      r" \| IR: ([^\(]+) \(\d+/\d+ used", out)
    _res = set()
    if _full and _full.group(1).strip() != "—":
        _res = {n.strip() for n in _full.group(1).split(",")}
    _bad_drops = [ln for ln in _moves
                  if _res and re.search(r" / DROP ([^(]+) \(",
                                        ln).group(1).strip() in _res]
    check(f"{tag}: no suggested DROP is an IR/reserve occupant on a FULL roster (E14)",
          not _bad_drops, f"illegal drops: {_bad_drops}")
    # E6: fairness bands — legend in the swaps header, a band on every
    # swap gap tag, and the league's largest accepted gap as the live
    # calibration in the trade history section.
    check(f"{tag}: fairness band legend (E6)",
          "10-20% FAIR" in out and "20-35% STRETCH" in out)
    _gaps = [ln for ln in out.splitlines()
             if re.search(r"\[gap (vs combined )?\d+%", ln)]
    _unbanded = [ln for ln in _gaps
                 if not re.search(r"\|(EXCELLENT|FAIR|STRETCH|UNFAIR)\]", ln)]
    check(f"{tag}: every swap gap carries a fairness band (E6)",
          not _unbanded, f"unbanded: {_unbanded}")
    check(f"{tag}: league calibration line or no-trades (E6)",
          ("league calibration: largest accepted gap this season" in out)
          or ("no completed trades yet" in out))
    # E7: every swap row prints a value range next to the point value
    # (vacuous when a league legitimately has no fits this run).
    _swaps = [ln for ln in out.splitlines()
              if ln.startswith("  you send ")]
    _ranged = [ln for ln in _swaps
               if re.search(r"\(\d+, \d+-\d+\)", ln)]
    check(f"{tag}: swap rows show value ranges (E7)",
          not _swaps or len(_ranged) == len(_swaps),
          f"rangeless rows: {[ln for ln in _swaps if ln not in _ranged]}")
    check(f"{tag}: value-range legend under swaps header (E7)",
          "value ranges: (point, lo-hi) from 30-day trend volatility" in out)


    # E8: the UNPROTECTED flag must fire exactly when a backup is free —
    # assert the live section's internal consistency (free <-> flagged).
    # (The snap-share fallback path prints no backup lines, so the rule
    # is vacuously satisfied there.)
    _hsec = out.split("=== HANDCUFF LEVERAGE MAP (G6")[1].split("===")[0]
    _hlines = [ln for ln in _hsec.splitlines()
               if ln.startswith("  ") and "backup" in ln]
    check(f"{tag}: every [free] backup line carries UNPROTECTED (E8)",
          all("<-- UNPROTECTED" in ln for ln in _hlines
              if "[free]" in ln),
          f"lines={_hlines}")
    check(f"{tag}: UNPROTECTED never fires without a free backup (E8)",
          all("[free]" in ln for ln in _hlines
              if "<-- UNPROTECTED" in ln),
          f"lines={_hlines}")

out = run_board(SNAPUSA).stdout
# ADV-FF-07: expectations derive from the live registry — the Pittman/Andrews
# and Shakir/Andrews offers were rejected 2026-09-22, so the registry is
# currently empty; if Wesley opens a new offer, the board must block it.
_onblock = tb.load_pending_offers(os.path.join(SKILL, "pending_offers.md"))
out = run_board(SNAPUSA).stdout
# ADV-FF-07: expectations derive from the live registry — the Pittman/Andrews
# and Shakir/Andrews offers were rejected 2026-09-22, so the registry is
# currently empty; if Wesley opens a new offer, the board must block it.
_onblock = tb.load_pending_offers(os.path.join(SKILL, "pending_offers.md"))
if _onblock:
    check("snapusa: PENDING OFFERS section (ADV-FF-07)",
          "=== PENDING OFFERS (your open offers" in out)
    for _pid, _info in _onblock.items():
        check(f"snapusa: {_info['name']} flagged ON-BLOCK (ADV-FF-07)",
              _info["name"] in out and "[ON-BLOCK" in out)
else:
    check("snapusa: registry empty -> no PENDING OFFERS section, "
          "no ON-BLOCK flags (ADV-FF-07)",
          "=== PENDING OFFERS" not in out and "[ON-BLOCK" not in out,
          "empty registry must not block any trades")
check("snapusa: posture line present", "| posture:" in out)

out_ww = run_board(WW).stdout
# NF-01: slot values reset weekly in Sleeper — assert the line's internal
# consistency (SCARCE iff slot <= 3) instead of a hardcoded slot number.
_m = re.search(r"snapusa waiver slot: (\d+) of 14( — SCARCE)?", out)
check("NF-01: snapusa slot line self-consistent",
      bool(_m) and (("— SCARCE" in _m.group(0))
                    == (int(_m.group(1)) <= tb.SCARCE_SLOT_CUTOFF)))
# E3: 2-for-1 consolidation is WW-only (4-team), never snapusa (14-team).
_m2 = re.search(r"weekend warriors waiver slot: (\d+) of 4( — SCARCE)?",
                out_ww)
check("NF-01: WW slot line self-consistent",
      bool(_m2) and (("— SCARCE" in _m2.group(0))
                     == (int(_m2.group(1)) <= tb.SCARCE_SLOT_CUTOFF)))
check("E3: WW prints the 2-for-1 consolidation section",
      "=== 2-FOR-1 CONSOLIDATION (stars over depth) ===" in out_ww,
      "WW output lacks the section")
out_snap = run_board(SNAPUSA).stdout
check("E3: snapusa never prints 2-for-1s (depth is currency there)",
      "=== 2-FOR-1 CONSOLIDATION" not in out_snap,
      "2-for-1 section leaked into the 14-team board")
check("NF-01: WW slot line self-consistent",
      bool(_m2) and (("— SCARCE" in _m2.group(0))
                     == (int(_m2.group(1)) <= tb.SCARCE_SLOT_CUTOFF)))

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("all golden tests passed")

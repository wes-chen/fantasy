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

SKILL = os.path.expanduser("~/workspace/skills/fantasy-trade-analyst")
sys.path.insert(0, os.path.join(SKILL, "bin"))
import trade_board as tb  # noqa: E402  (import runs no network)

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

check("churn threshold is 1.4 (persona 40% veto)",
      tb.CHURN_THRESHOLD == 1.4, f"got {tb.CHURN_THRESHOLD}")
check("WPOS covers K/DEF", "K" in tb.WPOS and "DEF" in tb.WPOS,
      f"got {tb.WPOS}")

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
check("pending_offers.md: Pittman ON-BLOCK",
      "6819" in real and real["6819"]["partner"] == "ydai",
      f"got {real}")

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
                "=== TEAM NEEDS (startable vs effective slots) ===",
                "=== YOUR ROSTER (by value) ===",
                "=== HOLE-CREATING OPTIONS (persona must price the roster cost) ===",
                "=== WAIVER WIRE ===",
                "=== WAIVER PRIORITY COST ==="):
        check(f"{tag}: header stable: {hdr[:30]}...", hdr in out)
    check(f"{tag}: hold-vs-spend line present",
          "hold-vs-spend" in out and "P(better target emerges" in out)

out = run_board(SNAPUSA).stdout
check("snapusa: PENDING OFFERS section (ADV-FF-07)",
      "=== PENDING OFFERS (your open offers" in out)
check("snapusa: Pittman flagged ON-BLOCK (ADV-FF-07)",
      "Michael Pittman" in out and "[ON-BLOCK" in out)
check("snapusa: posture line present", "| posture:" in out)

out_ww = run_board(WW).stdout
check("NF-01: WW slot line: 1 of 4, SCARCE",
      "weekend warriors waiver slot: 1 of 4 — SCARCE" in out_ww)
check("NF-01: snapusa slot line: 7 of 14, not scarce",
      "snapusa waiver slot: 7 of 14" in out
      and "snapusa waiver slot: 7 of 14 — SCARCE" not in out)

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("all golden tests passed")

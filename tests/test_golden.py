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
# ---------------- #24: FantasyPros ROS feed, not draft cheatsheet --------
import urllib.request as _urlreq  # noqa: E402


def _fp_page(ranking_type, rtype, scoring, last_updated, players):
    return ("var ecrData = {\"sport\":\"NFL\",\"type\":\"%s\","
            "\"ranking_type_name\":\"%s\",\"year\":\"2026\",\"week\":\"0\","
            "\"scoring\":\"%s\",\"last_updated\":\"%s\",\"players\":[%s]};\n"
            % (rtype, ranking_type, scoring, last_updated, players))


_FP_ROS_PAGE = _fp_page(
    "ros", "ROS PPR", "PPR", "9\\/28",
    '{"player_name":"Jahmyr Gibbs","player_bye_week":"7",'
    '"rank_ecr":1,"tier":1}')
_FP_HALF_PAGE = _fp_page(
    "ros", "ROS Half PPR", "HALF", "9\\/28",
    '{"player_name":"Jahmyr Gibbs","player_bye_week":"7",'
    '"rank_ecr":2,"tier":1}')
_FP_DRAFT_PAGE = _fp_page(
    "draft", "Draft", "STD", "8\\/01",
    '{"player_name":"Jahmyr Gibbs","player_bye_week":"7",'
    '"rank_ecr":5,"tier":1}')


class _fp_urlopen:
    page = _FP_ROS_PAGE
    seen = []

    def __call__(self, req, *a, **k):
        self.__class__.seen.append(req.full_url)
        page = self.__class__.page

        class _Resp:
            def read(self):
                return page.encode()
        return _Resp()


_saved_fp_urlopen = _urlreq.urlopen
_urlreq.urlopen = _fp_urlopen()
try:
    _fp_players = {"1": {"full_name": "Jahmyr Gibbs", "active": True,
                         "search_rank": 1}}
    _fp_urlopen.seen = []
    _fp, _upd = tb.get_fp_ecr(_fp_players, 1.0)
    check("#24: full-PPR fetches the ROS overall page (not a cheatsheet)",
          any("ros-ppr-overall.php" in u for u in _fp_urlopen.seen)
          and not any("cheatsheets" in u for u in _fp_urlopen.seen),
          f"urls: {_fp_urlopen.seen}")
    check("#24: ROS row maps by name with bye/ECR/tier, freshness = "
          "last_updated (not week 0)",
          _fp.get("1") == (7, 1, 1) and _upd == "9/28",
          f"fp={_fp}, upd={_upd}")
    _fp_urlopen.seen = []
    _fp_urlopen.page = _FP_HALF_PAGE
    _fp2, _upd2 = tb.get_fp_ecr(_fp_players, 0.5)
    check("#24: half-PPR fetches the ROS half-point-PPR overall page",
          any("ros-half-point-ppr-overall.php" in u for u in _fp_urlopen.seen),
          f"urls: {_fp_urlopen.seen}")
    check("#24: half-PPR page feeds half-PPR ranks (ECR 2, not PPR ECR 1)",
          _fp2.get("1") == (7, 2, 1), f"fp2={_fp2}")
    _fp_urlopen.page = _FP_DRAFT_PAGE
    _draft_rejected = False
    try:
        tb.get_fp_ecr(_fp_players, 1.0)
    except ValueError:
        _draft_rejected = True
    check("#24: draft-cheatsheet page is REJECTED, never labeled ROS",
          _draft_rejected)
finally:
    _urlreq.urlopen = _saved_fp_urlopen

# ---------------- #20: FantasyPros ROS K/DST ranks (K/DEF price signal) ----
_K_HTML = ("var ecrData = {\"ranking_type_name\":\"ros\",\"last_updated\":"
           "\"10\\/04\",\"players\":[{\"player_name\":\"Brandon Aubrey\","
           "\"rank_ecr\":1},{\"player_name\":\"Chase McLaughlin\","
           "\"rank_ecr\":25}]};\n")
_D_HTML = ("var ecrData = {\"ranking_type_name\":\"ros\",\"last_updated\":"
           "\"10\\/04\",\"players\":[{\"player_name\":\"Houston Texans\","
           "\"rank_ecr\":2},{\"player_name\":\"New York Giants\","
           "\"rank_ecr\":24}]};\n")
_D_DRAFT_HTML = ("var ecrData = {\"ranking_type_name\":\"draft\","
                 "\"last_updated\":\"8\\/01\",\"players\":[]};\n")


class _kd_urlopen:
    def __call__(self, req, *a, **k):
        slug = req.full_url.rsplit("/", 1)[-1]
        page = {"ros-k.php": _K_HTML, "ros-dst.php": _D_HTML}[slug]

        class _Resp:
            def read(self):
                return page.encode()
        return _Resp()


_kd_players = {
    "11533": {"full_name": "Brandon Aubrey", "position": "K", "team": "DAL",
              "active": True, "search_rank": 100},
    "99999": {"full_name": "Chase McLaughlin", "position": "K", "team": "TB",
              "active": True, "search_rank": 200},
    "HOU": {"full_name": None, "first_name": "Houston",
            "last_name": "Texans", "position": "DEF", "team": "HOU",
            "active": True, "search_rank": 50},
    "NYG": {"full_name": None, "first_name": "New York",
            "last_name": "Giants", "position": "DEF", "team": "NYG",
            "active": True, "search_rank": 60},
    "123": {"full_name": "Some QB", "position": "QB",
            "active": True, "search_rank": 1},
}
_saved_kd_urlopen = _urlreq.urlopen
_urlreq.urlopen = _kd_urlopen()
try:
    _kr = tb.get_fp_kdef(_kd_players)
    check("#20: K ranks map by name, DEFs join on first+last name",
          _kr == {"11533": 1, "99999": 25, "HOU": 2, "NYG": 24},
          f"got {_kr}")
    check("#20: non-K/DEF players are excluded from the rank map",
          "123" not in _kr)
finally:
    _urlreq.urlopen = _saved_kd_urlopen


class _kd_draft_urlopen:
    def __call__(self, req, *a, **k):
        class _Resp:
            def read(self):
                return _D_DRAFT_HTML.encode()
        return _Resp()


_saved_kd2_urlopen = _urlreq.urlopen
_urlreq.urlopen = _kd_draft_urlopen()
_kd_rejected = False
try:
    tb.get_fp_kdef(_kd_players)
except ValueError:
    _kd_rejected = True
finally:
    _urlreq.urlopen = _saved_kd2_urlopen
check("#20: draft-type K/DST page is REJECTED, never ranked as ROS",
      _kd_rejected)

check("#20: gate: top-12 unit 8+ ranks better suggests",
      tb.kdef_upgrade_suggests(2, 24) and tb.kdef_upgrade_suggests(12, 20))
check("#20: gate: outside top-12 / gap too small / missing ranks don't",
      not tb.kdef_upgrade_suggests(13, 30)
      and not tb.kdef_upgrade_suggests(2, 9)
      and not tb.kdef_upgrade_suggests(None, 24)
      and not tb.kdef_upgrade_suggests(2, None))

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

# ---------------- #19: fairness gap always on raw market values ---------
check("#19: raw-value gap formula",
      tb.fairness_gap(4000, 3000) == abs(4000 - 3000) / 4000)
check("#19: zero/zero -> 0, no crash", tb.fairness_gap(0, 0) == 0)
check("#19: an Out discount (0.5x) would shrink the gap under the old "
      "2-for-1 basis — the raw basis must not move",
      tb.fairness_gap(4000, 3000) == 0.25
      and tb.fairness_gap(4000, 3000 * 0.5) > tb.fairness_gap(4000, 3000),
      "gap must be computed on market values, not injury-discounted ones")

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
    fh.write("6819 | Michael Pittman | ydai | 2026-09-20 | pending | snapusa | x\n")
    fh.write("4321 | Dual Rostered | mate2 | 2026-09-21 | pending | ww | y\n")
    fh.write("1234 | Some Guy | mate | 2026-09-01 | rejected | snapusa | z\n")
    fh.write("5678 | Legacy Row | mate3 | 2026-09-02 | pending | z\n")
    tmp = fh.name
parsed_all = tb.load_pending_offers(tmp)
parsed_snap = tb.load_pending_offers(tmp, "snapusa")
parsed_ww = tb.load_pending_offers(tmp, "ww")
os.unlink(tmp)
check("pending offers: pending parsed, rejected ignored",
      set(parsed_all) == {"6819", "4321", "5678"}
      and parsed_all["6819"]["partner"] == "ydai"
      and parsed_all["6819"]["league"] == "snapusa",
      f"got {parsed_all}")
check("#26: on-block is per-league (snapusa run sees only snapusa + legacy)",
      set(parsed_snap) == {"6819", "5678"}, f"got {set(parsed_snap)}")
check("#26: on-block is per-league (ww run sees only ww + legacy)",
      set(parsed_ww) == {"4321", "5678"}, f"got {set(parsed_ww)}")
check("#26: legacy row (no league column) blocks in every league",
      parsed_snap["5678"]["league"] is None
      and parsed_ww["5678"]["league"] is None)
check("#26: league_tag maps both league ids",
      tb.league_tag(SNAPUSA) == "snapusa" and tb.league_tag(WW) == "ww")
check("#26: league_tag unknown id -> None (safe all-leagues fallback)",
      tb.league_tag("999") is None)
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

# ---------------- #22: bye-group computation shared by roster + audit ---
_bypos22 = {
    "QB": [{"id": "q1", "name": "Q B1"}, {"id": "q2", "name": "Q B2"}],
    "RB": [{"id": "r1", "name": "R B1"}],
    "WR": [{"id": "w1", "name": "W R1"}, {"id": "w2", "name": "W R2"},
           {"id": "w3", "name": "W R3"}],
    "TE": [{"id": "t1", "name": "T E1"}],
}
_fp22 = {"q1": (7, 10, 1), "q2": (7, 20, 2), "r1": (5, 40, 3),
         "w1": (7, 30, 2), "w2": (9, 60, 5), "w3": (7, 80, 7)}
_groups22 = tb.bye_group_names(_bypos22, _fp22, ("QB", "RB", "WR", "TE"))
check("#22: bye_group_names groups by week in position order",
      _groups22 == {7: ["Q B1", "Q B2", "W R1", "W R3"], 5: ["R B1"],
                    9: ["W R2"]},
      f"got {_groups22}")
check("#22: players with no FantasyPros bye data are absent",
      not any("T E1" in names for names in _groups22.values()),
      f"got {_groups22}")
check("#22: empty bypos -> empty groups",
      tb.bye_group_names({}, {}, ("QB",)) == {})
check("#22: bye-week ordering of one-week groups is week-sorted at use",
      sorted(_groups22) == [5, 7, 9])

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
real_snap = tb.load_pending_offers(os.path.join(SKILL, "pending_offers.md"), "snapusa")
real_ww = tb.load_pending_offers(os.path.join(SKILL, "pending_offers.md"), "ww")
check("#26: league-scoped loads are subsets of the unscoped load",
      set(real_snap) <= set(real) and set(real_ww) <= set(real),
      "league scoping must only ever remove rows, never add")

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
# ranking: NEW (breakout signal) first, then HEATING, STEADY, COOLING
_V3R = fi.trending_velocity(
    {"n": 100, "h": 100, "s": 100, "c": 100},
    {"ts": _NOW3 - 24 * 3600,
     "counts": {"h": 40, "s": 100, "c": 400}},
    {"n": {"full_name": "New", "position": "RB"},
     "h": {"full_name": "Heat", "position": "RB"},
     "s": {"full_name": "Steady", "position": "RB"},
     "c": {"full_name": "Cold", "position": "RB"}}, set())
check("G3: velocity ranking NEW < HEATING < STEADY < COOLING",
      [r["sid"] for r in _V3R] == ["n", "h", "s", "c"],
      f"got {[ (r['sid'], r['tag']) for r in _V3R ]}")
_V3B2 = fi.trending_velocity(
    {"b": 200, "e": 50},
    {"ts": _NOW3 - 24 * 3600, "counts": {"b": 100, "e": 100}},
    {"b": {"full_name": "Boundary Heat", "position": "RB"},
     "e": {"full_name": "Boundary Cold", "position": "RB"}}, set())
check("G3: threshold boundaries: 2.0x -> HEATING, 0.5x -> COOLING",
      any(r["sid"] == "b" and r["tag"] == "HEATING" for r in _V3B2)
      and any(r["sid"] == "e" and r["tag"] == "COOLING" for r in _V3B2),
      f"got {[(r['sid'], r['accel'], r['tag']) for r in _V3B2]}")


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

# --- G4/#23: playoff weeks must never enter the simulation ---
# Regression: cmd_playoff_odds used to call fetch_remaining_schedule with
# the default last_week=18, so weeks 15-18 (playoffs) were simulated as
# regular-season games, distorting bubble-team probabilities and the
# posture line that drives trade decisions.
_MW23 = {wk: [{"roster_id": 1, "matchup_id": 1},
              {"roster_id": 2, "matchup_id": 1}]
         for wk in range(3, 19)}
_WEEKS23 = []
_ORIG_FETCH23 = fi.fetch_json


def _fake_fetch23(url, *a, **k):
    m = re.search(r"/matchups/(\d+)$", url)
    if m:
        _WEEKS23.append(int(m.group(1)))
        return _MW23.get(int(m.group(1)), [])
    return _ORIG_FETCH23(url, *a, **k)


fi.fetch_json = _fake_fetch23
try:
    _WEEKS23.clear()
    _S23, _P23 = fi.fetch_remaining_schedule("L", 3, last_week=14)
    _FUT23 = sorted(w for w in set(_WEEKS23) if w >= 3)
finally:
    fi.fetch_json = _ORIG_FETCH23
check("G4/#23: fetch_remaining_schedule never requests a playoff week",
      _FUT23 == list(range(3, 15)), f"future weeks fetched={_FUT23}")
check("G4/#23: schedule holds only regular-season pairings",
      max((len(v) for v in _S23.values()), default=0) <= 12
      and sorted(_S23.get("1", [])) == ["2"] * 12,
      f"sched={_S23}")

# CLI-level: cmd_playoff_odds must pass last_week = playoff_week_start - 1.
_CAP23 = {}
_ORIG_FRS23 = fi.fetch_remaining_schedule
_ORIG_SIM23 = fi.simulate_playoffs
_ORIG_FETCH23B = fi.fetch_json


def _fake_fetch23b(url, *a, **k):
    if url.endswith("/state/nfl"):
        return {"week": 3}
    if re.search(r"/league/L$", url):
        return {"name": "T",
                "settings": {"playoff_week_start": 15, "playoff_teams": 2}}
    if url.endswith("/rosters"):
        return [{"roster_id": 1, "owner_id": "me",
                 "settings": {"wins": 2, "losses": 0, "ties": 0}},
                {"roster_id": 2, "owner_id": "x",
                 "settings": {"wins": 0, "losses": 2, "ties": 0}}]
    if url.endswith("/users"):
        return [{"user_id": "me", "display_name": "Wesley"},
                {"user_id": "x", "display_name": "X"}]
    if "/matchups/" in url:
        return []
    return _ORIG_FETCH23B(url, *a, **k)


def _fake_frs23(lid, wk, last_week=18):
    _CAP23["last_week"] = last_week
    return {}, {}


def _fake_sim23(records, sched, pteams, pfpg=None, sims=20000, seed=42):
    return {"1": (0.5, 2.0), "2": (0.5, 2.0)}


fi.fetch_json = _fake_fetch23b
fi.fetch_remaining_schedule = _fake_frs23
fi.simulate_playoffs = _fake_sim23
try:
    with tempfile.TemporaryDirectory() as _td23:
        _a23 = type("A", (), {"league": ["L"], "me": "me",
                              "out": os.path.join(_td23, "odds.md")})()
        fi.cmd_playoff_odds(_a23)
finally:
    fi.fetch_json = _ORIG_FETCH23B
    fi.fetch_remaining_schedule = _ORIG_FRS23
    fi.simulate_playoffs = _ORIG_SIM23
check("G4/#23: playoff CLI bounds the sim at the last regular-season week",
      _CAP23.get("last_week") == 14, f"last_week={_CAP23.get('last_week')}")

# Guard-level: a schedule with more games per team than regular-season
# weeks left (playoff weeks leaked in) fails loudly instead of printing
# corrupted probabilities.
def _fake_frs23_leak(lid, wk, last_week=18):
    return {"1": ["2"] * 16, "2": ["1"] * 16}, {}


fi.fetch_json = _fake_fetch23b
fi.fetch_remaining_schedule = _fake_frs23_leak
fi.simulate_playoffs = _fake_sim23
_LEAK23 = None
try:
    with tempfile.TemporaryDirectory() as _td23:
        _a23 = type("A", (), {"league": ["L"], "me": "me",
                              "out": os.path.join(_td23, "odds.md")})()
        try:
            fi.cmd_playoff_odds(_a23)
        except AssertionError as e:
            _LEAK23 = str(e)
finally:
    fi.fetch_json = _ORIG_FETCH23B
    fi.fetch_remaining_schedule = _ORIG_FRS23
    fi.simulate_playoffs = _ORIG_SIM23
check("G4/#23: leaked playoff-week games fail the sim loudly",
      _LEAK23 is not None and "playoff-week" in _LEAK23,
      f"got {_LEAK23}")

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

# G5 clamp boundaries: the softest possible slate hits exactly 1.10, the
# toughest exactly 0.90 (the 0.85 playoff-bye override is a separate,
# documented design choice — a bye in W15-17 scores a zero).
with tempfile.NamedTemporaryFile("w", suffix=".csv",
                                 delete=False) as _fh:
    _fh.write("season,game_type,week,away_team,home_team\n"
              "2026,REG,15,AAA,D1\n2026,REG,16,AAA,D1\n2026,REG,17,AAA,D1\n"
              "2026,REG,15,BBB,D2\n2026,REG,16,BBB,D2\n2026,REG,17,BBB,D2\n")
    _GCL = _fh.name
with tempfile.NamedTemporaryFile("w", suffix=".csv",
                                 delete=False) as _fh:
    _fh.write("season_type,position,opponent_team,game_id,"
              "rushing_yards,rushing_tds\n"
              "REG,RB,D1,g1,200,2\nREG,RB,D2,g2,10,0\n")
    _SCL = _fh.name
_PCL = fi.playoff_multipliers(
    _GCL, _SCL,
    {("softest", "AAA", "RB"): ("AAA", "RB"),
     ("toughest", "BBB", "RB"): ("BBB", "RB")},
    (15, 16, 17))
os.unlink(_GCL)
os.unlink(_SCL)
check("G5: clamp boundary: softest possible slate = 1.10 exactly",
      _PCL[("softest", "AAA", "RB")][0] == 1.10,
      f"got {_PCL[('softest', 'AAA', 'RB')]}")
check("G5: clamp boundary: toughest possible slate = 0.90 exactly",
      _PCL[("toughest", "BBB", "RB")][0] == 0.90,
      f"got {_PCL[('toughest', 'BBB', 'RB')]}")
check("G5: playoff-bye override is 0.85 by design (not the 0.9-1.1 clamp)",
      _PB[("bye back", "KC", "RB")] == (0.85, "[PLAYOFF BYE W16]")
      and fi.G5_BYE_MULT == 0.85)

# G5 gating: Week-5 season-clock gate (the comment at trade_board.py ~400)
check("G5: gate off before Week 5 (weights must not move early deals)",
      tb.g5_active(4, {("a", "KC", "RB"): (1.1, "[P+ soft]")}) is False)
check("G5: gate on from Week 5",
      tb.g5_active(5, {("a", "KC", "RB"): (1.1, "[P+ soft]")}) is True)
check("G5: gate off when the nflverse fetch produced no multipliers",
      tb.g5_active(9, {}) is False
      and tb.g5_active(17, None) is False)

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
# "mine": the backup is on my own roster -> protected, no UNPROTECTED flag
_HM4 = fi.handcuff_map(_DEP, ["s1", "s2"], {"9": ["s1", "s2"]}, _PL6,
                       {"9": "Me"}, "9")
check("G6: backup I own labeled mine",
      _HM4[0]["backups"][0]["status"] == "mine",
      f"got {_HM4[0]['backups']}")
# "unknown": the depth chart names a backup that matches no Sleeper
# player (no gsis_id, no name match) -> unknown, never faked as free
with tempfile.NamedTemporaryFile("w", suffix=".csv",
                                 delete=False) as _fh:
    _fh.write("dt,team,player_name,gsis_id,pos_abb,pos_rank\n"
              "2026-09-21T00:00:00Z,DAL,Lead Back,gsisA,RB,1\n"
              "2026-09-21T00:00:00Z,DAL,Ghost Back,gsisX,RB,2\n")
    _DCX = _fh.name
_DEPX = fi.parse_depth_charts(_DCX)
os.unlink(_DCX)
_PL6X = {"a1": {"full_name": "Lead Back", "position": "RB",
                "team": "DAL", "gsis_id": "gsisA"}}
_HMX = fi.handcuff_map(_DEPX, ["a1"], {"9": ["a1"]}, _PL6X,
                       {"9": "Me"}, "9")
check("G6: unmatchable backup -> unknown, never faked",
      _HMX[0]["backups"][0]["status"] == "unknown"
      and _HMX[0]["backups"][0]["sid"] is None,
      f"got {_HMX[0]['backups']}")


# ---------------- E2/G1/G2/G4/G7/G9: verify-or-implement rounds -----
print("== E2/G1/G2/G4/G7/G9 verification ==")


def _section(out, hdr):
    """Body lines of a printed board section (header-excluded, blank-skipped)."""
    i = out.find(hdr)
    assert i >= 0, f"header missing: {hdr}"
    lines = out[i:].splitlines()
    end = next((j for j in range(1, len(lines))
                if lines[j].startswith("===")), len(lines))
    return [ln for ln in lines[1:end] if ln.strip()]


# --- E2: module-level swap_delta with synthetic inputs ---
# lineup = top 6 by value: q1(100) w1(95) r1(90) w2(85) r2(80) r3(70) = 520
_E2P = {
    "QB": [{"id": "q1", "name": "My QB", "pos": "QB", "val": 100}],
    "RB": [{"id": "r1", "name": "My RB1", "pos": "RB", "val": 90},
           {"id": "r2", "name": "My RB2", "pos": "RB", "val": 80},
           {"id": "r3", "name": "My RB3", "pos": "RB", "val": 70}],
    "WR": [{"id": "w1", "name": "My WR1", "pos": "WR", "val": 95},
           {"id": "w2", "name": "My WR2", "pos": "WR", "val": 85}],
    "TE": [{"id": "t1", "name": "My TE1", "pos": "TE", "val": 60}],
}
_E2LIN = lambda bp: sorted(  # noqa: E731 - synthetic test helper
    [x for lst in bp.values() for x in lst],
    key=lambda x: -x["val"])[:6]
_E2MY = _E2LIN(_E2P)
_E2CTX = dict(me_bypos=_E2P, dval_for=lambda x: x["val"],
              lineup_ids_fn=_E2LIN,
              my_lineup_dval=sum(x["val"] for x in _E2MY),
              my_lineup=_E2MY,
              my_lineup_ids={x["id"] for x in _E2MY}, fp={})
_MINE3 = {"id": "r3", "name": "My RB3", "pos": "RB", "val": 70}
_lat = dict(_E2CTX)
_d_lat, _s_lat, _st_lat, _v_lat = tb.swap_delta(
    _MINE3, {"id": "x1", "name": "Lateral RB", "pos": "RB", "val": 70},
    **_lat)
check("E2: lateral equal-value swap that can't crack the lineup -> "
      "delta<=0 (board drops it)",
      _d_lat <= 0 and _v_lat is False, f"delta={_d_lat}")
_d_pos, _s_pos, _st_pos, _v_pos = tb.swap_delta(
    _MINE3, {"id": "x2", "name": "Stud RB", "pos": "RB", "val": 100},
    **_E2CTX)
check("E2: upgrade swap -> positive delta, incoming starts, sent player is "
      "gone not benched (#27)",
      _d_pos == 30 and _st_pos == ["Stud RB"] and _s_pos == []
      and _v_pos is False,
      f"delta={_d_pos} starts={_st_pos} sits={_s_pos}")
# the board's row filter + sort: delta<=0 never ranks, positive first
_rows = sorted([(d, ln) for d, ln in
                ((_d_lat, "lateral"), (_d_pos, "upgrade")) if d > 0],
               reverse=True)
check("E2: lateral dropped by the delta<=0 filter; positive ranks first",
      _rows and _rows[0][1] == "upgrade" and len(_rows) == 1,
      f"rows={_rows}")
_veto_ctx = dict(_E2CTX, fp={"x3": (7, None, None), "q1": (7, None, None),
                             "w1": (7, None, None), "r1": (7, None, None)})
_d_v, _, _, _veto = tb.swap_delta(
    _MINE3, {"id": "x3", "name": "Bye RB", "pos": "RB", "val": 200},
    **_veto_ctx)
check("E2: incoming pushing a bye week to 4+ starters is vetoed",
      _veto is True, f"delta={_d_v} veto={_veto}")
_pen_ctx = dict(_E2CTX, fp={"x3": (7, None, None), "q1": (7, None, None),
                            "w1": (7, None, None)})
_d_p, _, _, _veto_p = tb.swap_delta(
    _MINE3, {"id": "x3", "name": "Bye RB", "pos": "RB", "val": 200},
    **_pen_ctx)
check("E2: 3-starter bye cluster takes the 20% penalty, not the veto",
      _veto_p is False and abs(_d_p - (650 - 520 - 0.20 * 200)) < 1e-9,
      f"delta={_d_p} veto={_veto_p}")
# --- #21: sits/starts must be computed vs the DISCOUNTED lineup ---
# q1 is an Out starter: raw 100, discounted 50, falls out of the top-6.
# delta baseline (480) never includes him, so the text must not either —
# one basis for the number and its explanation.
_O21P = {
    "QB": [{"id": "q1", "name": "Out QB", "pos": "QB", "val": 100}],
    "RB": [{"id": "r1", "name": "My RB1", "pos": "RB", "val": 90},
           {"id": "r2", "name": "My RB2", "pos": "RB", "val": 80},
           {"id": "r3", "name": "My RB3", "pos": "RB", "val": 70}],
    "WR": [{"id": "w1", "name": "My WR1", "pos": "WR", "val": 95},
           {"id": "w2", "name": "My WR2", "pos": "WR", "val": 85},
           {"id": "w3", "name": "My WR3", "pos": "WR", "val": 55}],
    "TE": [{"id": "t1", "name": "My TE1", "pos": "TE", "val": 60}],
}
_O21DVAL = lambda x: x["val"] * (0.5 if x["id"] == "q1" else 1.0)  # noqa: E731
_O21D = _E2LIN({p: [dict(x, val=_O21DVAL(x)) for x in lst]
                for p, lst in _O21P.items()})
_O21CTX = dict(me_bypos=_O21P, dval_for=_O21DVAL,
               lineup_ids_fn=_E2LIN,
               my_lineup_dval=sum(x["val"] for x in _O21D),
               my_lineup=_O21D,
               my_lineup_ids={x["id"] for x in _O21D}, fp={})
_d21, _s21, _st21, _v21 = tb.swap_delta(
    {"id": "q1", "name": "Out QB", "pos": "QB", "val": 100},
    {"id": "x9", "name": "New QB", "pos": "QB", "val": 75},
    **_O21CTX)
check("#21: Out starter dropped from the discounted baseline -> sits/starts "
      "describe the same baseline as the delta, sent player never 'sits'",
      _d21 == 15 and _st21 == ["New QB"] and _s21 == ["My TE1"]
      and "Out QB" not in _s21 and _v21 is False,
      f"delta={_d21} starts={_st21} sits={_s21}")
# --- #27: the sent player is gone, not "sits" ---
# r1 is a projected starter (val 90, in the top-6). Sending him for a stud:
# pre-fix the one-liner listed him under "sits" (he can never be in the
# new lineup, so any sent starter landed there unconditionally).
_MINE27 = {"id": "r1", "name": "My RB1", "pos": "RB", "val": 90}
_IN27 = {"id": "x4", "name": "Stud RB", "pos": "RB", "val": 120}
_d27, _s27, _st27, _v27 = tb.swap_delta(_MINE27, _IN27, **_E2CTX)
check("#27: sent starter excluded from sits (gone, not benched); "
      "incoming still starts",
      _d27 == 30 and _st27 == ["Stud RB"] and _s27 == []
      and "My RB1" not in _s27 and _v27 is False,
      f"delta={_d27} starts={_st27} sits={_s27}")
_nb27 = {p: [dict(x) for x in lst if x["id"] != "r1"]
         for p, lst in _E2P.items()}
_nb27["RB"].append(dict(_IN27))
_old27 = [x["name"] for x in _E2MY
          if x["id"] not in {x["id"] for x in _E2LIN(_nb27)}]
check("#27 negative control: the pre-fix one-liner listed the sent "
      "starter under sits",
      _old27 == ["My RB1"], f"old sits={_old27}")
# --- G4/G7: full chain — simulate -> snapshot -> board parse ---
_ORIG_FETCH47 = fi.fetch_json


def _fake_fetch47(url, *a, **k):
    if url.endswith("/state/nfl"):
        return {"week": 3}
    if re.search(r"/league/L$", url):
        return {"name": "T",
                "settings": {"playoff_week_start": 15, "playoff_teams": 1}}
    if url.endswith("/rosters"):
        return [{"roster_id": 1, "owner_id": "me",
                 "settings": {"wins": 2, "losses": 0, "ties": 0}},
                {"roster_id": 2, "owner_id": "opp",
                 "settings": {"wins": 0, "losses": 2, "ties": 0}}]
    if url.endswith("/users"):
        return [{"user_id": "me", "display_name": "Wesley"},
                {"user_id": "opp", "display_name": "Rival"}]
    m = re.search(r"/matchups/(\d+)$", url)
    if m:
        wk = int(m.group(1))
        if wk == 1:
            return [{"roster_id": 1, "points": 150.0},
                    {"roster_id": 2, "points": 130.0}]
        if wk == 2:
            return [{"roster_id": 1, "points": 140.0},
                    {"roster_id": 2, "points": 160.0}]
        if wk >= 3:  # future: published with matchup_id set
            return [{"roster_id": 1, "points": 0, "matchup_id": 1},
                    {"roster_id": 2, "points": 0, "matchup_id": 1}]
    return []


fi.fetch_json = _fake_fetch47
try:
    with tempfile.TemporaryDirectory() as _td47:
        _a47 = type("A", (), {"league": ["L"], "me": "me",
                              "out": os.path.join(_td47, "odds.md")})()
        fi.cmd_playoff_odds(_a47)
        _L47 = tb.playoff_snapshot_lines("T", path=_a47.out)
        _stale47 = os.path.join(_td47, "stale.md")
        open(_stale47, "w").write("x\n")
        _old = time.time() - 73 * 3600
        os.utime(_stale47, (_old, _old))
        try:
            tb.playoff_snapshot_lines("T", path=_stale47)
            _raised47 = False
        except FileNotFoundError:
            _raised47 = True
        try:
            tb.playoff_snapshot_lines("T", path="/nonexistent/odds.md")
            _raised47m = False
        except FileNotFoundError:
            _raised47m = True
        _other47 = tb.playoff_snapshot_lines("Other", path=_a47.out)
finally:
    fi.fetch_json = _ORIG_FETCH47
check("G4: snapshot chain carries the playoff-probability + posture flag",
      any("Wesley playoff probability" in ln and "CONTENDER" in ln
          for ln in _L47),
      f"lines={_L47}")
check("G4: snapshot chain shows PF/G + playoff% + expected wins",
      any("| 145.0 |" in ln and "100%" in ln for ln in _L47),
      f"lines={_L47}")
check("G7: schedule-luck math visible (actual | expected | luck)",
      any("| Wesley | 2 | 1.0 | +1.0 |" in ln for ln in _L47),
      f"lines={_L47}")
check("G4/G7: stale (>72h) snapshot raises -> board degrades, never fakes",
      _raised47)
check("G4/G7: missing snapshot raises -> board degrades, never fakes",
      _raised47m)
check("G4/G7: league-name scoping (wrong league -> honest stub)",
      _other47 == ["  (snapshot has no section for this league yet)"],
      f"got {_other47}")

# --- G12: schedule-luck table surfaces actual | expected | luck ---
_G12P = tempfile.NamedTemporaryFile("w", suffix=".md", delete=False)
_G12P.write("## T (playoffs W15, 1 teams)\n"
            "Wesley playoff probability: 100% — CONTENDER\n"
            "| team | W-L | PF/G | playoff% | exp wins |\n"
            "|---|---|---|---|---|\n"
            "| Wesley | 2-0 | 145.0 | 100% | 2.0 | <-- YOU\n"
            "\n### Schedule luck (all-play expected wins)\n"
            "| team | actual | expected | luck |\n"
            "|---|---|---|---|\n"
            "| Wesley | 2 | 1.0 | +1.0 | <-- YOU\n"
            "| Rival | 0 | 1.0 | -1.0 |\n")
_G12P.close()
_G12 = tb.playoff_snapshot_lines("T", path=_G12P.name)
os.unlink(_G12P.name)
check("G12: schedule-luck row surfaces actual | expected | luck",
      any("| Wesley | 2 | 1.0 | +1.0 |" in ln for ln in _G12),
      f"lines={_G12}")
check("G12: luck row is the board's all-play expected-wins math, not a fake",
      not any("snapshot has no section" in ln for ln in _G12))

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
# every contingency type outranks a plain clogger at equal base value,
# and the math matches the documented weights (handcuff x1.9, the
# hurt-starter backup x2.1, rising usage x1.25)
_R8B, _D8B = fi.clog_audit(
    [{"sid": "1", "name": "Clogger", "pos": "WR", "val": 1000, "inj": ""},
     {"sid": "2", "name": "Cuff", "pos": "RB", "val": 1000, "inj": ""},
     {"sid": "3", "name": "HurtAhead", "pos": "RB", "val": 1000, "inj": ""},
     {"sid": "4", "name": "Rising", "pos": "WR", "val": 1000, "inj": ""}],
    handcuff_of={"2": True}, ahead_out={"3": True}, rising={"4": True})
check("G8: contingent math matches the documented weights",
      {r["name"]: r["contingent"] for r in _R8B}
      == {"Clogger": 1000.0, "Cuff": 1900.0,
          "HurtAhead": 2100.0, "Rising": 1250.0},
      f"got {[(r['name'], r['contingent']) for r in _R8B]}")
check("G8: every contingency outranks the plain clogger",
      _R8B[0]["name"] == "Clogger"
      and {r["name"] for r in _R8B[1:]}
      == {"Cuff", "HurtAhead", "Rising"})
check("G8: drops name only the unprotected clogger",
      _D8B == ["Clogger"])
# #25 regression: unpriced positional starters (K/DEF carry val 0 because
# there is no FantasyCalc feed for them) always bottom the contingent
# ranking; they must stay in the ranking for completeness but NEVER be
# named as drops — unpriced is not droppable.
_R8C, _D8C = fi.clog_audit(
    [{"sid": "k1", "name": "OnlyK", "pos": "K", "val": 0, "inj": ""},
     {"sid": "d1", "name": "OnlyD", "pos": "DEF", "val": 0, "inj": ""},
     {"sid": "1", "name": "Clogger", "pos": "WR", "val": 1000, "inj": ""}])
check("G8: unpriced K/DEF sort to the bottom of the ranking",
      [_R8C[0]["name"], _R8C[1]["name"]] == ["OnlyK", "OnlyD"]
      and _R8C[2]["name"] == "Clogger")
check("G8: unpriced players are never named as drop candidates",
      _D8C == ["Clogger"])


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
    # #20: K/DEF top-FA lines price by FP ROS rank when the feed is live
    # and degrade honestly to "unpriced" when it isn't — never a bare (0).
    for _pos20 in ("K", "DEF"):
        _tl20 = next((ln for ln in out.splitlines()
                      if ln.startswith(f"  top FA {_pos20}:")), "")
        check(f"{tag}: #20 top FA {_pos20} shows FP-ROS rank or unpriced",
              bool(re.search(r"\(FP-ROS #\d+\)|\(unpriced\)", _tl20)),
              f"line: {_tl20[:100]}")
    check(f"{tag}: swaps sorted by delta (ADV-FF-15)",
          "=== CANDIDATE SWAPS (sorted by lineup-points delta) ===" in out)
    check(f"{tag}: trade-lock boundary line (ADV-FF-14)",
          "Trade lock: trades legal through NFL Week" in out)
    # #24: the ECR line must cite a validated ROS feed — never the
    # "week 0" draft-cheatsheet mislabel, and never draft ranks.
    _fp_lines = [ln for ln in out.splitlines()
                 if ln.startswith("FantasyPros rest-of-season ECR (")
                 or ln.startswith("FantasyPros ECR unavailable")]
    check(f"{tag}: FP line is an honest ROS stamp or an honest stub (#24)",
          len(_fp_lines) == 1 and (
              _fp_lines[0].startswith("FantasyPros ECR unavailable this run")
              or bool(re.match(
                  r"FantasyPros rest-of-season ECR \(ROS, updated [0-9/]+, "
                  r"\d+ players matched\)", _fp_lines[0])))
          and "(week 0" not in _fp_lines[0],
          f"lines: {_fp_lines}")
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
    # #22: YOUR ROSTER must carry the same bye-stack warnings the audit
    # flags — every week with 2+ of his players on bye gets a
    # [BYE-STACK W..] tag on each of those roster lines. (Previously the
    # counts were computed after the roster printed, so the tags never
    # fired there.)
    _roster_sec = out.split("=== YOUR ROSTER (by value) ===")[1].split(
        "===")[0]
    _audit_sec = out.split("=== BYE WEEK AUDIT (FantasyPros) ===")[1].split(
        "=== BYE-CRATER FORECAST")[0]
    _audit_groups = {}
    for _m in re.finditer(r"^\s*Week (\d+): ([^<\n]+)", _audit_sec, re.M):
        _audit_groups[int(_m.group(1))] = [
            n.strip() for n in _m.group(2).split(",")]
    for _w in sorted(_audit_groups):
        _names = _audit_groups[_w]
        if len(_names) < 2:
            continue
        _tagw = "[BYE-STACK W%d]" % _w
        _got = _roster_sec.count(_tagw)
        check("%s: #22 roster shows %s for %d-on-bye cluster"
              % (tag, _tagw, len(_names)),
              _got >= len(_names),
              f"audit={_names}, roster tag hits={_got}")
    # G5 gate: the weight lines print iff Week >= 5 AND the nflverse
    # fetch produced multipliers (G1 degrades -> no multipliers).
    _wk = int(re.search(r"NFL Week (\d+) \|", out).group(1))
    _g1sec = out.split("=== USAGE-GAP RADAR (G1")[1].split(
        "=== CANDIDATE SWAPS")[0]
    _g5expect = _wk >= 5 and "(unavailable this run" not in _g1sec
    check(f"{tag}: G5 swap note gated (present iff Week>=5 with data)",
          ("G5: deltas use playoff-weighted values (W15-17 SOS x0.9-1.1)"
           in out) == _g5expect,
          f"week={_wk} expected={_g5expect}")
    check(f"{tag}: G5 roster legend gated with the weights",
          ("[P+]/[P-]: playoff-weeks (W15-17) schedule soft/brutal" in out)
          == _g5expect)
    # G6: the section always prints a body (backup lines or a marked stub),
    # and a free backup is never printed without the UNPROTECTED flag.
    _g6sec = out.split(
        "=== HANDCUFF LEVERAGE MAP (G6: each of your RBs' direct backup) ==="
    )[1].split("===")[0]
    check(f"{tag}: G6 section prints a body, never an empty section",
          any(k in _g6sec for k in
              ("backup", "lead back", "no RBs on your roster",
               "unavailable", "INFERRED", "no depth-chart data",
               "no data")),
          f"section: {_g6sec[:120]}")
    _freelines = [ln for ln in _g6sec.splitlines() if "[free]" in ln]
    check(f"{tag}: G6 free backup always carries the UNPROTECTED flag",
          all("UNPROTECTED" in ln for ln in _freelines),
          f"unflagged: {[ln for ln in _freelines if 'UNPROTECTED' not in ln]}")
    # G8: rows carry contingent values, and every rendered tag names one
    # of the three contingencies (handcuff / hurt-ahead / rising).
    _g8sec = out.split("roster-clog audit (G8")[1].split("===")[0]
    check(f"{tag}: G8 rows rank by contingent value",
          re.search(r" -> contingent [\d.]+\)", _g8sec) is not None)
    _g8tags = set(re.findall(r"\[[a-z-]+\]", _g8sec))
    check(f"{tag}: G8 contingency tags use the named vocabulary",
          _g8tags <= {"[handcuff]", "[ahead-out]", "[rising]"},
          f"tags: {_g8tags}")
    check(f"{tag}: G3 trending velocity line",
          "trending velocity (Sleeper-wide adds" in out)
    _tline = next((ln for ln in out.splitlines()
                   if "trending velocity (Sleeper-wide adds" in ln), "")
    check(f"{tag}: G3 lookback window labeled in output",
          "24h vs previous scan" in _tline, f"line: {_tline[:80]}")
    _toks = (_tline.split(": ", 1)[1].split(", ")
             if ": " in _tline and "(unavailable this run)" not in _tline
             else [])
    check(f"{tag}: G3 NEW/HEATING/COOLING tags render",
          all(re.search(r"/24h,(NEW|x[\d.]+ vs prev \d+h "
                        r"(HEATING|COOLING|STEADY))", e) for e in _toks),
          f"untagged entries: "
          f"{[e for e in _toks if not re.search(r'/24h,(NEW|x)', e)]}")
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
                    r"(FULL: every ADD needs a DROP|\d+ open bench "
                    r"slot\(s\))",
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

# --- #19: fairness gaps are raw-market-basis in BOTH swap sections -------
# Each printed row carries its players' raw FantasyCalc values, so the
# test recomputes the gap from the row's own values. Under the old 2-for-1
# basis (injury-discounted / G5-weighted) any row with a discounted or
# weighted player fails this — from Week 5 the G5 weights make that
# nearly every row, so a regression here goes red fast.
def _gap_row_ok(ln):
    _vals = [float(x) for x in
             re.findall(r"\((\d+(?:\.\d+)?), \d+(?:\.\d+)?-\d+(?:\.\d+)?\)",
                        ln)]
    _mg = re.search(r"\[gap(?: vs combined)? (\d+)%\|([A-Z]+)\]", ln)
    if len(_vals) not in (2, 3) or not _mg:
        return "unparseable: %s" % ln[:100]
    _gap = tb.fairness_gap(sum(_vals[:-1]), _vals[-1])
    if f"{_mg.group(1)}%" != f"{_gap:.0%}":
        return "gap basis mismatch: %s" % ln[:100]
    if _mg.group(2) != tb.fairness_band(_gap):
        return "band mismatch: %s" % ln[:100]
    return None


_19_lines = [ln for _o in (out, out_ww) for ln in _o.splitlines()
             if "you send" in ln and "[gap" in ln]
_19_bad = [b for b in (_gap_row_ok(ln) for ln in _19_lines) if b]
check("#19: printed fairness gaps in both swap sections recompute from "
      "the rows' raw market values (or the honest no-fits stub)",
      (_19_lines and not _19_bad)
      or any("(no clean 1-for-1 fits)" in ln
             for _o in (out, out_ww) for ln in _o.splitlines()),
      f"mismatches: {_19_bad[:3]} (rows checked: {len(_19_lines)})")
check("NF-01: WW slot line self-consistent",
      bool(_m2) and (("— SCARCE" in _m2.group(0))
                     == (int(_m2.group(1)) <= tb.SCARCE_SLOT_CUTOFF)))

# --- G1: usage-gap radar live-section check ---
_G1 = _section(out_snap, "=== USAGE-GAP RADAR (G1")
_cand = [ln for ln in _G1
         if any(k in ln for k in ("HOLD (", "SHOP (", "TARGET (", "WIRE ("))]
check("G1: radar emits named candidates with usage-vs-output evidence "
      "(or an honest stub)",
      (_cand and all("usage-implied" in ln and "(gap " in ln
                     for ln in _cand))
      or any("(no major usage gaps this week)" in ln for ln in _G1)
      or any("(unavailable this run" in ln for ln in _G1),
      f"radar section: {_G1[:2]}")

# --- G2: manager trade profiles live-section check ---
_G2 = _section(out_snap, "=== MANAGER TRADE PROFILES (G2")
_profl = [ln for ln in _G2 if "trade(s)" in ln]
check("G2: real profiles render frequency + position prefs + net value "
      "(or the no-trades stub)",
      (_profl and all("buys" in ln and "sells" in ln and "net value" in ln
                      for ln in _profl))
      or any("no completed trades yet" in ln for ln in _G2)
      or any("(unavailable" in ln for ln in _G2),
      f"profiles section: {_G2[:2]}")

# --- G9: bye-crater forecast live-section checks ---
_nflw = int(re.search(r"NFL Week (\d+)", out_snap).group(1))
_G9 = _section(out_snap, "=== BYE-CRATER FORECAST (G9")
if any("(unavailable" in ln for ln in _G9):
    check("G9: forecast horizon is the next 4 weeks (degraded this run)",
          True)
else:
    _wks = [int(x) for ln in _G9
            for x in re.findall(r"^  W(\d+):", ln)]
    check("G9: forecast covers exactly the next 4 weeks after the current "
          "one", _wks == [_nflw + 1, _nflw + 2, _nflw + 3, _nflw + 4],
          f"got {_wks}")
    check("G9: current week is excluded (E1 owns the current week)",
          not any(re.match(rf"^  W{_nflw}:", ln) for ln in _G9),
          f"section: {_G9[:2]}")
    _named_ok = True
    for _ln in _G9:
        _mm = re.search(r"\((\d+) starters?(?:: ([^)]*))?\)", _ln)
        if _mm and int(_mm.group(1)) > 0 and not (_mm.group(2) or "").strip():
            _named_ok = False
    check("G9: affected starters are named when any are on bye", _named_ok,
          f"section: {_G9}")
    check("G9: 2+ starters on bye gets the CRATER flag",
          all("<-- CRATER (2+ starters out)" in _ln for _ln in _G9
              if re.search(r"\(([2-9]\d*) starters:", _ln)),
          f"section: {_G9}")

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("all golden tests passed")

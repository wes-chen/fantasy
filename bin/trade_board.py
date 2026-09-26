#!/usr/bin/env python3
"""Fantasy trade board: league-wide needs/surplus + candidate deals.

Pulls live Sleeper rosters, FantasyCalc redraft values (settings-matched:
superflex/QB count, team count, PPR), FantasyPros rest-of-season ECR +
bye weeks (independent cross-check, scraped from their rankings page),
the season clock (NFL week vs trade deadline -> posture), and the league's
completed trade history as market comps. Prints a readable board; the
analyst (persona) turns it into recommendations.

Usage:
  trade_board.py --league <league_id> --me <user_id>
                 [--players-cache ~/workspace/sleeper/players.json]
"""
import argparse, json, os, re, sys, time, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fantasy_insights as fi  # G1-G10 analytics (pure functions + fetchers)

SLEEPER = "https://api.sleeper.app/v1"
FC = "https://api.fantasycalc.com/values/current"

# --- tuning constants (see gap-analysis ADV-FF-05/06/09/10) ---
CHURN_THRESHOLD = 1.4  # persona veto (SKILL.md judgment 7): no ADD/DROP churn
                       # below a ~40% value edge — a 12% edge in the 14-team
                       # desert means dropping a healthy contributor for a
                       # streamer, exactly the churn the persona prevents.
WPOS = ("QB", "RB", "WR", "TE", "K", "DEF")  # waiver model covers K/DEF:
                       # Wesley's real claims are mostly K/DST.
HURT = ("Out", "IR", "Doubtful", "Suspended")  # never suggest adding
# --- NF-01: waiver priority cost model (see gap-analysis NF-01) ---
# The engine suggests ADD/DROP pairs but never priced the priority slot
# burned. Opportunity cost = P(a better target emerges before the weekly
# Tuesday reset) x typical pickup value; scarce slots demand a bigger edge.
BASE_EMERGE = 0.6     # ~3-in-5 weeks a claim-worthy target emerges on a RICH
                      # wire; scaled down by wire richness for deep leagues
SCARCE_SLOT_CUTOFF = 3  # slots 1..3 are scarce: spending one is a real cost
SCARCE_SLOT_MIN_EDGE = 0.75  # a scarce slot demands a >=75% value upgrade
                      # (stricter than the 40% CHURN_THRESHOLD gate) —
                      # a 40% edge means less when it costs the #1 slot
TYPICAL_EDGE_FALLBACK = 6.0  # assumed median pickup edge when this run
                      # suggests no moves; ~one flex-starter tier jump

# --- E6: value-gap fairness bands (see gap-analysis E6) ---
# A raw gap percentage is unanchored: is 25% "close enough"? Bands give the
# persona a calibrated read, and the trade-history section prints this
# league's largest accepted gap as the live calibration (recalibrated every
# run as trades complete — a league that accepted 59% for a QB is a market
# where STRETCH gaps are ordinary business).
def fairness_band(gap):
    """Fairness band for a value gap (fraction 0..1), from E6."""
    if gap < 0.10:
        return "EXCELLENT"
    if gap <= 0.20:
        return "FAIR"
    if gap <= 0.35:
        return "STRETCH"
    return "UNFAIR"

def wire_richness(num_teams):
    """Wire talent density: small-league wires are rich (replacement level
    is high, a better target almost always emerges); 14-team wires are a
    desert where most weeks nothing claim-worthy appears."""
    if num_teams <= 6:
        return 1.0
    if num_teams <= 10:
        return 0.6
    return 0.3

def waiver_slot(rosters, me):
    """Wesley's waiver slot from settings.waiver_position (field verified
    against live Sleeper rosters output). Falls back to roster_id order —
    Sleeper's default display order — if the field is ever absent."""
    mine = next((r for r in rosters if r.get("owner_id") == me), None)
    if mine is None:
        return None
    wp = (mine.get("settings") or {}).get("waiver_position")
    if wp:
        return int(wp)
    ordered = sorted(rosters, key=lambda r: r.get("roster_id") or 0)
    return next(i for i, r in enumerate(ordered, 1)
                if r.get("owner_id") == me)

def slot_scarcity(position, num_teams):
    """0..1: 1.0 at #1 (top of the order), ~0 at the tail (near-free)."""
    return max(0.0, 1.0 - (position - 1) / max(num_teams, 1))

def hold_value(position, num_teams, typical_edge):
    """Expected value of holding the slot: P(better target emerges before
    the Tuesday reset) x typical pickup edge.

    P = base emergence x slot scarcity x wire richness. Rationale: in a
    rich-wire small league a #1 slot is likely to catch a better target
    next week; in a thin-wire deep league at #7 the slot is nearly free."""
    p = (BASE_EMERGE * slot_scarcity(position, num_teams)
         * wire_richness(num_teams))
    return p, p * typical_edge

def slot_verdict(edge, drop_val, position, num_teams, forced=False):
    """NF-01 slot-cost veto: a scarce (top-3) slot burned on a marginal gain
    is flagged HOLD. Layers on top of the 1.4x CHURN_THRESHOLD — a claim that
    clears the churn gate can still be a bad spend at #1. Forced
    replacements (a hurt starter must be replaced) are never flagged."""
    if forced or edge is None or drop_val is None:
        return None
    if position is not None and position <= SCARCE_SLOT_CUTOFF:
        if edge < SCARCE_SLOT_MIN_EDGE * drop_val:
            return (f"BURNS #{position} — edge too thin, HOLD "
                    f"(scarce slot needs >={SCARCE_SLOT_MIN_EDGE:.0%} "
                    f"upgrade)")
    return None
INJURY_DISCOUNT = {  # E5: discount stale values before RANKING (ADV-FF-10)
    "Out": 0.5, "IR": 0.5, "Suspended": 0.5,
    "Doubtful": 0.7, "Questionable": 0.85,
}

def acquisition_path(pid, recent_drops, waiver_clear_days=2, now_ms=None):
    """FA NOW vs CLAIM (ADV-FF-18): a player dropped within the league's
    waiver_clear_days sits on waivers (claim: clears Tuesday, burns his
    priority slot); anything else is an instant free-agent add with no
    priority cost. Wesley's 9/23 ruling: price priority only when a claim
    is actually required."""
    pid = str(pid)
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    try:
        days = int(waiver_clear_days or 2)
    except (TypeError, ValueError):
        days = 2
    dropped_at = recent_drops.get(pid)
    if dropped_at and (now_ms - int(dropped_at)) < days * 86400 * 1000:
        return "claim"
    return "fa"

def clear_label(league_settings):
    """Waiver-clear wording. Sleeper waiver_day_of_week: 1 is verified
    to clear at midnight at the end of Monday, going into Tuesday."""
    dow = (league_settings or {}).get("waiver_day_of_week")
    if dow == 1:
        return "clears Tue 12:00am PT"
    return f"clears (waiver day {dow} — unverified)"

def slot_math(roster, rp, reserve_slots):
    """Live active/bench/IR arithmetic (E14). Sleeper double-lists IR
    occupants in `players`, so active = players minus the reserve
    overlap; a roster is FULL when active >= len(roster_positions). Every
    ADD/DROP the board emits is validated against this — the 9/23
    Rodgers advice ("no drop needed") was built on a bad roster read
    and this is the machine check that kills that class."""
    pids = [str(x) for x in ((roster or {}).get("players") or [])]
    res = {str(x) for x in ((roster or {}).get("reserve") or [])}
    reserve_ids = [p for p in pids if p in res]
    active = len(pids) - len(reserve_ids)
    bench_max = len(rp or [])
    slots, open_ = ir_capacity(reserve_slots, len(reserve_ids))
    return {"active": active, "bench_max": bench_max,
            "drop_needed": active >= bench_max,
            "reserve_ids": reserve_ids, "ir_slots": slots, "ir_open": open_}

def ir_move_valid(pid, reserve_ids, ir_open):
    """IR-move gate (E14/ADV-FF-16): any recommendation to move a player
    into the IR slot must pass this first. Catches the 9/23 no-op —
    Pacheco was already in reserve, so the "move" freed no bench slot.
    The caller must separately confirm IR eligibility from
    injury_status; this function validates occupancy only."""
    pid = str(pid)
    if pid in {str(x) for x in (reserve_ids or [])}:
        return (False, "already on IR — the move is a no-op")
    try:
        open_ = int(ir_open or 0)
    except (TypeError, ValueError):
        open_ = 0
    if open_ <= 0:
        return (False, "IR full — stashing here costs an active roster spot")
    return (True, "ok")

# E5 timing bracket: a season-ending-style injury hurts less early in the
# year (the player may return and contribute) than late (the season is
# nearly over). Applies to Out/IR/Doubtful only — Questionable and
# Suspended keep their static multipliers.
def injury_week_bracket(nfl_week):
    """Timing multiplier for season-long injury designations (E5)."""
    if nfl_week is None or nfl_week <= 6:
        return 1.0
    if nfl_week <= 12:
        return 0.85
    return 0.6


def injury_discount(status, nfl_week=None):
    """Value multiplier for an injury designation (E5).

    ADV-FF-10 applied the static status multiplier; E5 adds the season
    timing: Out/IR/Doubtful decay as the season progresses (an 8-week
    injury in Week 12 is worth far less than the same injury in Week 3).
    Pass nfl_week=None (the default) for the static behavior when the
    week is unknown."""
    base = INJURY_DISCOUNT.get(status or "", 1.0)
    if status in ("Out", "IR", "Doubtful"):
        base *= injury_week_bracket(nfl_week)
    return base


def value_range(val, trend30d):
    """E7: per-player value RANGE from 30-day trend volatility.

    Single-point FantasyCalc values overstate precision. The 30-day
    trend is the signed drift over the last month; treat half its
    magnitude as the +/- uncertainty band around the point value — a
    move inside that band isn't distinguishable from recent noise.
    Returns (lo, hi) as ints, lo clamped at 0."""
    try:
        half = abs(int(trend30d or 0)) / 2
    except (TypeError, ValueError):
        half = 0.0
    v = val or 0
    return (max(0, int(v - half)), int(v + half))


# E7: a swap is a "low-signal lateral" when FantasyCalc itself doesn't
# consider either side's 30-day move worth displaying (displayTrend False
# on both sides) AND the projected lineup gain is under this fraction of
# the larger point value. Flagged, not dropped — fit/bye reasons can
# still justify the deal.
FLAT_TREND_DELTA_FRAC = 0.10


def low_signal_lateral(mine_disp, theirs_disp, delta, mine_val, theirs_val):
    """E7: the "this move matters" filter — flag, don't drop.

    Both 30-day trends flat (FC displayTrend False on both sides) plus a
    small lineup delta relative to the point values = a lateral move the
    numbers alone can't justify. Returns a flag string, or "" when the
    move carries a real signal."""
    if mine_disp or theirs_disp:
        return ""
    try:
        big = max(float(mine_val), float(theirs_val))
    except (TypeError, ValueError):
        big = 0.0
    if big <= 0:
        return ""
    if delta < FLAT_TREND_DELTA_FRAC * big:
        return (" [LOW-SIGNAL LATERAL: both 30-day trends flat, "
                "lineup delta small]")
    return ""


def legal_drops(bench, reserve_ids, drop_needed):
    """DROP candidates for a legal ADD/DROP pair (E14 follow-up).

    When the roster is FULL, dropping an IR/reserve occupant frees no
    ACTIVE bench slot — the ADD would have nowhere to go. So on a full
    roster the drop must come from the active roster; when slots are open,
    any bench player (including reserve occupants) is a legal drop.
    """
    res = {str(x) for x in (reserve_ids or [])}
    if drop_needed:
        return [b for b in bench if str(b.get("id")) not in res]
    return list(bench)


def ir_capacity(reserve_slots, reserve_count):
    """IR-slot accounting (E13): returns (slots, open).

    Sleeper exposes IR capacity as league settings.reserve_slots and
    occupants as each roster's `reserve` list. A full IR means the next
    injury costs an active roster spot — stash value depends on this.
    """
    try:
        slots = int(reserve_slots or 0)
    except (TypeError, ValueError):
        slots = 0
    try:
        used = int(reserve_count or 0)
    except (TypeError, ValueError):
        used = 0
    return slots, max(slots - used, 0)


def ir_line(ir_names, reserve_slots, reserve_count):
    """Roster-audit IR line with capacity and a full-IR warning (E13)."""
    slots, open_ = ir_capacity(reserve_slots, reserve_count)
    names = ", ".join(ir_names) if ir_names else "\u2014"
    s = f"  IR: {names} ({len(ir_names)}/{slots} used, {open_} open)"
    if slots and open_ == 0:
        s += (" — IR FULL: the next injury costs an ACTIVE roster spot; "
              "injured stashes can't hide here")
    return s

def count_flex_slots(roster_positions):
    """Exact 'FLEX' slots only — 'SUPER_FLEX' is a QB slot in disguise.
    (ADV-FF-09: `"FLEX" in "SUPER_FLEX"` modeled 10 starters in snapusa;
    real offensive slots are 9.)"""
    return sum(1 for s in roster_positions if s == "FLEX")

def load_pending_offers(path):
    """Wesley's own open trade offers. Returns {player_id: info} for
    status=pending rows. (ADV-FF-07: engine must not double-commit a player
    he has already offered.)"""
    onblock = {}
    try:
        fh = open(path)
    except FileNotFoundError:
        return onblock
    for line in fh:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 5 and parts[4].lower() == "pending":
            onblock[parts[0]] = {"name": parts[1], "partner": parts[2],
                                 "date": parts[3],
                                 "notes": parts[5] if len(parts) > 5 else ""}
    return onblock

def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "trade-board/1.0"})
    return json.load(urllib.request.urlopen(req, timeout=30))

def norm_name(n):
    n = (n or "").lower().replace(".", "").replace("'", "").replace("-", " ")
    n = re.sub(r"\s+(jr|sr|ii|iii|iv|v)$", "", n)
    return re.sub(r"\s+", " ", n).strip()

def get_fp_ecr(players, ppr):
    """FantasyPros rest-of-season ECR + bye weeks.

    Scraped from the rankings page (embeds `var ecrData`; no auth).
    Returns (fp, fp_week): fp maps sleeper_id -> (bye_week, ecr_rank, tier).
    Raises on fetch/parse failure; caller falls back to empty.
    """
    scoring = "ppr" if ppr >= 1.0 else "half"
    url = (f"https://www.fantasypros.com/nfl/rankings/"
           f"{scoring}-ppr-cheatsheets.php")
    req = urllib.request.Request(url, headers={
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/126.0 Safari/537.36")})
    html = urllib.request.urlopen(req, timeout=30).read().decode(
        "utf-8", "replace")
    m = re.search(r"var ecrData = (\{.*?\});\s*\n", html, re.S)
    d = json.loads(m.group(1))
    name2sid = {}
    def _key(p):
        # prefer active, fantasy-relevant players on name collisions
        return (0 if p.get("active") else 1, p.get("search_rank") or 9999999)
    for pid, p in players.items():
        if isinstance(p, dict) and p.get("full_name"):
            k = norm_name(p["full_name"])
            cur = name2sid.get(k)
            if cur is None or _key(p) < _key(players[cur]):
                name2sid[k] = str(pid)
    fp = {}
    for pl in d.get("players") or []:
        sid = name2sid.get(norm_name(pl.get("player_name")))
        if not sid:
            continue
        try:
            bye = int(pl.get("player_bye_week") or 0) or None
        except (TypeError, ValueError):
            bye = None
        fp[sid] = (bye, pl.get("rank_ecr"), pl.get("tier"))
    return fp, d.get("week")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--league", required=True)
    ap.add_argument("--me", required=True)
    ap.add_argument("--players-cache",
                    default=os.path.expanduser("~/workspace/sleeper/players.json"))
    a = ap.parse_args()

    league = get(f"{SLEEPER}/league/{a.league}")
    rosters = get(f"{SLEEPER}/league/{a.league}/rosters")
    users = get(f"{SLEEPER}/league/{a.league}/users")
    players = json.load(open(a.players_cache))

    num_teams = len(rosters)
    rp = league.get("roster_positions") or []
    is_sf = "SUPER_FLEX" in rp
    _ss = league.get("scoring_settings") or {}
    _st = league.get("settings") or {}
    ppr = float(_ss["rec"] if "rec" in _ss else _st.get("rec", 1.0))
    num_qbs = 2 if is_sf else 1

    # FantasyCalc values matched to league settings
    fc = get(f"{FC}?isDynasty=false&numQbs={num_qbs}&numTeams={num_teams}&ppr={ppr}")
    fval = {}  # sleeper_id -> (value, positionRank, overallRank,
              # trend30Day, displayTrend)  # E7: FC's own "this move matters" flag
    for e in fc:
        p = e.get("player") or {}
        sid = str(p.get("sleeperId") or "")
        if sid:
            fval[sid] = (e.get("redraftValue") or e.get("value") or 0,
                         e.get("positionRank") or 999,
                         e.get("overallRank") or 999, e.get("trend30Day") or 0,
                         bool(e.get("displayTrend")))

    # --- season clock: current NFL week + trade deadline -> posture ---
    try:
        nfl_week = int((get(f"{SLEEPER}/state/nfl") or {}).get("week") or 1)
    except Exception:
        nfl_week = 1
    trade_dl = (league.get("settings") or {}).get("trade_deadline") or 11
    ir_slots = (league.get("settings") or {}).get("reserve_slots") or 0  # E13
    # ADV-FF-14: one canonical boundary shared with fantasy-trade-deadline-stop.
    # Sleeper trade_deadline=N means trades are legal THROUGH NFL Week N;
    # the lock takes effect when the state week rolls to N+1. (Lock semantics
    # are a documented-community convention, not API-verified; if Sleeper
    # ever locks at the start of the deadline week, move the boundary to
    # `nfl_week >= trade_dl` and the stop job one week earlier.)
    trade_review_days = (league.get("settings") or {}).get(
        "trade_review_days", 0)
    if nfl_week > trade_dl:
        posture = "DEADLINE PASSED: waivers only"
    elif nfl_week >= 9:
        posture = ("WIN NOW: maximize rest-of-season + playoff points; "
                   "pay up for certainty; label every deal RENTAL vs KEEPER")
    elif nfl_week >= 5:
        posture = ("BALANCED: fill real starting-lineup needs; start weighing "
                   "playoff-week (W15-17) value in every deal")
    else:
        posture = ("ACQUIRE TALENT: buy roles and talent, not last week's points; "
                   "slow starters with elite roles are buy-lows; "
                   "never rent a one-week wonder")

    # --- FantasyPros rest-of-season ECR + bye weeks (independent cross-check) ---
    try:
        fp, fp_week = get_fp_ecr(players, ppr)
    except Exception:
        fp, fp_week = {}, None

    # --- nflverse bulk data: powers G1 (usage gaps), G5 (playoff SOS
    # weighting) and G6 (handcuff map). Cached 24h; every consumer below
    # degrades to a marked stub when the fetch fails. Route participation
    # is unavailable in every free source — target share + air-yards
    # share + snap% are the route proxies, never synthesized.
    nfl_usage, pweight, nfl_depth, nfl_err, nvp = {}, {}, {}, None, {}
    try:
        nvp = fi.nflverse_paths(keys=("stats", "snaps", "games", "depth"))
        nfl_usage = fi.load_usage(nvp["stats"], nvp["snaps"])
        _pstart = (league.get("settings") or {}).get("playoff_week_start") or 15
        _pteams = {k: (v["team"], v["pos"]) for k, v in nfl_usage.items()}
        pweight = fi.playoff_multipliers(nvp["games"], nvp["stats"], _pteams,
                                         tuple(range(_pstart, 19)))
        nfl_depth = fi.parse_depth_charts(nvp["depth"])
    except Exception as e:  # noqa: BLE001 - degraded paths below
        nfl_err = str(e)[:140]

    nkey_to_sid = {}  # (norm name, team, pos) -> sleeper id
    for _sid, _p in players.items():
        if isinstance(_p, dict) and _p.get("full_name"):
            nkey_to_sid[(norm_name(_p["full_name"]), _p.get("team"),
                         _p.get("position"))] = str(_sid)
    _by_np = {}  # fallback: (norm name, pos) -> sleeper id (team-agnostic)
    for _nk, _sid in nkey_to_sid.items():
        _by_np.setdefault((_nk[0], _nk[2]), _sid)

    def nkey_of_pid(pid):
        _p = players.get(str(pid), {})
        if not isinstance(_p, dict):
            return ("", "", "")
        return (norm_name(_p.get("full_name")), _p.get("team"),
                _p.get("position"))

    def sid_of_nkey(nkey):
        return nkey_to_sid.get(nkey) or _by_np.get((nkey[0], nkey[2]))

    # G5: playoff-schedule value multiplier, active from Week 5 (the point
    # in the season clock where playoff weeks start mattering in deals).
    pw_active = nfl_week >= 5 and bool(pweight)

    def pw_of(pid):
        if not pw_active:
            return 1.0
        return pweight.get(nkey_of_pid(pid), (1.0, ""))[0]

    def ptag_of(pid):
        if not pw_active:
            return ""
        return pweight.get(nkey_of_pid(pid), (1.0, ""))[1]

    def ftrend(sid):
        return fval.get(str(sid), (0, 999, 999, 0, False))[3]

    def fdisplay(sid):
        """E7: FantasyCalc's own 'this move matters' flag (True when the
        30-day trend is big enough for FC to display)."""
        return fval.get(str(sid), (0, 999, 999, 0, False))[4]

    def fstr(sid, val):
        t = ftrend(sid)
        return f"{val}({t:+.0f}/30d)" if t else f"{val}"

    uname = {u["user_id"]: u.get("display_name", "?") for u in users}
    rid2name = {r["roster_id"]: uname.get(r["owner_id"], "?") for r in rosters}
    my_rid = next((r["roster_id"] for r in rosters if r["owner_id"] == a.me), None)

    # --- Wesley's own pending offers: ON-BLOCK players (ADV-FF-07) ---
    pending_path = os.path.normpath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "pending_offers.md"))
    onblock = load_pending_offers(pending_path)

    def pname(pid):
        p = players.get(str(pid), {})
        return p.get("full_name") or str(pid)

    def ppos(pid):
        return (players.get(str(pid), {}).get("position") or "?")

    # startable cutoffs: positional rank that fills league starting slots + flex
    base_slots = {}
    for s in rp:
        if s in ("QB", "RB", "WR", "TE"):
            base_slots[s] = base_slots.get(s, 0) + 1
    flex_slots = count_flex_slots(rp)
    cutoff = {
        "QB": int(num_teams * (2.2 if is_sf else 1.3)),
        "RB": int(num_teams * 2.6),
        "WR": int(num_teams * 3.0),
        "TE": int(num_teams * 1.5),
    }

    teams = {}
    for r in rosters:
        rid = r["roster_id"]
        plist = []
        for pid in (r.get("players") or []):
            pos = ppos(pid)
            v, pr, ovr, _, _ = fval.get(str(pid), (0, 999, 999, 0, False))
            plist.append({"id": str(pid), "name": pname(pid), "pos": pos,
                          "val": v, "prank": pr})
        bypos = {}
        for pl in plist:
            bypos.setdefault(pl["pos"], []).append(pl)
        for v in bypos.values():
            v.sort(key=lambda x: -x["val"])
        startable = {p: sum(1 for pl in bypos.get(p, [])
                            if pl["prank"] <= cutoff[p]) for p in cutoff}
        rs = r.get("settings") or {}
        pf = (rs.get("fpts") or 0) + (rs.get("fpts_decimal") or 0) / 100
        poss = (rs.get("ppts") or 0) + (rs.get("ppts_decimal") or 0) / 100
        teams[rid] = {"name": rid2name[rid], "bypos": bypos,
                      "startable": startable,
                      "ir": [pname(x) for x in (r.get("reserve") or [])],
                      "record": (rs.get("wins") or 0, rs.get("losses") or 0,
                                 rs.get("ties") or 0),
                      "pf": pf, "poss": poss}

    # roster-wide ownership (computed once; used by G1/G3/G6/G8)
    rostered = set()
    owner_of = {}
    for r in rosters:
        _rid = str(r["roster_id"])
        for pid in (r.get("players") or []) + (r.get("reserve") or []):
            rostered.add(str(pid))
            owner_of[str(pid)] = _rid

    # --- trade history (market comps) ---
    print(f"LEAGUE: {league.get('name')} | {num_teams} teams | "
          f"{'Superflex' if is_sf else '1-QB'} | PPR {ppr}")
    print(f"FantasyCalc baseline: redraft, numQbs={num_qbs}, "
          f"numTeams={num_teams}, ppr={ppr}")
    print(f"NFL Week {nfl_week} | trade deadline Week {trade_dl} "
          f"({max(trade_dl - nfl_week, 0)} wks left) | posture: {posture}")
    print(f"Trade lock: trades legal through NFL Week {trade_dl} "
          f"(lock = start of Week {trade_dl + 1}); "
          f"trade review: {trade_review_days} day(s)")
    if nfl_week == trade_dl and trade_review_days:
        print(f"DEADLINE WEEK: deals must be ACCEPTED {trade_review_days}+ "
              f"day(s) before the Week {trade_dl + 1} lock to clear review")
    if fp:
        print(f"FantasyPros rest-of-season ECR (week {fp_week}, "
              f"{len(fp)} players matched)")
    else:
        print("FantasyPros ECR unavailable this run (bye/divergence skipped)")
    print()
    print("=== LEAGUE TRADE HISTORY (market comps) ===")
    ntrades = 0
    all_trades = []  # G2: mined for manager trade profiles below
    # E6: league calibration — the largest accepted value gap this season
    # is the live edge of the fairness bands (bands recalibrate every run
    # as new trades complete).
    calib_gap, calib_desc = 0.0, ""
    # ADV-FF-18: latest drop timestamp per player, from complete add/drop
    # transactions — a drop inside waiver_clear_days is waiver-locked
    # (CLAIM); anything else is an instant FA add.
    recent_drops = {}
    for rnd in range(1, 19):
        try:
            txns = get(f"{SLEEPER}/league/{a.league}/transactions/{rnd}")
        except Exception:
            continue
        for t in txns:
            if t.get("status") == "complete" and t.get("type") in (
                    "free_agent", "waiver"):
                _c = t.get("created") or 0
                if _c:
                    for _pid in (t.get("drops") or {}):
                        _pid = str(_pid)
                        if _c > recent_drops.get(_pid, 0):
                            recent_drops[_pid] = _c
            if t.get("type") != "trade" or t.get("status") != "complete":
                continue
            ntrades += 1
            adds, drops = t.get("adds") or {}, t.get("drops") or {}
            all_trades.append({
                "round": rnd,
                "adds": {str(k): str(v) for k, v in adds.items()},
                "drops": {str(k): str(v) for k, v in drops.items()}})
            # adds: player_id -> roster_id receiving
            recv = {}
            for pid, rid in adds.items():
                recv.setdefault(rid, []).append(pname(pid))
            sides = " | ".join(
                f"{rid2name.get(int(rid), rid)} gets {', '.join(v)}"
                for rid, v in sorted(recv.items(), key=lambda x: int(x[0])))
            print(f"  round {rnd}: {sides}")
            # value check on the deal
            tot = {}
            for pid, rid in adds.items():
                tot[rid] = tot.get(rid, 0) + fval.get(str(pid), (0,))[0]
            if len(tot) == 2:
                rids = sorted(tot, key=int)
                v0, v1 = tot[rids[0]], tot[rids[1]]
                big = max(v0, v1) or 1
                _g = abs(v0-v1)/big  # noqa: E226 - mirrors printed gap
                print(f"    value: {v0} vs {v1} "
                      f"({_g:.0%} gap)")
                if _g > calib_gap:
                    calib_gap, calib_desc = _g, sides
    if not ntrades:
        print("  (no completed trades yet)")
    else:
        print(f"  league calibration: largest accepted gap this season "
              f"{calib_gap:.0%} ({calib_desc}) — fairness bands read "
              f"against this market, not a national chart")
    print()
    print("=== MANAGER TRADE PROFILES (G2: mined from this league's deals) ===")
    try:
        _profs = fi.trade_profiles(
            all_trades, ppos,
            lambda pid: fval.get(str(pid), (0,))[0],
            {str(k): v for k, v in rid2name.items()})
        if not _profs:
            print("  (no completed trades yet — profiles appear after "
                  "the first deal)")
        for _rid in sorted(_profs, key=lambda r: -_profs[r]["n_trades"])[:8]:
            print("  " + fi.profile_line(_profs[_rid]))
    except Exception as e:  # noqa: BLE001 - never break the board
        print(f"  (unavailable: {str(e)[:100]})")
    print()

    # --- team-by-team needs/surplus ---
    # effective starting slots: superflex means ~2 QB slots in practice
    eff_slots = {"QB": 2 if is_sf else 1, "RB": 2, "WR": 2, "TE": 1}

    def need_score(st):
        # positive = thin at the position (needs depth or a starter)
        return {p: (eff_slots[p] + 1) - st["startable"].get(p, 0)
                for p in eff_slots}

    def tradable(st):
        # Startable players beyond effective starters.
        # thins=True: trading it leaves zero depth at the position.
        out = []
        for p in eff_slots:
            pls = [x for x in st["bypos"].get(p, [])
                   if x["prank"] <= cutoff[p]]
            for i, x in enumerate(pls[eff_slots[p]:]):
                out.append((x, i == 0))
        return out

    def hole_creating(st):
        # Trading the last depth/starter at a thin position creates a hole.
        # Returns (player, position) pairs for the persona to price explicitly.
        out = []
        for p in eff_slots:
            pls = [x for x in st["bypos"].get(p, [])
                   if x["prank"] <= cutoff[p]]
            if len(pls) <= eff_slots[p] and pls:
                out.append((pls[-1], p))
        return out

    my_bye_counts = {}  # filled by the bye audit before swaps print

    def _btag(x):
        b = fp.get(x["id"], (None, None, None))[0]
        if not b:
            return ""
        stack = (" [BYE-STACK W%d]" % b) if my_bye_counts.get(b, 0) >= 2 else ""
        return f"[bye {b}]{stack}"

    def show_swap(mine, mflag, theirs, tflag, partner, hole=False):
        gap = abs(mine["val"] - theirs["val"]) / max(
            mine["val"], theirs["val"], 1)
        delta, sits, starts, _ = swap_delta(mine, theirs)
        tag = " [CREATES YOUR %s HOLE]" % mine["pos"] if hole else ""
        thin = " (thins %s)" % ("you" if mflag else "them") if mflag or tflag else ""
        fit = ""
        if starts:
            fit = ("; " + ", ".join(starts) + " starts" +
                   (", " + ", ".join(sits) + " sits" if sits else ""))
        # E7: value range from 30-day volatility + the "this move matters"
        # filter (FC displayTrend = the flat-trend signal).
        mlo, mhi = value_range(mine["val"], ftrend(mine["id"]))
        tlo, thi = value_range(theirs["val"], ftrend(theirs["id"]))
        lsig = low_signal_lateral(fdisplay(mine["id"]),
                                  fdisplay(theirs["id"]), delta,
                                  mine["val"], theirs["val"])
        return (delta, f"  you send {mine['name']} ({mine['val']}, {mlo}-{mhi}){_btag(mine)}"
                       f" -> {partner}; "
                       f"you get {theirs['name']} ({theirs['val']}, {tlo}-{thi}){_btag(theirs)} "
                       f"[lineup {delta:+.0f}{fit}][gap {gap:.0%}|"
                       f"{fairness_band(gap)}]{thin}{tag}{lsig}")

    print("=== TEAM NEEDS (startable vs effective slots) ===")
    print(f"  effective slots: {eff_slots} (+{flex_slots} flex) | "
          f"startable cutoffs (pos rank): {cutoff}")
    order = sorted(teams, key=lambda rid: sum(need_score(teams[rid]).values()))
    for rid in order:
        st = teams[rid]
        ns = need_score(st)
        tag = "  <-- YOU" if rid == my_rid else ""
        line = ", ".join(f"{p}:{st['startable'].get(p,0)}"
                         f"({'!' if ns[p] > 0 else ''})" for p in eff_slots)
        w, l, t = st["record"]
        print(f"  {st['name']}: {line} | {w}-{l}"
              + (f"-{t}" if t else "") +
              f" PF {st['pf']:.0f} poss {st['poss']:.0f}{tag}")
    print("  (!) = thin: no startable depth behind the starters")
    print()
    print("=== PLAYOFF ODDS + SCHEDULE LUCK (G4/G7: weekly snapshot) ===")
    try:
        _pop = os.path.join(fi.GOAL_DIR, "hidden_files", "playoff-odds.md")
        _age = (time.time() - os.stat(_pop).st_mtime) / 3600
        if _age > 72:
            raise FileNotFoundError("stale snapshot")
        _txt = open(_pop).read().splitlines()
        _in, _shown = False, 0
        for _ln in _txt:
            if _ln.startswith("## "):
                _in = (league.get("name", "") in _ln)
                continue
            if _in and ("Wesley playoff probability" in _ln
                        or "<-- YOU" in _ln):
                print("  " + _ln.replace(" <-- YOU", ""))
                _shown += 1
        if not _shown:
            print("  (snapshot has no section for this league yet)")
    except Exception:  # noqa: BLE001 - degraded, never fatal
        print("  (snapshot missing/stale — run: python3 "
              "bin/fantasy_insights.py playoff-odds --league <id> "
              "--me <you> --out <goal>/hidden_files/playoff-odds.md)")
    print()

    # --- your chips & needs ---
    me = teams[my_rid]

    def lineup_ids(bypos):
        """Your projected starting lineup, by FantasyCalc redraft value:
        fill base slots first, then flex slots with the best remaining
        RB/WR/TE (superflex is covered by the 2 QB base slots)."""
        used, starters = set(), []
        for pos in ("QB", "RB", "WR", "TE"):
            for x in sorted(bypos.get(pos, []),
                            key=lambda x: -x["val"])[:eff_slots[pos]]:
                used.add(x["id"])
                starters.append(x)
        pool = [x for pos in ("RB", "WR", "TE") for x in bypos.get(pos, [])
                if x["id"] not in used]
        pool.sort(key=lambda x: -x["val"])
        return starters + pool[:flex_slots]

    my_lineup = lineup_ids(me["bypos"])
    my_lineup_val = sum(x["val"] for x in my_lineup)
    my_lineup_ids = {x["id"] for x in my_lineup}
    print("=== YOUR ROSTER (by value) ===")
    if pw_active:
        print("  [P+]/[P-]: playoff-weeks (W15-17) schedule soft/brutal — "
              "values below are playoff-weighted x0.9-1.1")
    for p in ("QB", "RB", "WR", "TE"):
        pls = me["bypos"].get(p, [])
        print(f"  {p}: " + ", ".join(
            f"{x['name']}({x['val']}){_btag(x)}{ptag_of(x['id'])}"
            + ("[ON-BLOCK: pending offer to %s]" % onblock[x["id"]]["partner"]
               if x["id"] in onblock else "")
            for x in pls))
    if me["ir"] or ir_slots:
        print(ir_line(me["ir"], ir_slots, len(me["ir"])))
    print()
    if onblock:
        print("=== PENDING OFFERS (your open offers — ON-BLOCK: never propose) ===")
        for pid, info in onblock.items():
            print(f"  {info['name']}: offered to {info['partner']} "
                  f"on {info['date']}"
                  + (f" ({info['notes']})" if info["notes"] else ""))
        print()

    print("=== BYE WEEK AUDIT (FantasyPros) ===")
    if fp:
        bye_groups = {}
        for p in eff_slots:
            for x in me["bypos"].get(p, []):
                b = fp.get(x["id"], (None, None, None))[0]
                if b:
                    bye_groups.setdefault(b, []).append(x["name"])
        my_bye_counts = {b: len(v) for b, v in bye_groups.items()}
        for b in sorted(bye_groups):
            names = bye_groups[b]
            flag = "  <-- CLUSTER (avoid adding more)" if len(names) >= 3 else ""
            print(f"  Week {b}: {', '.join(names)}{flag}")
        if not bye_groups:
            print("  (no bye data matched)")
    else:
        print("  (skipped: no FantasyPros data)")
    print()
    print("=== BYE-CRATER FORECAST (G9: next 4 weeks) ===")
    try:
        if not fp:
            raise RuntimeError("no FantasyPros data")
        _byes = {sid: v[0] for sid, v in fp.items() if v[0]}
        _my_ids = [x["id"] for lst in me["bypos"].values() for x in lst]
        _sid2name = {x["id"]: x["name"] for lst in me["bypos"].values()
                     for x in lst}
        _craters = fi.bye_craters(_byes, _my_ids, my_lineup_ids, nfl_week)
        for _wk in sorted(_craters):
            _c = _craters[_wk]
            _sn = [_sid2name.get(s, s) for s in _c["starters"]]
            _flag = "  <-- CRATER (2+ starters out)" if _c["crater"] else ""
            _who = f": {', '.join(_sn)}" if _sn else ""
            _sl = "starter" if len(_sn) == 1 else "starters"
            print(f"  W{_wk}: {_c['total']} of yours on bye "
                  f"({len(_sn)} {_sl}{_who}){_flag}")
    except Exception as e:  # noqa: BLE001
        print(f"  (unavailable: {str(e)[:100]})")
    print()
    print("=== VALUE DIVERGENCE (FantasyPros ECR rank vs FantasyCalc rank) ===")
    if fp:
        divs = []
        for p in eff_slots:
            for x in me["bypos"].get(p, []):
                _, ecr, _ = fp.get(x["id"], (None, None, None))
                fcovr = fval.get(x["id"], (0, 999, 999, 0, False))[2]
                if not ecr or fcovr >= 999:
                    continue
                d = fcovr - ecr
                if abs(d) >= 12:
                    lbl = ("ECR HIGHER (calc undervalues: hold / buy-low window)"
                           if d >= 12 else
                           "CALC HIGHER (sell-high candidate)")
                    divs.append((abs(d),
                                 f"  {x['name']}: ECR {ecr} vs FC #{fcovr}"
                                 f" -> {lbl}"))
        for _, line in sorted(divs, reverse=True)[:10]:
            print(line)
        if not divs:
            print("  (no major divergences on your roster)")
    else:
        print("  (skipped: no FantasyPros data)")
    print()
    print("=== USAGE-GAP RADAR (G1: nflverse usage vs fantasy output) ===")
    print("  routes are unavailable in every free source — target share + "
          "air-yards share + snap% are the route proxies, never synthesized")
    buy_low_sids = set()  # stashed for the G8 clog audit
    try:
        if nfl_err or not nfl_usage:
            raise RuntimeError(nfl_err or "no usage rows")
        _buys, _sells = fi.usage_gaps(nfl_usage)
        _my = {x["id"] for lst in me["bypos"].values() for x in lst}
        buy_low_sids = {sid_of_nkey(b["nkey"]) for b in _buys}
        buy_low_sids.discard(None)
        _hold = [b for b in _buys if sid_of_nkey(b["nkey"]) in _my][:4]
        _shop = [s for s in _sells if sid_of_nkey(s["nkey"]) in _my][:4]
        _tgt, _wire = [], []
        for b in _buys:
            _sid = sid_of_nkey(b["nkey"])
            if _sid in _my or not _sid:
                continue
            (_tgt if _sid in rostered else _wire).append(b)
        def _u1(b):
            return (f"{b['name']} ({b['pos']},{b['team']}) "
                    f"usage-implied {b['exp']} vs actual {b['ppg']} "
                    f"(gap {b['gap']:+.1f}, {b['note']})")
        if _hold:
            print("  HOLD (yours, positive regression due): "
                  + "; ".join(_u1(b) for b in _hold))
        if _shop:
            print("  SHOP (yours, sell the TD luck): "
                  + "; ".join(_u1(s) for s in _shop))
        for b in _tgt[:4]:
            _sid = sid_of_nkey(b["nkey"])
            _mgr = rid2name.get(int(owner_of.get(_sid, -1)), "?")
            print(f"  TARGET (buy the usage from {_mgr}): {_u1(b)}")
        if _wire[:3]:
            print("  WIRE (free agents, buy the usage): "
                  + "; ".join(_u1(b) for b in _wire[:3]))
        if not (_hold or _shop or _tgt or _wire):
            print("  (no major usage gaps this week)")
    except Exception as e:  # noqa: BLE001
        print(f"  (unavailable this run: {str(e)[:110]})")
    print()
    # candidate 1-for-1s: your surplus -> their need, their surplus -> your need
    print("=== CANDIDATE SWAPS (sorted by lineup-points delta) ===")
    print("  ranked by projected lineup-points delta for you; value gap shown "
          "for fairness (bands: <10% EXCELLENT, 10-20% FAIR, 20-35% STRETCH, "
          ">35% UNFAIR); incoming must crack your projected starting lineup")
    print("  value ranges: (point, lo-hi) from 30-day trend volatility "
          "(+/- half the 30d drift); [LOW-SIGNAL LATERAL] = both 30-day "
          "trends flat (FC displayTrend) with a small lineup delta")
    if pw_active:
        print("  G5: deltas use playoff-weighted values (W15-17 SOS x0.9-1.1)")
    my_need = need_score(me)
    _all_tradable = tradable(me)
    my_tradable = [(x, f) for x, f in _all_tradable if x["id"] not in onblock]
    n_blocked = len(_all_tradable) - len(my_tradable)

    def dval_for(x):
        """FantasyCalc value with the E5 injury discount applied
        (ADV-FF-10 static status multiplier + E5 season-timing bracket for
        Out/IR/Doubtful, from this run's NFL week — injured-player values
        visibly decay as the season progresses) and the G5
        playoff-schedule weight (x0.9-1.1 from Week 5)."""
        st = (players.get(str(x["id"]), {}) or {}).get("injury_status") or ""
        return x["val"] * injury_discount(st, nfl_week) * pw_of(x["id"])

    def disc_lineup(bypos):
        return lineup_ids({p: [dict(x, val=dval_for(x)) for x in lst]
                           for p, lst in bypos.items()})

    my_lineup_dval = sum(x["val"] for x in disc_lineup(me["bypos"]))

    def swap_delta(mine, theirs):
        """Lineup-points delta of the swap for you: who starts, who sits.

        Values are injury-discounted (E5/ADV-FF-10); a swap that would push
        a bye week to 4+ projected starters is vetoed, 3 starters take a
        20% penalty (completes E1/ADV-FF-15)."""
        new_bypos = {p: [dict(x, val=dval_for(x)) for x in lst
                         if x["id"] != mine["id"]]
                     for p, lst in me["bypos"].items()}
        new_bypos.setdefault(theirs["pos"], []).append(
            dict(theirs, val=dval_for(theirs)))
        new_lineup = lineup_ids(new_bypos)
        new_ids = {x["id"] for x in new_lineup}
        delta = sum(x["val"] for x in new_lineup) - my_lineup_dval
        bye_veto = False
        tb = fp.get(theirs["id"], (None, None, None))[0]
        if tb:
            others = sum(1 for x in new_lineup if x["id"] != theirs["id"]
                         and fp.get(x["id"], (None, None, None))[0] == tb)
            if others >= 3:
                bye_veto = True  # would push the bye week to 4+ starters
            elif others == 2:
                delta -= 0.20 * dval_for(theirs)  # 3-starter cluster penalty
        sits = [x["name"] for x in my_lineup if x["id"] not in new_ids]
        starts = [x["name"] for x in new_lineup
                  if x["id"] not in my_lineup_ids]
        return delta, sits, starts, bye_veto

    rows = []
    dropped = 0
    bye_dropped = 0
    for rid, st in teams.items():
        if rid == my_rid:
            continue
        t_need = need_score(st)
        for mine, mflag in my_tradable:
            if t_need.get(mine["pos"], 0) <= 0:
                continue  # they don't need it
            for theirs, tflag in tradable(st):
                if my_need.get(theirs["pos"], 0) <= 0:
                    continue  # you don't need it
                delta, _, _, bye_veto = swap_delta(mine, theirs)
                if bye_veto:
                    # ADV-FF-15: incoming would push a bye week to 4+
                    # projected starters — vetoed, not just tagged
                    bye_dropped += 1
                    continue
                if delta <= 0:
                    # P5 fit veto: incoming can't crack his starting
                    # lineup, so equal calc value buys zero lineup gain.
                    dropped += 1
                    continue
                rows.append(show_swap(mine, mflag, theirs, tflag, st["name"]))
    for _, line in sorted(rows, reverse=True)[:12]:
        print(line)
    if not rows:
        print("  (no clean 1-for-1 fits)")
    if dropped:
        print(f"  ({dropped} lateral swap(s) dropped: incoming player "
              f"couldn't crack your projected starting lineup)")
    if bye_dropped:
        print(f"  ({bye_dropped} swap(s) vetoed: incoming player would push "
              f"a bye week to 4+ projected starters)")
    if n_blocked:
        print(f"  ({n_blocked} of your tradable player(s) ON-BLOCK: pending "
              f"offer open, not proposed)")
    print()
    # --- E3: 2-for-1 consolidation (small leagues only) ---
    # WW's core move: two of your surplus starters for one elite. In a
    # 14-team desert depth is currency, so this section only runs where
    # num_teams <= 6. Package pieces come from tradable() surplus (so the
    # deal never creates a hole for you); targets are each partner's elite
    # core. Ranked by lineup-points delta per freed roster slot (a 2-for-1
    # always frees exactly one, which refills from the wire in a 4-team
    # league), with the value gap vs the combined package shown for
    # fairness and the same bye-cluster veto as 1-for-1s.
    if num_teams <= 6:
        print("=== 2-FOR-1 CONSOLIDATION (stars over depth) ===")
        print("  two surplus pieces -> one elite; ranked by lineup-points "
              "delta per freed roster slot (freed slot refills from the "
              "wire); both pieces must be a position they need")

        def swap_delta_2for1(mine_a, mine_b, theirs):
            """Lineup-points delta of sending two players for one.
            Values are injury-discounted (E5/ADV-FF-10); a swap pushing a
            bye week to 4+ projected starters is vetoed, 3 starters take
            a 20% penalty (completes E1/ADV-FF-15)."""
            gone = {mine_a["id"], mine_b["id"]}
            new_bypos = {p: [dict(x, val=dval_for(x)) for x in lst
                             if x["id"] not in gone]
                         for p, lst in me["bypos"].items()}
            new_bypos.setdefault(theirs["pos"], []).append(
                dict(theirs, val=dval_for(theirs)))
            new_lineup = lineup_ids(new_bypos)
            new_ids = {x["id"] for x in new_lineup}
            delta = sum(x["val"] for x in new_lineup) - my_lineup_dval
            bye_veto = False
            tb = fp.get(theirs["id"], (None, None, None))[0]
            if tb:
                others = sum(1 for x in new_lineup if x["id"] != theirs["id"]
                             and fp.get(x["id"], (None, None, None))[0] == tb)
                if others >= 3:
                    bye_veto = True
                elif others == 2:
                    delta -= 0.20 * dval_for(theirs)
            sits = [x["name"] for x in my_lineup if x["id"] not in new_ids]
            starts = [x["name"] for x in new_lineup
                      if x["id"] not in my_lineup_ids]
            return delta, sits, starts, bye_veto

        def show_2for1(a, b, theirs, partner, thins_them):
            da, db, dt = dval_for(a), dval_for(b), dval_for(theirs)
            gap = abs((da + db) - dt) / max(da + db, dt, 1)
            delta, sits, _, _ = swap_delta_2for1(a, b, theirs)
            tag = " [THINS THEM]" if thins_them else ""
            fit = ("; " + theirs["name"] + " starts"
                   + (", " + ", ".join(sits) + " sits" if sits else ""))
            # E7: value ranges from 30-day volatility, same as 1-for-1s.
            alo, ahi = value_range(a["val"], ftrend(a["id"]))
            blo, bhi = value_range(b["val"], ftrend(b["id"]))
            tlo, thi = value_range(theirs["val"], ftrend(theirs["id"]))
            return (delta,
                    f"  you send {a['name']} ({a['val']}, {alo}-{ahi}) + "
                    f"{b['name']} ({b['val']}, {blo}-{bhi}){_btag(a)}{_btag(b)} -> "
                    f"{partner}; you get {theirs['name']} "
                    f"({theirs['val']}, {tlo}-{thi}){_btag(theirs)} "
                    f"[lineup {delta:+.0f}{fit}][gap vs combined "
                    f"{gap:.0%}|{fairness_band(gap)}][frees 1 slot]{tag}")

        package = sorted(my_tradable, key=lambda t: -t[0]["val"])[:12]
        rows2, bye_dropped2 = [], 0
        for i in range(len(package)):
            for j in range(i + 1, len(package)):
                (pa, _), (pb, _) = package[i], package[j]
                for rid, st in teams.items():
                    if rid == my_rid:
                        continue
                    t_need = need_score(st)
                    if t_need.get(pa["pos"], 0) <= 0 or \
                       t_need.get(pb["pos"], 0) <= 0:
                        continue  # they don't need both pieces
                    elite = sorted(
                        [x for p in eff_slots for x in st["bypos"].get(p, [])
                         if x["prank"] <= cutoff[p]],
                        key=lambda x: -dval_for(x))[:6]
                    thin_ids = {x["id"] for x, _ in hole_creating(st)}
                    da, db = dval_for(pa), dval_for(pb)
                    for theirs in elite:
                        dt = dval_for(theirs)
                        if dt <= 0 or dt < max(da, db) * 1.15:
                            continue  # not a tier-break upgrade
                        delta, _, _, veto = swap_delta_2for1(pa, pb, theirs)
                        if veto:
                            bye_dropped2 += 1
                            continue
                        if delta <= 0:
                            continue  # fit veto: no lineup gain
                        rows2.append(show_2for1(
                            pa, pb, theirs, st["name"],
                            theirs["id"] in thin_ids))
        for _, line in sorted(rows2, reverse=True)[:5]:
            print(line)
        if not rows2:
            print("  (no 2-for-1 consolidation fits: your surplus doesn't "
                  "buy an elite's lineup gain)")
        if bye_dropped2:
            print(f"  ({bye_dropped2} 2-for-1(s) vetoed: incoming elite "
                  f"would push a bye week to 4+ projected starters)")
        print()
    print("=== HOLE-CREATING OPTIONS (persona must price the roster cost) ===")
    for mine, mp in hole_creating(me):
        if mine["id"] in onblock:
            continue  # committed to a pending offer; not shoppable
        suitors = [st["name"] for rid, st in teams.items()
                   if rid != my_rid and need_score(st).get(mp, 0) > 0]
        if suitors:
            print(f"  {mine['name']} ({mine['val']}) -> "
                  f"{', '.join(suitors)} need {mp}; "
                  f"trading him leaves you with "
                  f"{me['startable'].get(mp, 0) - 1} startable {mp}")
    print()
    # E8 weekly handcuff audit: RB-only by design (see fi.handcuff_map
    # docstring) — the handcuff is a contingent-value concept; only RB
    # backups inherit a full workload. Header text below is byte-frozen
    # (a downstream Tuesday worker parses it); the per-line rendering
    # lives in fi.render_handcuff_lines so the free->UNPROTECTED flag
    # logic is unit-testable.
    print("=== HANDCUFF LEVERAGE MAP (G6: each of your RBs' direct backup) ===")
    hm_cache = []  # stashed for the G8 clog audit
    try:
        if not nfl_depth:
            raise RuntimeError(nfl_err or "empty depth chart")
        _my_rbs = [x["id"] for x in me["bypos"].get("RB", [])]
        _rids = {str(k): v for k, v in rid2name.items()}
        _rost_ids = {str(r["roster_id"]):
                     [str(p) for p in (r.get("players") or [])]
                     for r in rosters}
        hm_cache = fi.handcuff_map(nfl_depth, _my_rbs, _rost_ids, players,
                                   _rids, my_rid)
        if not hm_cache:
            print("  (no RBs on your roster)")
        for _line in fi.render_handcuff_lines(hm_cache):
            print(_line)
    except Exception as e:  # noqa: BLE001 - snap-share fallback, then stub
        _fb = []
        try:
            for _x in me["bypos"].get("RB", [])[:6]:
                _t = (players.get(_x["id"], {}) or {}).get("team")
                if _t and nvp.get("snaps"):
                    _fb.append((_x["name"], _t,
                                fi.infer_backups_from_snaps(nvp["snaps"],
                                                            _t)))
        except Exception:  # noqa: BLE001
            _fb = []
        if _fb:
            print("  (depth-chart source thin — backups INFERRED from "
                  "snap share, not a real depth chart)")
            for _nm, _t, _backs in _fb:
                print(f"  {_nm} ({_t}): "
                      + ", ".join(f"{n} ({s} snaps/g)" for n, s, _ in _backs))
        else:
            print(f"  (unavailable: {str(e)[:100]})")
    print()
    # --- waiver wire ---
    # Free agents ranked by settings-matched FantasyCalc value, because the
    # wire is a different game by league size: an ocean in 4-team leagues
    # (replacement level is high, stream freely) and a desert in 14-team
    # leagues (only startable FAs at thin positions matter).
    # WPOS (incl. K/DEF) and HURT live at module level (ADV-FF-06).
    print("=== WAIVER WIRE ===")
    if num_teams <= 6:
        print("  small league: wire is rich — stream QB/TE/K/DEF, drop freely, "
              "never pay trade value for depth")
    else:
        print("  deep league: wire is thin — only startable FAs at thin "
              "positions matter; never drop a startable asset")
    fa_by_pos = {}
    for pid, p in players.items():
        if not isinstance(p, dict):
            continue
        pos = p.get("position")
        if pos not in WPOS or not p.get("active"):
            continue
        if str(pid) in rostered:
            continue
        pid_s = str(pid)
        v = fval.get(pid_s, (0, 999, 999, 0, False))[0]
        inj = p.get("injury_status") or ""
        fa_by_pos.setdefault(pos, []).append(
            (v, p.get("full_name") or pid_s, inj, pid_s))
    for pos in WPOS:
        fa_by_pos.setdefault(pos, []).sort(reverse=True)
        def _fa_fmt(v, n, inj, pid_s):
            # K/DEF have no value feed (FantasyCalc omits them; Sleeper
            # projections are empty) — label them unpriced, not zero.
            val = fstr(pid_s, v) if (v or pos not in ("K", "DEF")) else "unpriced"
            return (f"{n}({val}){_btag({'id': pid_s})}"
                    + (f"[!{inj}]" if inj else ""))
        top = ", ".join(_fa_fmt(v, n, inj, pid_s)
                        for v, n, inj, pid_s in fa_by_pos[pos][:4])
        print(f"  top FA {pos}: {top or '(none)'}")
    # G3: trending velocity — exactly ONE Sleeper call per scan; velocity
    # is derived from our own timestamped snapshot history (this scan's
    # 24h adds vs adds over the gap since the previous scan).
    _vel_rows = []
    try:
        _counts = {str(t.get("player_id")): t.get("count") or 0
                   for t in fi.fetch_trending()}
        _snaps = fi.load_trend_snapshots()
        _prev = _snaps[-1] if _snaps else None
        fi.save_trend_snapshot(_counts)
        _vel_rows = fi.trending_velocity(_counts, _prev, players, rostered)
    except Exception:
        pass
    tfa = []
    for _row in _vel_rows:
        if _row["pos"] not in WPOS:
            continue
        _pid = _row["sid"]
        v = fval.get(_pid, (0, 999, 999, 0, False))[0]
        inj = (players.get(_pid, {}) or {}).get("injury_status") or ""
        _vel = (f"x{_row['accel']} vs {_row['base']} {_row['tag']}"
                if _row["accel"] is not None else "NEW")
        tfa.append(
            f"{_row['name']}({fstr(_pid, v)},+{_row['c24']}/24h,{_vel})"
            + (f"[!{inj}]" if inj in HURT else ""))
        if len(tfa) >= 8:
            break
    print(f"  trending velocity (Sleeper-wide adds, 24h vs previous scan): "
          f"{', '.join(tfa) if tfa else '(unavailable this run)'}")
    bench = []
    for p in eff_slots:
        bench.extend(me["bypos"].get(p, [])[eff_slots[p]:])
    bench.sort(key=lambda x: x["val"])
    # ADV-FF-18: every suggested ADD is labeled FA NOW (instant add, no
    # priority cost) or CLAIM (waiver-locked: clears Tue, burns his slot)
    # from the recent-drop timestamps — Wesley's 9/23 "FA or claim?" ask.
    _clear_days = (league.get("settings") or {}).get("waiver_clear_days", 2)
    _now_ms = int(time.time() * 1000)
    _clear_lbl = clear_label(league.get("settings"))
    _waiver_slot = waiver_slot(rosters, a.me)

    def _path(fapid):
        return acquisition_path(fapid, recent_drops, _clear_days, _now_ms)

    def _pathtag(path):
        if path == "claim":
            return f" [CLAIM — {_clear_lbl}, burns #{_waiver_slot}]"
        return " [FA NOW — instant add, no priority cost]"

    # E14 follow-up: slot math must be known BEFORE suggestions emit — on a
    # FULL roster the DROP must be an active player, since dropping an
    # IR/reserve occupant frees no active bench slot.
    _my = next((r for r in rosters if r.get("owner_id") == a.me), None)
    _sm = slot_math(_my, rp, ir_slots)
    _drop_pool = legal_drops(bench, _sm["reserve_ids"], _sm["drop_needed"])

    sug = []
    for pos in WPOS:
        if not fa_by_pos.get(pos):
            continue
        fav, fan, fainj, fapid = fa_by_pos[pos][0]
        if fav <= 0 or fainj in HURT:
            continue  # never suggest adding an injured player
        if pos in ("K", "DEF"):
            # ADV-FF-06: the natural drop for a K/DEF add is your current
            # unit at that position, not a QB/RB/WR/TE bench player.
            # No value feed prices K/DEF, so the value gate only fires if
            # one ever appears; the fallback is a health signal — a hurt
            # starter must be replaced from the top healthy FAs.
            # The drop pool honors the FULL-roster rule (reserve occupants
            # can't free an active slot).
            mine = legal_drops(sorted(me["bypos"].get(pos, []),
                                      key=lambda x: x["val"]),
                               _sm["reserve_ids"], _sm["drop_needed"])
            if mine and fav > mine[0]["val"] * CHURN_THRESHOLD:
                _p = _path(fapid)
                sug.append((fav - mine[0]["val"], fav, mine[0]["val"],
                            False, fapid, _p,
                            f"  ADD {fan} ({pos},{fstr(fapid, fav)}) / "
                            f"DROP {mine[0]['name']} ({pos},{mine[0]['val']})"
                            + _pathtag(_p)))
            elif mine and not fainj:
                cur = mine[0]
                cur_inj = ((players.get(cur["id"], {}) or {})
                           .get("injury_status") or "")
                if cur_inj in HURT:
                    # forced replacement: a hurt starter must be replaced —
                    # edge is 0 but NF-01 never flags a forced move
                    _p = _path(fapid)
                    sug.append((0, 0, cur["val"], True, fapid, _p,
                                f"  ADD {fan} ({pos}) / "
                                f"DROP {cur['name']} ({pos}) — "
                                f"your {pos} is {cur_inj}"
                                + _pathtag(_p)))
            continue
        for b in _drop_pool:
            if fav > b["val"] * CHURN_THRESHOLD:
                _p = _path(fapid)
                sug.append((fav - b["val"], fav, b["val"], False, fapid, _p,
                            f"  ADD {fan} ({pos},{fstr(fapid, fav)}) / "
                            f"DROP {b['name']} ({b['pos']},{b['val']})"
                            + _pathtag(_p)))
                break
    sug.sort(key=lambda t: t[0], reverse=True)
    # E14: validate slot arithmetic before emitting — the 9/23 Rodgers
    # "no drop needed" advice was built on a bad roster read (Pacheco was
    # already in reserve; the roster was full). Live counts, not memory.
    # (_sm computed above, before suggestion generation.)
    _res_names = ", ".join(pname(p) for p in _sm["reserve_ids"]) or "—"
    _slot_state = ("FULL: every ADD needs a DROP" if _sm["drop_needed"]
                   else (f"{_sm['bench_max'] - _sm['active']} open bench "
                         "slot(s): adds need no drop"))
    print(f"  slot check: active {_sm['active']}/{_sm['bench_max']} — "
          f"{_slot_state} | IR: {_res_names} ({len(_sm['reserve_ids'])}/"
          f"{_sm['ir_slots']} used, {_sm['ir_open']} open)")
    print("  suggested moves:")
    for _, _, _, _, _, _, line in sug[:5]:
        print(line)
    if not sug:
        print("  (no FA beats your droppable bench)")
    print("  roster-clog audit (G8: bench by contingent value, low = droppable):")
    try:
        _ball = []
        for _pos, _lst in me["bypos"].items():
            if _pos not in ("QB", "RB", "WR", "TE", "K", "DEF"):
                continue
            for _b in _lst:
                if _b["id"] in my_lineup_ids:
                    continue  # starters aren't clog
                _bp = players.get(_b["id"], {}) or {}
                _ball.append({"sid": _b["id"], "name": _b["name"],
                              "pos": _b["pos"], "val": _b["val"],
                              "inj": _bp.get("injury_status") or ""})
        _hcuff = {b["sid"] for h in hm_cache for b in h["backups"]
                  if b.get("status") == "mine" and b.get("sid")}
        _ahead = {}
        for _b in _ball:
            for _s in my_lineup:
                if _s["pos"] == _b["pos"] and _s["val"] > _b["val"]:
                    _si = ((players.get(_s["id"], {}) or {})
                           .get("injury_status") or "")
                    if _si in HURT:
                        _ahead[_b["sid"]] = True
                        break
        _ranked, _drops = fi.clog_audit(
            _ball, handcuff_of=_hcuff, ahead_out=_ahead,
            rising={s: True for s in buy_low_sids},
            onblock=set(onblock))
        for _r in _ranked[:6]:
            _tags = ("[handcuff]" if _r["sid"] in _hcuff else "") + \
                    ("[ahead-out]" if _r["sid"] in _ahead else "") + \
                    ("[rising]" if _r["sid"] in buy_low_sids else "")
            print(f"    {_r['name']} ({_r['pos']},{_r['val']} -> "
                  f"contingent {_r['contingent']}){_tags}")
        if _drops:
            print(f"    DROP CANDIDATES: {', '.join(_drops)}")
    except Exception as e:  # noqa: BLE001
        print(f"    (unavailable: {str(e)[:100]})")
    # --- NF-01: waiver priority cost ---
    slot = _waiver_slot  # computed above (ADV-FF-18 needs it for CLAIM tags)
    print()
    print("=== WAIVER PRIORITY COST ===")
    if slot is None:
        print("  (waiver slot unknown: Wesley's roster not found)")
    else:
        scarce = slot <= SCARCE_SLOT_CUTOFF
        print(f"  {league.get('name')} waiver slot: {slot} of {num_teams}"
              + (" — SCARCE" if scarce else ""))
        v_edges = [e for e, _, _, forced, _, _, _ in sug if not forced]
        typical = (sorted(v_edges)[len(v_edges) // 2] if v_edges
                   else TYPICAL_EDGE_FALLBACK)
        p, hv = hold_value(slot, num_teams, typical)
        print("  hold-vs-spend: P(better target emerges before the Tuesday "
              f"reset) = {p:.2f} x typical edge {typical:.1f} "
              f"-> hold value {hv:.1f}")
        print(f"    (P = {BASE_EMERGE} base x "
              f"{slot_scarcity(slot, num_teams):.2f} slot scarcity x "
              f"{wire_richness(num_teams):.1f} wire richness)")
        if not sug:
            print("  (no suggested moves to price)")
        for edge, _, dropv, forced, _, path, line in sug[:5]:
            if path == "fa":
                # Wesley's 9/23 ruling: price priority only when a claim
                # is actually required — FA-now adds cost nothing.
                print(f"{line}\n    -> FA NOW: no priority cost")
                continue
            verdict = slot_verdict(edge, dropv, slot, num_teams, forced)
            if verdict:
                print(f"{line}\n    -> {verdict}")
            elif forced:
                print(f"{line} (forced replacement: spend regardless)")
            else:
                print(f"{line} (edge {edge:.1f} vs hold {hv:.1f}: "
                      "spend justified)")
    print()
    print("(Persona applies judgment on top: roster-context vetoes, "
          "both-sides motivation, market comps above.)")
    for rid, st in teams.items():
        if rid == my_rid:
            continue
        t_need = need_score(st)
if __name__ == "__main__":
    sys.exit(main())

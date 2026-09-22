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
import argparse, json, os, re, sys, urllib.request

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

def injury_discount(status):
    """Value multiplier for an injury designation (E5)."""
    return INJURY_DISCOUNT.get(status or "", 1.0)

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
    fval = {}  # sleeper_id -> (value, positionRank, overallRank, trend30Day)
    for e in fc:
        p = e.get("player") or {}
        sid = str(p.get("sleeperId") or "")
        if sid:
            fval[sid] = (e.get("redraftValue") or e.get("value") or 0,
                         e.get("positionRank") or 999,
                         e.get("overallRank") or 999, e.get("trend30Day") or 0)

    # --- season clock: current NFL week + trade deadline -> posture ---
    try:
        nfl_week = int((get(f"{SLEEPER}/state/nfl") or {}).get("week") or 1)
    except Exception:
        nfl_week = 1
    trade_dl = (league.get("settings") or {}).get("trade_deadline") or 11
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

    def ftrend(sid):
        return fval.get(str(sid), (0, 999, 999, 0))[3]

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
            v, pr, ovr, _ = fval.get(str(pid), (0, 999, 999, 0))
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
    for rnd in range(1, 19):
        try:
            txns = get(f"{SLEEPER}/league/{a.league}/transactions/{rnd}")
        except Exception:
            continue
        for t in txns:
            if t.get("type") != "trade" or t.get("status") != "complete":
                continue
            ntrades += 1
            adds, drops = t.get("adds") or {}, t.get("drops") or {}
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
                print(f"    value: {v0} vs {v1} "
                      f"({abs(v0-v1)/big:.0%} gap)")
    if not ntrades:
        print("  (no completed trades yet)")
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
        return (delta, f"  you send {mine['name']} ({mine['val']}){_btag(mine)}"
                       f" -> {partner}; "
                       f"you get {theirs['name']} ({theirs['val']}){_btag(theirs)} "
                       f"[lineup {delta:+.0f}{fit}][gap {gap:.0%}]{thin}{tag}")

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

    # --- your chips & needs ---
    me = teams[my_rid]
    print("=== YOUR ROSTER (by value) ===")
    for p in ("QB", "RB", "WR", "TE"):
        pls = me["bypos"].get(p, [])
        print(f"  {p}: " + ", ".join(
            f"{x['name']}({x['val']}){_btag(x)}"
            + ("[ON-BLOCK: pending offer to %s]" % onblock[x["id"]]["partner"]
               if x["id"] in onblock else "")
            for x in pls))
    if me["ir"]:
        print(f"  IR: {', '.join(me['ir'])}")
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
    print("=== VALUE DIVERGENCE (FantasyPros ECR rank vs FantasyCalc rank) ===")
    if fp:
        divs = []
        for p in eff_slots:
            for x in me["bypos"].get(p, []):
                _, ecr, _ = fp.get(x["id"], (None, None, None))
                fcovr = fval.get(x["id"], (0, 999, 999, 0))[2]
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
    # candidate 1-for-1s: your surplus -> their need, their surplus -> your need
    print("=== CANDIDATE SWAPS (sorted by lineup-points delta) ===")
    print("  ranked by projected lineup-points delta for you; value gap shown "
          "for fairness; incoming must crack your projected starting lineup")
    my_need = need_score(me)
    _all_tradable = tradable(me)
    my_tradable = [(x, f) for x, f in _all_tradable if x["id"] not in onblock]
    n_blocked = len(_all_tradable) - len(my_tradable)

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

    def dval_for(x):
        """FantasyCalc value with the E5 injury discount applied
        (ADV-FF-10: stale values must not rank first)."""
        st = (players.get(str(x["id"]), {}) or {}).get("injury_status") or ""
        return x["val"] * injury_discount(st)

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
    rostered = set()
    for r in rosters:
        for pid in (r.get("players") or []) + (r.get("reserve") or []):
            rostered.add(str(pid))
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
        v = fval.get(pid_s, (0, 999, 999, 0))[0]
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
    try:
        trending = get(f"{SLEEPER}/players/nfl/trending/add"
                       "?lookback_hours=24&limit=25")
    except Exception:
        trending = []
    tfa = []
    for t in trending:
        pid = str(t.get("player_id"))
        if pid in rostered:
            continue
        p = players.get(pid, {})
        if p.get("position") not in WPOS:
            continue
        v = fval.get(pid, (0, 999, 999, 0))[0]
        inj = p.get("injury_status") or ""
        tfa.append(f"{p.get('full_name') or pid}({fstr(pid, v)},+{t.get('count')})"
                   + (f"[!{inj}]" if inj in HURT else ""))
    print(f"  trending FA adds (24h): {', '.join(tfa) or '(none)'}")
    bench = []
    for p in eff_slots:
        bench.extend(me["bypos"].get(p, [])[eff_slots[p]:])
    bench.sort(key=lambda x: x["val"])
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
            mine = sorted(me["bypos"].get(pos, []), key=lambda x: x["val"])
            if mine and fav > mine[0]["val"] * CHURN_THRESHOLD:
                sug.append((fav - mine[0]["val"], fav, mine[0]["val"], False,
                            f"  ADD {fan} ({pos},{fstr(fapid, fav)}) / "
                            f"DROP {mine[0]['name']} ({pos},{mine[0]['val']})"))
            elif mine and not fainj:
                cur = mine[0]
                cur_inj = ((players.get(cur["id"], {}) or {})
                           .get("injury_status") or "")
                if cur_inj in HURT:
                    # forced replacement: a hurt starter must be replaced —
                    # edge is 0 but NF-01 never flags a forced move
                    sug.append((0, 0, cur["val"], True,
                                f"  ADD {fan} ({pos}) / "
                                f"DROP {cur['name']} ({pos}) — "
                                f"your {pos} is {cur_inj}"))
            continue
        for b in bench:
            if fav > b["val"] * CHURN_THRESHOLD:
                sug.append((fav - b["val"], fav, b["val"], False,
                            f"  ADD {fan} ({pos},{fstr(fapid, fav)}) / "
                            f"DROP {b['name']} ({b['pos']},{b['val']})"))
                break
    sug.sort(key=lambda t: t[0], reverse=True)
    print("  suggested moves:")
    for _, _, _, _, line in sug[:5]:
        print(line)
    if not sug:
        print("  (no FA beats your droppable bench)")
    # --- NF-01: waiver priority cost ---
    slot = waiver_slot(rosters, a.me)
    print()
    print("=== WAIVER PRIORITY COST ===")
    if slot is None:
        print("  (waiver slot unknown: Wesley's roster not found)")
    else:
        scarce = slot <= SCARCE_SLOT_CUTOFF
        print(f"  {league.get('name')} waiver slot: {slot} of {num_teams}"
              + (" — SCARCE" if scarce else ""))
        v_edges = [e for e, _, _, forced, _ in sug if not forced]
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
        for edge, _, dropv, forced, line in sug[:5]:
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

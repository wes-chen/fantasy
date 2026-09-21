#!/usr/bin/env python3
"""Fantasy trade board: league-wide needs/surplus + candidate deals.

Pulls live Sleeper rosters, FantasyCalc redraft values (settings-matched:
superflex/QB count, team count, PPR), and the league's completed trade
history as market comps. Prints a readable board; the analyst (persona)
turns it into recommendations.

Usage:
  trade_board.py --league <league_id> --me <user_id>
                 [--players-cache ~/workspace/sleeper/players.json]
"""
import argparse, json, os, sys, urllib.request

SLEEPER = "https://api.sleeper.app/v1"
FC = "https://api.fantasycalc.com/values/current"

def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "trade-board/1.0"})
    return json.load(urllib.request.urlopen(req, timeout=30))

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
    ppr = float((league.get("scoring_settings") or {}).get("rec", 1.0))
    num_qbs = 2 if is_sf else 1

    # FantasyCalc values matched to league settings
    fc = get(f"{FC}?isDynasty=false&numQbs={num_qbs}&numTeams={num_teams}&ppr={ppr}")
    fval = {}  # sleeper_id -> (value, positionRank, overallRank)
    for e in fc:
        p = e.get("player") or {}
        sid = str(p.get("sleeperId") or "")
        if sid:
            fval[sid] = (e.get("value") or 0, e.get("positionRank") or 999,
                         e.get("overallRank") or 999)

    uname = {u["user_id"]: u.get("display_name", "?") for u in users}
    rid2name = {r["roster_id"]: uname.get(r["owner_id"], "?") for r in rosters}
    my_rid = next((r["roster_id"] for r in rosters if r["owner_id"] == a.me), None)

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
    flex_slots = sum(1 for s in rp if "FLEX" in s)
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
            v, pr, ovr = fval.get(str(pid), (0, 999, 999))
            plist.append({"id": str(pid), "name": pname(pid), "pos": pos,
                          "val": v, "prank": pr})
        bypos = {}
        for pl in plist:
            bypos.setdefault(pl["pos"], []).append(pl)
        for v in bypos.values():
            v.sort(key=lambda x: -x["val"])
        startable = {p: sum(1 for pl in bypos.get(p, [])
                            if pl["prank"] <= cutoff[p]) for p in cutoff}
        teams[rid] = {"name": rid2name[rid], "bypos": bypos,
                      "startable": startable,
                      "ir": [pname(x) for x in (r.get("reserve") or [])]}

    # --- trade history (market comps) ---
    print(f"LEAGUE: {league.get('name')} | {num_teams} teams | "
          f"{'Superflex' if is_sf else '1-QB'} | PPR {ppr}")
    print(f"FantasyCalc baseline: redraft, numQbs={num_qbs}, "
          f"numTeams={num_teams}, ppr={ppr}")
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

    def show_swap(mine, mflag, theirs, tflag, partner, hole=False):
        gap = abs(mine["val"] - theirs["val"]) / max(
            mine["val"], theirs["val"], 1)
        tag = " [CREATES YOUR %s HOLE]" % mine["pos"] if hole else ""
        thin = " (thins %s)" % ("you" if mflag else "them") if mflag or tflag else ""
        return (gap, f"  you send {mine['name']} ({mine['val']}) -> {partner}; "
                     f"you get {theirs['name']} ({theirs['val']}) "
                     f"[gap {gap:.0%}]{thin}{tag}")

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
        print(f"  {st['name']}: {line}{tag}")
    print("  (!) = thin: no startable depth behind the starters")
    print()

    # --- your chips & needs ---
    me = teams[my_rid]
    print("=== YOUR ROSTER (by value) ===")
    for p in ("QB", "RB", "WR", "TE"):
        pls = me["bypos"].get(p, [])
        print(f"  {p}: " + ", ".join(
            f"{x['name']}({x['val']})" for x in pls))
    if me["ir"]:
        print(f"  IR: {', '.join(me['ir'])}")
    print()

    # candidate 1-for-1s: your surplus -> their need, their surplus -> your need
    print("=== CANDIDATE SWAPS (sorted by value gap) ===")
    my_need = need_score(me)
    my_tradable = tradable(me)
    rows = []
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
                rows.append(show_swap(mine, mflag, theirs, tflag, st["name"]))
    for _, line in sorted(rows)[:12]:
        print(line)
    if not rows:
        print("  (no clean 1-for-1 fits)")
    print()
    print("=== HOLE-CREATING OPTIONS (persona must price the roster cost) ===")
    for mine, mp in hole_creating(me):
        suitors = [st["name"] for rid, st in teams.items()
                   if rid != my_rid and need_score(st).get(mp, 0) > 0]
        if suitors:
            print(f"  {mine['name']} ({mine['val']}) -> "
                  f"{', '.join(suitors)} need {mp}; "
                  f"trading him leaves you with "
                  f"{me['startable'].get(mp, 0) - 1} startable {mp}")
    print()
    print("(Persona applies judgment on top: roster-context vetoes, "
          "both-sides motivation, market comps above.)")
    for rid, st in teams.items():
        if rid == my_rid:
            continue
        t_need = need_score(st)
if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""fantasy_insights.py — G1-G10 advanced analytics for Wesley's trade desk.

Companion to bin/trade_board.py. All analytics are PURE functions (no
network at import time) so the golden tests can pin them with fixtures.
Network lives in fetch_* helpers; nflverse bulk files are cached under
~/workspace/goals/fantasy-football-2026-season-monitoring/hidden_files/nflverse/
with a 24h TTL.

Feature map:
  G1 usage-gap radar        usage_gaps()        <- nflverse stats+snap counts
  G2 manager trade profiles trade_profiles()    <- Sleeper transactions
  G3 trending velocity      trending_velocity() <- ONE Sleeper trending/add
       call per scan; velocity derived from timestamped snapshot history
  G4 playoff-odds Monte Carlo simulate_playoffs() <- records + schedules
  G5 playoff-week weighting playoff_multipliers() <- nflverse games.csv + defense
  G6 handcuff leverage map  handcuff_map()      <- nflverse depth charts
  G7 schedule-luck audit    schedule_luck()     <- Sleeper matchups
  G8 roster-clog audit      clog_audit()        <- bench + G1/G6 context
  G9 bye-crater forecast    bye_craters()       <- FantasyPros byes
  G10 betting cross-check   fetch_week_odds() + start_sit_signals()
                            <- ESPN core API (DraftKings, game-level only)

Rules: read-only everything; never fake data — every fetch helper raises
on failure and every caller prints a clearly-marked degraded line.
Route participation is NOT available in any free source (checked nflverse,
ftn_charting, PFR — paywalled at PFF); target_share + air_yards_share +
offense_pct are used as the route proxies and routes are labeled
unavailable, never synthesized.
"""
import argparse
import csv
import gzip
import json
import math
import os
import random
import re
import sys
import time
import urllib.request
import urllib.error

SLEEPER = "https://api.sleeper.app/v1"
ESPN_CORE = "https://sports.core.api.espn.com/v2/sports/football/leagues/nfl"
NFLVERSE = {
    "stats": ("https://github.com/nflverse/nflverse-data/releases/download/"
              "stats_player/stats_player_week_2026.csv.gz"),
    "snaps": ("https://github.com/nflverse/nflverse-data/releases/download/"
              "snap_counts/snap_counts_2026.csv"),
    "depth": ("https://github.com/nflverse/nflverse-data/releases/download/"
              "depth_charts/depth_charts_2026.csv"),
    "games": ("https://github.com/nflverse/nflverse-data/releases/download/"
              "schedules/games.csv.gz"),
}
GOAL_DIR = os.path.expanduser(
    "~/workspace/goals/fantasy-football-2026-season-monitoring")
CACHE_DIR = os.path.join(GOAL_DIR, "hidden_files", "nflverse")
UA = {"User-Agent": "fantasy-insights/1.0"}

# ---------------------------------------------------------------- shared

def norm_name(n):
    n = (n or "").lower().replace(".", "").replace("'", "").replace("-", " ")
    n = re.sub(r"\s+(jr|sr|ii|iii|iv|v)$", "", n)
    return re.sub(r"\s+", " ", n).strip()


def fetch_json(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    return json.load(urllib.request.urlopen(req, timeout=timeout))


def download(url, dest, timeout=120):
    """Download a URL to dest. Raises on failure (caller degrades)."""
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r, \
            open(dest, "wb") as fh:
        fh.write(r.read())


def nflverse_paths(cache_dir=CACHE_DIR, ttl_hours=24, keys=None):
    """Ensure nflverse bulk files are cached; download when stale/missing.

    Returns {key: path}. Raises RuntimeError listing what failed — the
    caller must degrade, never fake.
    """
    os.makedirs(cache_dir, exist_ok=True)
    names = {"stats": "stats_player_week_2026.csv.gz",
             "snaps": "snap_counts_2026.csv",
             "depth": "depth_charts_2026.csv",
             "games": "games.csv.gz"}
    out, failed = {}, []
    for key in (keys or names):
        dest = os.path.join(cache_dir, names[key])
        stale = (not os.path.exists(dest) or
                 time.time() - os.path.getmtime(dest) > ttl_hours * 3600)
        if stale:
            try:
                download(NFLVERSE[key], dest)
            except Exception as e:  # noqa: BLE001 - reported, not hidden
                failed.append(f"{key}: {e}")
                if os.path.exists(dest):
                    out[key] = dest  # fall back to the stale copy
                continue
        out[key] = dest
    if failed and len(out) < len(keys or names):
        raise RuntimeError("nflverse fetch failed: " + "; ".join(failed))
    return out


# ---------------------------------------------------------------- G1 usage-gap radar

def ppr_points(row):
    """Half/full-PPR-ish fantasy points from nflverse component columns.

    Formula (documented, fixed): passing yds/25 + 4*passTD - 2*INT;
    rushing yds/10 + 6*rushTD - 2*fumblesLost; receiving rec + yds/10 +
    6*recTD - 2*fumblesLost; +2 per 2pt conversion. Return-game and kicking
    points excluded (skill-position radar only).
    """
    def f(k):
        try:
            return float(row.get(k) or 0)
        except (TypeError, ValueError):
            return 0.0
    pts = (f("passing_yards") / 25 + 4 * f("passing_tds")
           - 2 * f("passing_interceptions"))
    pts += (f("rushing_yards") / 10 + 6 * f("rushing_tds")
            - 2 * f("rushing_fumbles_lost"))
    pts += (f("receptions") + f("receiving_yards") / 10
            + 6 * f("receiving_tds") - 2 * f("receiving_fumbles_lost"))
    pts += 2 * (f("passing_2pt_conversions") + f("rushing_2pt_conversions")
                + f("receiving_2pt_conversions"))
    return pts


def load_usage(stats_gz, snaps_csv):
    """Aggregate nflverse weekly stats + snap counts per player.

    Returns {nkey: dict} with nkey=(norm_name, team, pos); team = most
    recent week seen. Fields: games, targets, target_share, air_yards_share,
    wopr, carries, tds, pts (PPR), offense_pct (avg), team.
    """
    agg = {}

    def key_of(name, team, pos):
        return (norm_name(name), team, pos)

    def get(k):
        return agg.setdefault(k, {
            "games": 0, "targets": 0.0, "ts": 0.0, "ays": 0.0, "wopr": 0.0,
            "carries": 0.0, "tds": 0.0, "pts": 0.0, "rec_yards": 0.0,
            "air_yards": 0.0, "snap_sum": 0.0, "snap_n": 0, "week": 0,
            "team": "", "name": "", "pos": ""})

    def num(v):
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0

    opener = gzip.open if str(stats_gz).endswith(".gz") else open
    with opener(stats_gz, "rt") as fh:
        for r in csv.DictReader(fh):
            if not _reg_game_row(r):
                continue
            pos = r.get("position")
            if pos not in ("QB", "RB", "WR", "TE"):
                continue
            k = key_of(r.get("player_display_name"), r.get("team"), pos)
            a = get(k)
            a["games"] += 1
            a["targets"] += num(r.get("targets"))
            a["ts"] += num(r.get("target_share"))
            a["ays"] += num(r.get("air_yards_share"))
            a["wopr"] += num(r.get("wopr"))
            a["carries"] += num(r.get("carries"))
            a["tds"] += (num(r.get("passing_tds")) + num(r.get("rushing_tds"))
                         + num(r.get("receiving_tds")))
            a["pts"] += ppr_points(r)
            a["rec_yards"] += num(r.get("receiving_yards"))
            a["air_yards"] += num(r.get("receiving_air_yards"))
            try:
                wk = int(r.get("week") or 0)
            except (TypeError, ValueError):
                wk = 0
            if wk >= a["week"]:
                a["week"] = wk
                a["team"] = r.get("team") or a["team"]
            a["name"] = r.get("player_display_name") or a["name"]
            a["pos"] = pos
    with open(snaps_csv) as fh:
        for r in csv.DictReader(fh):
            if not _reg_game_row(r):
                continue
            pos = r.get("position")
            if pos not in ("QB", "RB", "WR", "TE"):
                continue
            k = key_of(r.get("player"), r.get("team"), pos)
            a = agg.get(k)
            if a is None:
                continue
            a["snap_sum"] += num(r.get("offense_pct"))
            a["snap_n"] += 1
    out = {}
    for k, a in agg.items():
        g = max(a["games"], 1)
        out[k] = {
            "name": a["name"], "pos": a["pos"], "team": a["team"],
            "games": a["games"],
            "targets_pg": a["targets"] / g,
            "target_share": a["ts"] / g,
            "air_yards_share": a["ays"] / g,
            "wopr": a["wopr"] / g,
            "carries_pg": a["carries"] / g,
            "tds_pg": a["tds"] / g,
            "ppg": a["pts"] / g,
            "offense_pct": (a["snap_sum"] / a["snap_n"]) if a["snap_n"] else 0.0,
            "racr": (a["rec_yards"] / a["air_yards"]) if a["air_yards"] else 0.0,
        }
    return out


def expected_ppg(u):
    """Usage-implied PPR points/game (transparent linear proxies).

    WR/TE: routes aren't available free anywhere (verified), so wopr
    (1.5*target_share + 0.7*air_yards_share) is the route proxy.
    RB: carries + targets proxy the role. Calibrated so a mid WR2 (~13%
    target share) implies ~10 ppg and a bellcow RB (~18 carries + 3.5
    targets) implies ~18 ppg.
    """
    if u["pos"] in ("WR", "TE"):
        return 2.5 + 22.0 * u["wopr"]
    if u["pos"] == "RB":
        return 0.5 + 0.7 * u["carries_pg"] + 1.4 * u["targets_pg"]
    return u["ppg"]  # QBs not modeled


# G1 thresholds (tuned so only real divergences flag, not noise)
G1_MIN_GAMES = 2
G1_BUYLOW_GAP = 3.5    # usage implies >=3.5 ppg more than actual
G1_SELLHIGH_GAP = 4.0  # actual >=4.0 ppg above usage-implied
G1_BUYLOW_TARGETS = 5.0   # targets/game floor (role is real)
G1_BUYLOW_CARRIES = 10.0  # carries/game floor for RBs
G1_TD_DEPENDENT = 0.6     # tds/game marking a TD-dependent profile


def usage_gaps(usage, min_games=G1_MIN_GAMES):
    """Split usage-tracked players into buy-low / sell-high lists.

    buy-low: elite usage (targets/carries + air yards) with poor output —
    positive TD/yardage regression candidate. sell-high: TD-inflated
    output on thin usage — negative regression candidate. Returns
    (buy_lows, sell_highs), each a list of dicts sorted by gap desc.
    """
    buys, sells = [], []
    for nkey, u in usage.items():
        if u["games"] < min_games or u["pos"] not in ("RB", "WR", "TE"):
            continue
        exp = expected_ppg(u)
        gap = exp - u["ppg"]  # positive = underperforming usage
        base = {"nkey": nkey, "name": u["name"], "pos": u["pos"],
                "team": u["team"], "games": u["games"],
                "ppg": round(u["ppg"], 1), "exp": round(exp, 1),
                "gap": round(gap, 1),
                "ts": round(u["target_share"], 3),
                "ays": round(u["air_yards_share"], 3),
                "snap": round(u["offense_pct"], 2)}
        if u["pos"] in ("WR", "TE"):
            role_ok = u["targets_pg"] >= G1_BUYLOW_TARGETS
        else:
            role_ok = u["carries_pg"] >= G1_BUYLOW_CARRIES
        if gap >= G1_BUYLOW_GAP and role_ok:
            note = (f"yards lag air yards (racr {u['racr']:.2f})"
                    if u["pos"] in ("WR", "TE") and u["racr"] < 0.75
                    else "usage without production")
            buys.append(dict(base, note=note))
        elif gap <= -G1_SELLHIGH_GAP and u["tds_pg"] >= G1_TD_DEPENDENT:
            thin = (u["target_share"] < 0.20 if u["pos"] in ("WR", "TE")
                    else u["targets_pg"] + u["carries_pg"] < 14)
            if thin or u["tds_pg"] >= 1.0:
                sells.append(dict(base, note="TD-inflated on thin usage"))
    buys.sort(key=lambda d: -d["gap"])
    sells.sort(key=lambda d: d["gap"])
    return buys, sells

# ---------------------------------------------------------------- G2 manager trade profiles

def trade_profiles(trades, ppos_of, pval_of, rid2name):
    """Model each manager's trade behavior from completed trades.

    trades: [{round, adds: {pid: receiving_rid}, drops: {pid: sending_rid}}]
    Returns {rid: {name, n_trades, rounds, acquired_pos, sent_pos,
    net_value, partners, first_round, last_round}}. net_value = value
    acquired - value sent (positive = wins the value ledger).
    """
    prof = {}
    for t in trades:
        adds, drops = t.get("adds") or {}, t.get("drops") or {}
        # value each side received
        recv_val = {}
        for pid, rid in adds.items():
            rid = str(rid)
            recv_val[rid] = recv_val.get(rid, 0) + (pval_of(str(pid)) or 0)
        for rid in recv_val:
            p = prof.setdefault(str(rid), {
                "name": rid2name.get(str(rid), str(rid)), "n_trades": 0,
                "rounds": [], "acquired_pos": {}, "sent_pos": {},
                "net_value": 0, "partners": set()})
            p["n_trades"] += 1
            p["rounds"].append(t.get("round"))
        for pid, rid in adds.items():
            rid = str(rid)
            pos = ppos_of(str(pid)) or "?"
            d = prof[rid]["acquired_pos"]
            d[pos] = d.get(pos, 0) + 1
        for pid, rid in drops.items():
            rid = str(rid)
            if rid not in prof:
                continue  # drops-only side of a multi-team deal edge
            pos = ppos_of(str(pid)) or "?"
            d = prof[rid]["sent_pos"]
            d[pos] = d.get(pos, 0) + 1
        # partners: everyone else in this deal
        rids = {str(r) for r in list(adds.values()) + list(drops.values())}
        for rid in rids:
            if rid in prof:
                prof[rid]["partners"] |= (rids - {rid})
        # net value: acquired minus sent per roster
        sent_val = {}
        for pid, rid in drops.items():
            rid = str(rid)
            sent_val[rid] = sent_val.get(rid, 0) + (pval_of(str(pid)) or 0)
        for rid in rids:
            if rid in prof:
                prof[rid]["net_value"] += (recv_val.get(rid, 0)
                                           - sent_val.get(rid, 0))
    for p in prof.values():
        p["partners"] = sorted(p["partners"])
        p["first_round"] = min(p["rounds"]) if p["rounds"] else None
        p["last_round"] = max(p["rounds"]) if p["rounds"] else None
    return prof


def profile_line(p):
    """One-line human summary of a manager profile."""
    def top(d):
        return max(d, key=d.get) if d else "-"
    timing = ""
    if p["first_round"] is not None:
        timing = (f", first r{p['first_round']}, last r{p['last_round']}")
    return (f"{p['name']}: {p['n_trades']} trade(s){timing}; "
            f"buys {top(p['acquired_pos'])}, sells {top(p['sent_pos'])}; "
            f"net value {p['net_value']:+.0f}")


# ---------------------------------------------------------------- G3 trending velocity

# Wesley's rule: exactly ONE Sleeper call per scan. Velocity is derived
# from our own timestamped snapshot history, not from extra lookback
# windows — the previous three-window design made 3 calls per scan.
G3_HEATING = 2.0   # trailing-24h adds >= 2x the previous scan's 24h
G3_COOLING = 0.5   # trailing-24h adds <= half the previous scan's 24h
G3_SNAP_PATH = os.path.join(GOAL_DIR, "hidden_files",
                            "trending_snapshots.json")
G3_MAX_SNAPS = 40


def fetch_trending():
    """The single allowed trending call per scan: 24h add counts.

    Returns the raw Sleeper list [{player_id, count}]. Raises on failure —
    the caller prints a marked degraded line, never fakes velocity.
    """
    return fetch_json(
        f"{SLEEPER}/players/nfl/trending/add?lookback_hours=24&limit=25")


def load_trend_snapshots(path=G3_SNAP_PATH):
    """[ {ts, counts} ] oldest-first; [] when none exist (never crashes)."""
    try:
        with open(path) as fh:
            snaps = json.load(fh)
    except (OSError, ValueError):
        return []
    return [s for s in snaps
            if isinstance(s, dict) and "ts" in s and "counts" in s]


def save_trend_snapshot(counts, ts=None, path=G3_SNAP_PATH):
    """Append {ts, counts} to the snapshot history (atomic, capped).

    Returns the full history list. Call AFTER a successful scan so the
    next scan has a baseline to measure velocity against.
    """
    snaps = load_trend_snapshots(path)
    snaps.append({"ts": ts if ts is not None else time.time(),
                  "counts": counts})
    snaps = snaps[-G3_MAX_SNAPS:]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(snaps, fh)
    os.replace(tmp, path)
    return snaps


def trending_velocity(current, prev, players, rostered):
    """Classify free-agent add velocity from snapshot history.

    current: {pid: adds over the last 24h} — this scan's single call.
    prev: the most recent prior snapshot {ts, counts} or None.
    accel = (adds over the trailing 24h now) / (adds over the trailing
    24h ending at the previous scan) — both windows are trailing-24h, so
    the ratio is a clean velocity read with no window-size math. A prior
    snapshot less than an hour old overlaps too much to be a baseline;
    no prior snapshot, or the player absent from it (or at zero there),
    -> NEW. No honest acceleration exists there and one is never faked.
    Returns [{sid, name, pos, c24, accel, base, tag}], tag in {NEW,
    HEATING, COOLING, STEADY}, new/heating first.
    """
    gap_h = None
    if prev:
        try:
            gap_h = (time.time() - float(prev["ts"])) / 3600.0
        except (TypeError, ValueError):
            gap_h = None
    prev_counts = (prev or {}).get("counts") or {}
    baseline_ok = gap_h is not None and gap_h >= 1.0
    out = []
    for pid, c24 in (current or {}).items():
        if pid in rostered:
            continue
        p = players.get(pid, {}) if isinstance(players, dict) else {}
        pos = (p.get("position") or "?") if isinstance(p, dict) else "?"
        prev_c = prev_counts.get(pid)
        if (baseline_ok and prev_c is not None
                and float(prev_c) > 0 and (c24 or 0) > 0):
            accel = round(float(c24) / float(prev_c), 2)
            base = f"prev {gap_h:.0f}h"
            if accel >= G3_HEATING:
                tag = "HEATING"
            elif accel <= G3_COOLING:
                tag = "COOLING"
            else:
                tag = "STEADY"
        elif (baseline_ok and prev_c is not None and float(prev_c) > 0
                and not (c24 or 0)):
            accel, base, tag = 0.0, f"prev {gap_h:.0f}h", "COOLING"
        else:
            accel, base, tag = None, "—", "NEW"
        out.append({"sid": pid,
                    "name": (p.get("full_name") or pid)
                    if isinstance(p, dict) else pid,
                    "pos": pos, "c24": c24 or 0,
                    "accel": accel, "base": base, "tag": tag})
    _rank = {"NEW": 0, "HEATING": 1, "STEADY": 2, "COOLING": 3}
    out.sort(key=lambda d: (_rank[d["tag"]], -(d["accel"] or 0), -d["c24"]))
    return out


def _reg_game_row(r):
    """True for regular-season rows in both nflverse layouts.

    games.csv (schedules) uses `game_type`; player_stats uses
    `season_type`. Checking both is what makes the playoff-schedule
    and ESPN-game readers see the schedule at all.
    """
    return (r.get("season_type") or r.get("game_type")) == "REG"


# Fixed 2026 NFL team universe. load_playoff_opponents must NOT derive the
# universe from the data: a playoff week missing from the file would turn
# into 32 phantom byes. Weeks with no schedule rows are unsupported.
NFL_TEAMS = frozenset(
    "ARI ATL BAL BUF CAR CHI CIN CLE DAL DEN DET GB HOU IND JAX KC LV LAC "
    "LAR MIA MIN NE NO NYG NYJ PHI PIT SF SEA TB TEN WAS".split())


# ---------------------------------------------------------------- G4 playoff-odds Monte Carlo

def simulate_playoffs(records, schedule, playoff_teams, sims=2000, seed=7,
                      pfpg=None):
    """Monte Carlo rest-of-season -> playoff probability per roster.

    records: {rid: (w, l, t)} current. schedule: {rid: [opp_rid, ...]}
    remaining games (each game listed once per team; deduped internally).
    pfpg: {rid: points-for/game} optional — win prob via logistic on the
    scoring gap; without it, regressed win% is the strength proxy.
    Returns {rid: (play_prob, exp_wins)}. Seeded -> deterministic.
    """
    rng = random.Random(seed)
    rids = [str(r) for r in records]
    # dedupe games: schedule lists each game twice (once per team)
    games = set()
    for rid, opps in schedule.items():
        for o in opps:
            a, b = str(rid), str(o)
            if a != b:
                games.add(tuple(sorted((a, b))))
    games = sorted(games)

    def strength(rid):
        w, l, t = records.get(rid, (0, 0, 0))
        g = w + l + t
        if pfpg and pfpg.get(rid):
            return ("pts", pfpg[rid])
        return ("pct", (w + 1.5) / (g + 3) if g else 0.5)

    def win_prob(a, b):
        ka, sa = strength(a)
        kb, sb = strength(b)
        if ka == "pts":
            return 1.0 / (1.0 + math.exp(-(sa - sb) / 20.0))
        return sa / (sa + sb) if (sa + sb) else 0.5

    made = {r: 0 for r in rids}
    tot_w = {r: 0.0 for r in rids}
    for _ in range(sims):
        wins = {r: float(records.get(r, (0, 0, 0))[0]) for r in rids}
        for a, b in games:
            if rng.random() < win_prob(a, b):
                wins[a] += 1
            else:
                wins[b] += 1
        # rank: wins desc, then pfpg desc, then random (stable via rng)
        order = sorted(rids, key=lambda r: (
            -wins[r], -(pfpg.get(r, 0) if pfpg else 0), rng.random()))
        for r in order[:max(playoff_teams, 0)]:
            made[r] += 1
        for r in rids:
            tot_w[r] += wins[r]
    return {r: (made[r] / sims, tot_w[r] / sims) for r in rids}


def fetch_remaining_schedule(league_id, nfl_week, last_week=18):
    """{rid: [opp_rid,...]} for weeks nfl_week..last_week + {rid: pfpg}.

    Sleeper publishes future matchups with matchup_id set, so the
    remaining schedule is known. Past weeks' points come from the
    matchups endpoints (completed weeks only).
    """
    sched, pf, played = {}, {}, {}
    for wk in range(nfl_week, last_week + 1):
        try:
            ms = fetch_json(f"{SLEEPER}/league/{league_id}/matchups/{wk}")
        except Exception:  # noqa: BLE001 - a missing future week is normal
            continue
        by_mid = {}
        for m in ms:
            by_mid.setdefault(m.get("matchup_id"), []).append(m)
        for mid, pair in by_mid.items():
            if len(pair) != 2:
                continue
            a, b = str(pair[0]["roster_id"]), str(pair[1]["roster_id"])
            sched.setdefault(a, []).append(b)
            sched.setdefault(b, []).append(a)
    for wk in range(1, nfl_week):
        try:
            ms = fetch_json(f"{SLEEPER}/league/{league_id}/matchups/{wk}")
        except Exception:  # noqa: BLE001 - skip weeks that fail
            continue
        for m in ms:
            rid = str(m.get("roster_id"))
            pts = m.get("points") or 0
            pf[rid] = pf.get(rid, 0) + pts
            played[rid] = played.get(rid, 0) + 1
    pfpg = {r: pf[r] / played[r] for r in pf if played.get(r)}
    return sched, pfpg


def posture_for(prob):
    """Contender/pretender posture from playoff probability."""
    if prob >= 0.60:
        return "CONTENDER — win-now: pay up for certainty"
    if prob >= 0.30:
        return "BUBBLE — balanced: fill lineup needs, keep flexibility"
    return "LONGSHOT — stop buying: develop youth, sell vets for keepers"

# ---------------------------------------------------------------- G5 playoff-week schedule weighting

def load_playoff_opponents(games_gz, playoff_weeks, season="2026"):
    """{(team): [(week, opp_or_None)]} + [playoff weeks with no schedule rows].

    opp None = bye that week (devastating in a playoff week). Rows are
    filtered to `season` — the cached games.csv spans 1999-2026 and mixing
    seasons blends 28 years of slates into one. The team universe is the
    fixed 32-team NFL set, never the teams seen in the file — deriving it
    from data turns a missing week into 32 phantom byes. A playoff week
    with no schedule rows is returned as unsupported; the caller must
    degrade for it, never claim byes.
    """
    opps, played = {}, {}
    opener = gzip.open if str(games_gz).endswith(".gz") else open
    with opener(games_gz, "rt") as fh:
        for r in csv.DictReader(fh):
            if not _reg_game_row(r) or str(r.get("season") or "") != season:
                continue
            try:
                wk = int(r.get("week") or 0)
            except (TypeError, ValueError):
                continue
            if wk not in playoff_weeks:
                continue
            a, h = r.get("away_team"), r.get("home_team")
            if not a or not h:
                continue
            opps.setdefault(a, []).append((wk, h))
            opps.setdefault(h, []).append((wk, a))
            played.setdefault(wk, set()).update((a, h))
    missing = [wk for wk in playoff_weeks if not played.get(wk)]
    for wk in playoff_weeks:
        if wk in missing:
            continue  # unsupported week: no bye claims, period
        idle = NFL_TEAMS - played[wk]
        for t in idle:
            opps.setdefault(t, []).append((wk, None))
    return opps, missing


def defensive_strength(stats_gz):
    """Fantasy points allowed per game by (defense team, position).

    Built from 2026 nflverse player-week rows: a defense's number is the
    PPR points its opponents' players at that position scored, per game.
    Returns {(defteam, pos): allowed_pg}.
    """
    allowed, games = {}, {}
    opener = gzip.open if str(stats_gz).endswith(".gz") else open
    with opener(stats_gz, "rt") as fh:
        for r in csv.DictReader(fh):
            if not _reg_game_row(r):
                continue
            pos = r.get("position")
            if pos not in ("QB", "RB", "WR", "TE"):
                continue
            d = r.get("opponent_team")
            if not d:
                continue
            key = (d, pos)
            allowed[key] = allowed.get(key, 0.0) + ppr_points(r)
            games[d] = games.get(d, set())
            games[d].add(r.get("game_id"))
    return {k: v / max(len(games[k[0]]), 1) for k, v in allowed.items()}


# G5: playoff-schedule value multiplier, clamped so schedule never
# dominates talent (a soft playoff slate is a nudge, not a verdict).
G5_MIN_MULT, G5_MAX_MULT = 0.90, 1.10
G5_BYE_MULT = 0.85  # a bye during YOUR playoff weeks: scores a zero


def playoff_multipliers(games_gz, stats_gz, player_teams, playoff_weeks):
    """{nkey -> (mult, tag)} weighting trade values by Weeks 15-17 slate.

    player_teams: {nkey: (team, pos)}. mult in [0.9, 1.1]: 1.1 = softest
    playoff schedule at the position, 0.9 = toughest. A bye in a playoff
    week forces 0.85 and a [PLAYOFF BYE] tag. When a playoff week has no
    schedule rows the SOS is unsupported — every player gets a marked
    neutral (never a phantom bye, never a silent 1.0).
    """
    opps, missing = load_playoff_opponents(games_gz, playoff_weeks)
    out = {}
    if missing:
        tag = ("[G5 SOS unavailable: W{} schedule missing]"
               .format("+W".join(str(w) for w in missing)))
        for nkey in player_teams:
            out[nkey] = (1.0, tag)
        return out
    allowed = defensive_strength(stats_gz)
    # percentile rank of each defense per position (0 = toughest)
    by_pos = {}
    for (d, pos), v in allowed.items():
        by_pos.setdefault(pos, []).append((v, d))
    pct = {}
    for pos, lst in by_pos.items():
        srt = sorted(lst)
        n = max(len(srt) - 1, 1)
        for i, (_, d) in enumerate(srt):
            pct[(d, pos)] = i / n
    for nkey, (team, pos) in player_teams.items():
        if pos not in ("QB", "RB", "WR", "TE") or not team:
            out[nkey] = (1.0, "")
            continue
        weeks = opps.get(team, [])
        if not weeks:
            out[nkey] = (1.0, "")
            continue
        bye_wk = next((w for w, o in weeks if o is None), None)
        if bye_wk is not None:
            out[nkey] = (G5_BYE_MULT, f"[PLAYOFF BYE W{bye_wk}]")
            continue
        # average defensive softness: pct (0 = toughest, 1 = softest)
        soft = sum(pct.get((o, pos), 0.5) for _, o in weeks)
        soft /= max(len(weeks), 1)
        mult = min(G5_MAX_MULT, max(G5_MIN_MULT, 1.0 + 0.2 * (soft - 0.5)))
        tag = "[P+ soft]" if mult >= 1.05 else ("[P- brutal]"
                                                if mult <= 0.95 else "")
        out[nkey] = (round(mult, 3), tag)
    return out


# ---------------------------------------------------------------- G6 handcuff leverage map

def parse_depth_charts(dc_csv):
    """Latest depth-chart snapshot per team/position.

    Returns {team: {pos_abb: [(rank, name, gsis), ...]}} sorted by rank.
    Deduplicates the daily snapshots (dt) — latest dt wins per player.
    """
    latest = {}
    with open(dc_csv) as fh:
        for r in csv.DictReader(fh):
            pos = r.get("pos_abb")
            if pos not in ("RB", "QB", "WR", "TE"):
                continue
            # Key on gsis when present: player names change between
            # snapshots (trades/renames); keying on the name leaves
            # stale rows alive as phantom duplicates.
            gsis = (r.get("gsis_id") or "").strip()
            ident = gsis if gsis and gsis.lower() != "none" \
                else r.get("player_name")
            key = (r.get("team"), ident, pos)
            if key not in latest or (r.get("dt") or "") > latest[key]["dt"]:
                latest[key] = r
    depth = {}
    for (_team, _ident, pos), r in latest.items():
        try:
            rank = int(r.get("pos_rank") or 99)
        except (TypeError, ValueError):
            rank = 99
        depth.setdefault(_team, {}).setdefault(pos, []).append(
            (rank, r.get("player_name"), r.get("gsis_id")))
    for team in depth:
        for pos in depth[team]:
            depth[team][pos].sort()
    return depth


def handcuff_map(depth, my_ids, rostered_ids, players, rid2name, my_rid):
    """For each of MY rostered RBs: the direct backup and who holds him.

    Returns [{starter_sid, starter, team, lead (bool), backups:
    [{sid, name, status}]}] where status is 'mine' | 'free' |
    'opp:<manager>' | 'unknown'. A tie at rank 2 lists every co-backup.
    """
    gsis_to_sid = {}
    for sid, p in (players.items() if isinstance(players, dict) else []):
        if isinstance(p, dict) and p.get("gsis_id"):
            gsis_to_sid[p["gsis_id"]] = str(sid)
    roster_owner = {}
    for rid, ids in rostered_ids.items():
        for pid in ids:
            roster_owner[str(pid)] = str(rid)
    out = []
    for sid in my_ids:
        p = players.get(str(sid), {}) if isinstance(players, dict) else {}
        if not isinstance(p, dict) or p.get("position") != "RB":
            continue
        team = p.get("team")
        rbs = (depth.get(team) or {}).get("RB", [])
        if not rbs:
            out.append({"starter_sid": str(sid), "starter": p.get("full_name"),
                        "team": team, "lead": None, "backups": [],
                        "note": "no depth-chart data"})
            continue
        me_norm = norm_name(p.get("full_name"))
        my_rank = next((rk for rk, nm, _ in rbs
                        if norm_name(nm) == me_norm), None)
        lead = (my_rank == 1)
        backs = []
        if lead:
            for rk, nm, gsis in rbs:
                if rk != 2:
                    continue
                bsid = gsis_to_sid.get(gsis or "")
                if bsid is None:
                    # fallback: normalized-name match on the same team
                    for csid, cp in players.items():
                        if (isinstance(cp, dict)
                                and norm_name(cp.get("full_name")) ==
                                norm_name(nm)
                                and cp.get("team") == team):
                            bsid = str(csid)
                            break
                if bsid is not None and bsid in my_ids:
                    status = "mine"
                elif bsid is not None and bsid in roster_owner:
                    orid = roster_owner[bsid]
                    status = ("mine" if orid == str(my_rid)
                              else f"opp:{rid2name.get(orid, orid)}")
                elif bsid is not None:
                    status = "free"
                else:
                    status = "unknown"
                backs.append({"sid": bsid, "name": nm, "status": status})
        out.append({"starter_sid": str(sid), "starter": p.get("full_name"),
                    "team": team, "lead": lead, "backups": backs,
                    "rank": my_rank,
                    "note": "" if lead else f"not the lead back (rank {my_rank})"})
    return out


def infer_backups_from_snaps(snaps_csv, team):
    """G6 fallback when the depth-chart source is thin: top-2 RBs by
    average offense snaps for a team, labeled INFERRED (never presented
    as a real depth chart)."""
    tot, n = {}, {}
    with open(snaps_csv) as fh:
        for r in csv.DictReader(fh):
            if (not _reg_game_row(r) or r.get("team") != team
                    or r.get("position") != "RB"):
                continue
            nm = r.get("player")
            try:
                tot[nm] = tot.get(nm, 0.0) + float(r.get("offense_snaps") or 0)
            except (TypeError, ValueError):
                continue
            n[nm] = n.get(nm, 0) + 1
    avg = sorted(((tot[k] / n[k], k) for k in tot), reverse=True)
    return [(nm, round(a, 1), "INFERRED from snap share") for a, nm in avg[:2]]


# ---------------------------------------------------------------- G7 schedule-luck audit

def schedule_luck(matchups_by_week, records):
    """All-play expected wins vs actual record, per roster.

    matchups_by_week: {week: [(rid, points), ...]} completed weeks.
    records: {rid: (w, l, t)}.
    expected[rid] = sum over weeks of (opponents beaten that week / (n-1))
    — the win total a league-average schedule would produce (0..weeks
    scale, so two completed weeks max out at 2.0).
    Returns {rid: {wins, expected, weeks}}; luck = wins - expected.
    """
    exp = {}
    n_weeks = 0
    for wk in sorted(matchups_by_week):
        rows = [(str(r), pts) for r, pts in matchups_by_week[wk]]
        n = len(rows)
        if n < 2:
            continue
        n_weeks += 1
        for rid, pts in rows:
            beaten = sum(1 for o, op in rows if o != rid and pts > op)
            exp[rid] = exp.get(rid, 0.0) + beaten / (n - 1)
    return {rid: {"wins": records.get(rid, (0, 0, 0))[0],
                  "expected": round(exp.get(rid, 0.0), 2),
                  "weeks": n_weeks}
            for rid in exp}


# ---------------------------------------------------------------- G8 roster-clog audit

def clog_audit(bench, handcuff_of=None, ahead_out=None, rising=None,
               onblock=None):
    """Rank the bench by contingent value (lowest = most droppable).

    bench: [{sid, name, pos, val, inj}]. handcuff_of: {sid: True} if the
    player is the direct backup to one of MY starters (G6). ahead_out:
    {sid: True} if the starter ahead of him is Out/IR (he'd start).
    rising: {sid: True} if usage is climbing (G1 buy-low). onblock: set of
    sids with open offers (never named as drops).
    contingent = val * (1 + 0.9*handcuff + 1.1*ahead_out + 0.25*rising).
    Returns (ranked, drops): ranked asc by contingent value; drops = the
    bottom names that are safe to name (not on-block, not IR-stashed).
    """
    handcuff_of = handcuff_of or {}
    ahead_out = ahead_out or {}
    rising = rising or {}
    onblock = onblock or set()
    ranked = []
    for b in bench:
        sid = str(b["sid"])
        mult = (1.0 + 0.9 * bool(handcuff_of.get(sid))
                + 1.1 * bool(ahead_out.get(sid))
                + 0.25 * bool(rising.get(sid)))
        ranked.append(dict(b, contingent=round(b.get("val", 0) * mult, 1),
                           blocked=sid in onblock))
    ranked.sort(key=lambda d: d["contingent"])
    # Drops: lowest contingent value AND no contingency boost (no handcuff
    # role, no hurt starter ahead, no rising usage), never on-block/IR.
    drops = [d["name"] for d in ranked
             if not d["blocked"] and (d.get("inj") or "") != "IR"
             and d["contingent"] <= d.get("val", 0)][:3]
    return ranked, drops


# ---------------------------------------------------------------- G9 bye-crater forecast

def bye_craters(byes, my_ids, lineup_ids, nfl_week, horizon=4):
    """{week: {total, starters, crater}} for the next `horizon` weeks AFTER
    the current one (W{nfl_week+1}..W{nfl_week+horizon}).

    byes: {sid: bye_week}. A week with >=2 projected starters on bye is a
    CRATER — the forecast names exactly who is out.
    """
    my_ids, lineup_ids = set(map(str, my_ids)), set(map(str, lineup_ids))
    out = {}
    for wk in range(nfl_week + 1, nfl_week + horizon + 1):
        on_bye = [sid for sid in my_ids if byes.get(str(sid)) == wk]
        starters = [sid for sid in on_bye if sid in lineup_ids]
        out[wk] = {"total": len(on_bye), "starters": starters,
                   "crater": len(starters) >= 2}
    return out

# ---------------------------------------------------------------- G10 betting-market cross-check (start/sit)

# Game-level only: spread + total + moneylines from a single provider
# (DraftKings) via ESPN's undocumented core API. No player props exist in
# this feed and none are synthesized. Any fetch failure -> degraded path
# ("No pricing data available today"), never presented as advice.
G10_PROVIDER_MATCH = "draft"


def parse_odds_item(item, away, home):
    """Parse one ESPN core-API odds item into a game-pricing dict.

    Returns None when the item carries no usable spread/total.
    spread_home: home-team spread (negative = home favored).
    """
    if not isinstance(item, dict):
        return None
    try:
        total = float(item.get("overUnder"))
    except (TypeError, ValueError):
        return None
    try:
        spread_home = float(item.get("spread"))
    except (TypeError, ValueError):
        spread_home = 0.0
    details = item.get("details") or ""
    m = re.match(r"\s*([A-Z]{2,3})\s*([+-])\s*([\d.]+)", details)
    fav_abbr, line = None, abs(spread_home)
    if m:
        fav_abbr = m.group(1)
        line = float(m.group(3))
        if m.group(2) == "+":
            line = -line  # "+6" in details = getting points (dog)
            fav_abbr = None
    elif details.strip().upper() in ("PK", "PICK", "EVEN"):
        line = 0.0
    home_ml = ((item.get("homeTeamOdds") or {}).get("moneyLine"))
    away_ml = ((item.get("awayTeamOdds") or {}).get("moneyLine"))
    open_total = None
    try:
        open_total = float((item.get("open") or {}).get("total", {})
                           .get("alternateDisplayValue"))
    except (TypeError, ValueError):
        open_total = None
    # who is favored: trust the team-odds flags, fall back to the sign
    home_fav = (item.get("homeTeamOdds") or {}).get("favorite")
    if home_fav is None:
        home_fav = spread_home < 0
    fav = home if home_fav else away
    dog = away if home_fav else home
    fav_impl = total / 2 + line / 2
    dog_impl = total / 2 - line / 2
    return {"away": away, "home": home, "fav": fav, "dog": dog,
            "line": round(line, 1), "total": round(total, 1),
            "open_total": open_total,
            "fav_impl": round(fav_impl, 1), "dog_impl": round(dog_impl, 1),
            "home_ml": home_ml, "away_ml": away_ml,
            "provider": ((item.get("provider") or {}).get("name")
                         or "unknown")}


def fetch_week_odds(games_gz, nfl_week, season="2026"):
    """DraftKings game odds for every game of an NFL week.

    Event IDs come from nflverse games.csv (espn column), filtered to
    `season` — the cached file spans 1999-2026 and unfiltered reads match
    every prior year's same-numbered week. Pricing from the ESPN core API
    odds endpoint (undocumented, game-level only). Returns [game dicts].
    Raises RuntimeError when nothing usable comes back — the caller must
    print the degraded path, not advice.
    """
    games = []
    opener = gzip.open if str(games_gz).endswith(".gz") else open
    with opener(games_gz, "rt") as fh:
        for r in csv.DictReader(fh):
            try:
                wk = int(r.get("week") or 0)
            except (TypeError, ValueError):
                continue
            if (not _reg_game_row(r) or wk != nfl_week
                    or str(r.get("season") or "") != season
                    or not r.get("espn")):
                continue
            games.append({"eid": r["espn"], "away": r["away_team"],
                          "home": r["home_team"],
                          "gameday": r.get("gameday")})
    priced, errors = [], []
    for g in games:
        url = (f"{ESPN_CORE}/events/{g['eid']}/competitions/"
               f"{g['eid']}/odds")
        try:
            d = fetch_json(url)
            items = d.get("items") or []
            item = next(
                (i for i in items
                 if G10_PROVIDER_MATCH in
                 str(((i.get("provider") or {}).get("name")) or "").lower()),
                None)
            if item is None:
                item = next((i for i in items if "spread" in i), None)
            parsed = parse_odds_item(item, g["away"], g["home"])
            if parsed:
                parsed["gameday"] = g["gameday"]
                priced.append(parsed)
            else:
                errors.append(f"{g['away']}@{g['home']}: no spread/total")
        except Exception as e:  # noqa: BLE001 - per-game degrade
            errors.append(f"{g['away']}@{g['home']}: {e}")
        time.sleep(0.4)  # polite: ~14 games, one call each
    if not priced:
        raise RuntimeError("odds feed unreachable: "
                           + "; ".join(errors[:3]))
    return priced, errors


G10_SHOOTOUT = 48.0
G10_GRIND = 41.5
G10_BLOWOUT = 7.0


def start_sit_signals(priced_games, my_players):
    """Game-script start/sit notes from game-level pricing.

    my_players: {sid: (name, pos, team)}. Returns [lines]. Signals are
    directional (script/total driven) — never a start/bench verdict on
    their own, and the header says so.
    """
    lines = []
    for g in priced_games:
        mine = [(s, n, p) for s, (n, p, t) in my_players.items()
                if t == g["away"] or t == g["home"]]
        if not mine:
            continue
        ml = (f"{g['away']} @ {g['home']}: {g['fav']} -{g['line']} "
              f"O/U {g['total']} (impl {g['fav']} {g['fav_impl']}, "
              f"{g['dog']} {g['dog_impl']})")
        notes = []
        if g["line"] >= G10_BLOWOUT:
            notes.append(f"heavy favorite ({g['fav']} -{g['line']}): "
                         "positive script for RB/DEF/K")
            notes.append(f"{g['dog']} catching {g['line']}: negative script "
                         "— fade their RB, pass-catchers get garbage-time "
                         "funnel")
        if g["total"] >= G10_SHOOTOUT:
            notes.append("shootout total: green light for all pieces")
        elif g["total"] <= G10_GRIND:
            notes.append("grind total: fringe FLEX fades")
        if g["open_total"] and abs(g["total"] - g["open_total"]) >= 2:
            d = "up" if g["total"] > g["open_total"] else "down"
            notes.append(f"market moved total {d} "
                         f"({g['open_total']} -> {g['total']})")
        who = ", ".join(f"{n} ({p})" for _, n, p in mine)
        lines.append(f"  {ml}\n    your pieces: {who}")
        for n_ in notes:
            lines.append(f"    -> {n_}")
    return lines


# ---------------------------------------------------------------- CLI

def _ts_pt():
    return time.strftime("%Y-%m-%d %H:%M PT", time.localtime())


def cmd_playoff_odds(a):
    """G4+G7 weekly job: simulate playoffs, audit schedule luck.

    Writes a markdown snapshot for the Wednesday trade job and Friday
    matchup preview to read. Exits nonzero on total failure (keeps the
    last good snapshot instead of overwriting with a stub).
    """
    nfl_week = int((fetch_json(f"{SLEEPER}/state/nfl") or {}).get("week") or 1)
    parts = [f"# Playoff odds + schedule luck — generated {_ts_pt()} "
             f"(NFL Week {nfl_week})\n"]
    for lid in a.league:
        league = fetch_json(f"{SLEEPER}/league/{lid}")
        lname = league.get("name", lid)
        settings = league.get("settings") or {}
        pstart = settings.get("playoff_week_start") or 15
        pteams = settings.get("playoff_teams") or 6
        rosters = fetch_json(f"{SLEEPER}/league/{lid}/rosters")
        users = fetch_json(f"{SLEEPER}/league/{lid}/users")
        uname = {u["user_id"]: u.get("display_name", "?") for u in users}
        records, pfpg, names = {}, {}, {}
        for r in rosters:
            rid = str(r["roster_id"])
            rs = r.get("settings") or {}
            records[rid] = (rs.get("wins") or 0, rs.get("losses") or 0,
                            rs.get("ties") or 0)
            names[rid] = uname.get(r.get("owner_id"), "?")
        # fetch_remaining_schedule already returns points FOR PER GAME —
        # assigning directly. (Dividing again by weeks-played was a real
        # bug: it shrank every team's PF/G and corrupted the sim odds.)
        sched, pfpg = fetch_remaining_schedule(lid, nfl_week)
        probs = simulate_playoffs(records, sched, pteams, pfpg=pfpg)
        my_rid = next((str(r["roster_id"]) for r in rosters
                       if r.get("owner_id") == a.me), None)
        my_prob = probs.get(my_rid, (0, 0))[0] if my_rid else 0
        parts.append(f"## {lname} (playoffs W{pstart}, {pteams} teams)\n")
        parts.append(f"Wesley playoff probability: {my_prob:.0%} — "
                     f"{posture_for(my_prob)}\n")
        parts.append("| team | W-L | PF/G | playoff% | exp wins |")
        parts.append("|---|---|---|---|---|")
        for rid in sorted(probs, key=lambda r: -probs[r][0]):
            w, l, t = records.get(rid, (0, 0, 0))
            rec = f"{w}-{l}" + (f"-{t}" if t else "")
            p, ew = probs[rid]
            me = " <-- YOU" if rid == my_rid else ""
            parts.append(f"| {names.get(rid, rid)} | {rec} | "
                         f"{pfpg.get(rid, 0):.1f} | {p:.0%} | {ew:.1f} |{me}")
        # G7 schedule luck from completed weeks
        mbw = {}
        for wk in range(1, nfl_week):
            try:
                ms = fetch_json(f"{SLEEPER}/league/{lid}/matchups/{wk}")
            except Exception:  # noqa: BLE001 - skip failed weeks
                continue
            mbw[wk] = [(m.get("roster_id"), m.get("points") or 0) for m in ms]
        luck = schedule_luck(mbw, records)
        parts.append("\n### Schedule luck (all-play expected wins)\n")
        parts.append("| team | actual | expected | luck |")
        parts.append("|---|---|---|---|")
        for rid in sorted(luck, key=lambda r: luck[r]["expected"]):
            w = records.get(rid, (0, 0, 0))[0]
            e = luck[rid]["expected"]
            me = " <-- YOU" if rid == my_rid else ""
            parts.append(f"| {names.get(rid, rid)} | {w} | {e:.1f} | "
                         f"{w - e:+.1f} |{me}")
        parts.append("")
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w") as fh:
        fh.write("\n".join(parts) + "\n")
    print(f"wrote {a.out}")


def cmd_game_odds(a):
    """G10 weekly job: DraftKings game pricing -> start/sit cross-check.

    Writes hidden_files/game-odds.md for the Saturday auto-sub reminder
    and Sunday inactives checks. Degraded path: keeps the last good file
    and exits nonzero with 'No pricing data available today'.
    """
    nfl_week = int((fetch_json(f"{SLEEPER}/state/nfl") or {}).get("week") or 1)
    players = json.load(open(a.players_cache))
    my_players = {}
    for lid in a.league:
        rosters = fetch_json(f"{SLEEPER}/league/{lid}/rosters")
        mine = next((r for r in rosters if r.get("owner_id") == a.me), None)
        if not mine:
            continue
        for pid in (mine.get("players") or []):
            p = players.get(str(pid), {})
            pos = p.get("position") or "?"
            team = (str(pid) if pos == "DEF"
                    else (p.get("team") or "?"))
            my_players[str(pid)] = (p.get("full_name") or str(pid), pos, team)
    try:
        paths = nflverse_paths(keys=("games",))
        priced, errors = fetch_week_odds(paths["games"], nfl_week)
    except RuntimeError as e:
        print(f"No pricing data available today ({e})")
        sys.exit(3)
    parts = [f"# Game odds cross-check — generated {_ts_pt()} "
             f"(NFL Week {nfl_week}, DraftKings via ESPN core API)\n",
             "Game-level pricing only (spread/total/moneyline). No player "
             "props exist in this feed. Signals are directional game-script "
             "notes, not start/bench verdicts.\n"]
    for g in priced:
        parts.append(f"## {g['away']} @ {g['home']} ({g['gameday']})")
        parts.append(f"spread: {g['fav']} -{g['line']} | total: {g['total']} "
                     f"(open {g['open_total'] or 'n/a'}) | implied: "
                     f"{g['fav']} {g['fav_impl']}, {g['dog']} {g['dog_impl']} "
                     f"| ML {g['home']} {g['home_ml']}, {g['away']} "
                     f"{g['away_ml']}")
    parts.append("\n## Start/sit signals for your pieces\n")
    sig = start_sit_signals(priced, my_players)
    parts.extend(sig if sig else ["(none of your players' games priced)"])
    if errors:
        parts.append(f"\n_unpriced games: {'; '.join(errors[:6])}_")
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w") as fh:
        fh.write("\n".join(parts) + "\n")
    print(f"wrote {a.out} ({len(priced)} games priced)")


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("playoff-odds", cmd_playoff_odds),
                     ("game-odds", cmd_game_odds)):
        p = sub.add_parser(name)
        p.add_argument("--league", action="append", required=True)
        p.add_argument("--me", required=True)
        p.add_argument("--out", required=True)
        p.add_argument("--players-cache",
                       default=os.path.expanduser(
                           "~/workspace/sleeper/players.json"))
        p.set_defaults(fn=fn)
    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()

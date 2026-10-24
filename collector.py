#!/usr/bin/env python3
"""
Raccoglitore quote + indice di mercato (1X2, Over/Under gol, Goal/NoGoal).

Uso:
    export ODDS_API_KEY=la_tua_chiave        (Windows: set ODDS_API_KEY=...)
    python collector.py                      # solo 1X2
    python collector.py --markets totals,btts --btts-max 10 --btts-hours 30
    python collector.py --loop 120           # ripete ogni 120 minuti
    python collector.py --demo               # dati finti per provare la dashboard

Costi (crediti The Odds API):
    1X2 = 1 credito per campionato; con totals = 2 per campionato;
    btts = 1 credito per ogni partita richiesta (--btts-max).
"""
import argparse
import json
import os
import random
import sqlite3
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime

# chiave The Odds API -> nome leggibile
LEAGUES = {
    "soccer_italy_serie_a": "Serie A",
    "soccer_italy_serie_b": "Serie B",
    "soccer_epl": "Premier League",
    "soccer_efl_champ": "Championship",
    "soccer_spain_la_liga": "La Liga",
    "soccer_spain_segunda_division": "Segunda División",
    "soccer_germany_bundesliga": "Bundesliga",
    "soccer_germany_bundesliga2": "2. Bundesliga",
    "soccer_france_ligue_one": "Ligue 1",
    "soccer_netherlands_eredivisie": "Eredivisie",
    "soccer_portugal_primeira_liga": "Primeira Liga",
    "soccer_uefa_champs_league": "Champions League",
    "soccer_uefa_europa_league": "Europa League",
}

BASE = "https://api.the-odds-api.com/v4"
DB_FILE = "odds.db"
OUT_FILE = "data.js"
OUTCOMES = ["1", "X", "2"]


# ---------------------------------------------------------------- database
def open_db(path):
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS snapshots (
            ts INTEGER, event_id TEXT, league TEXT, home TEXT, away TEXT,
            commence INTEGER, book TEXT, h REAL, d REAL, a REAL)"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS ix_ev ON snapshots(event_id, ts)")
    # mercati a due esiti: totals (Over/Under, con linea) e btts (Goal/NoGoal)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS snap2 (
            ts INTEGER, event_id TEXT, league TEXT, home TEXT, away TEXT,
            commence INTEGER, market TEXT, book TEXT, line REAL, o1 REAL, o2 REAL)"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS ix_ev2 ON snap2(event_id, market, ts)")
    return conn


# ---------------------------------------------------------------- API
def ssl_context():
    """usa i certificati di certifi se disponibili (risolve CERTIFICATE_VERIFY_FAILED su Mac)"""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def api_get(path, params):
    url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=30, context=ssl_context()) as r:
        return json.load(r), r.headers.get("x-requests-remaining")


def fetch_league(sport_key, api_key, regions, markets):
    return api_get(
        f"/sports/{sport_key}/odds/",
        {"apiKey": api_key, "regions": regions, "markets": markets, "oddsFormat": "decimal"},
    )


def parse_time(s):
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def save_h2h(conn, ts, sport_key, ev, bk, m, commence):
    o = {x["name"]: x["price"] for x in m["outcomes"]}
    h, d, a = o.get(ev["home_team"]), o.get("Draw"), o.get(ev["away_team"])
    if not (h and d and a):
        return 0
    conn.execute(
        "INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?)",
        (ts, ev["id"], sport_key, ev["home_team"], ev["away_team"], commence, bk["title"], h, d, a),
    )
    return 1


def save_two_way(conn, ts, sport_key, ev, bk, m, commence):
    """totals: Over/Under per ogni linea; btts: Yes/No"""
    first, second = ("over", "under") if m["key"] == "totals" else ("yes", "no")
    groups = defaultdict(dict)
    for x in m["outcomes"]:
        groups[x.get("point", 0.0)][x["name"].lower()] = x["price"]
    n = 0
    for line, o in groups.items():
        a, b = o.get(first), o.get(second)
        if a and b:
            conn.execute(
                "INSERT INTO snap2 VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (ts, ev["id"], sport_key, ev["home_team"], ev["away_team"], commence,
                 m["key"], bk["title"], float(line), a, b),
            )
            n += 1
    return n


def save_events(conn, ts, sport_key, events):
    n = 0
    for ev in events:
        commence = parse_time(ev["commence_time"])
        for bk in ev.get("bookmakers", []):
            for m in bk.get("markets", []):
                if m["key"] == "h2h":
                    n += save_h2h(conn, ts, sport_key, ev, bk, m, commence)
                elif m["key"] in ("totals", "btts"):
                    n += save_two_way(conn, ts, sport_key, ev, bk, m, commence)
    conn.commit()
    return n


def collect(conn, api_key, leagues, regions, markets, btts_max, btts_hours):
    ts = int(time.time())
    remaining = None
    upcoming = []
    main = "h2h,totals" if "totals" in markets else "h2h"
    for key in leagues:
        try:
            events, remaining = fetch_league(key, api_key, regions, main)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                sys.exit("Chiave API non valida o crediti esauriti (401).")
            if e.code == 429:
                print("Troppe richieste (429), mi fermo per questo giro.")
                break
            print(f"  {key}: saltato (HTTP {e.code})")
            continue
        except Exception as e:  # rete, timeout
            print(f"  {key}: errore {e}")
            continue
        n = save_events(conn, ts, key, events)
        print(f"  {LEAGUES.get(key, key)}: {len(events)} partite, {n} quote salvate")
        for ev in events:
            upcoming.append((parse_time(ev["commence_time"]), key, ev["id"]))

    if "btts" in markets:
        now = time.time()
        cand = sorted(x for x in upcoming if now < x[0] < now + btts_hours * 3600)[:btts_max]
        print(f"  Goal/NoGoal: richiedo {len(cand)} partite (1 credito ciascuna)")
        saved = 0
        for _, key, ev_id in cand:
            try:
                data, remaining = api_get(
                    f"/sports/{key}/events/{ev_id}/odds",
                    {"apiKey": api_key, "regions": regions, "markets": "btts", "oddsFormat": "decimal"},
                )
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    print("  Troppe richieste (429), mi fermo.")
                    break
                print(f"  btts {ev_id}: saltato (HTTP {e.code})")
                continue
            except Exception as e:
                print(f"  btts {ev_id}: errore {e}")
                continue
            saved += save_events(conn, ts, key, [data])
        print(f"  Goal/NoGoal: {saved} quote salvate")

    if remaining is not None:
        print(f"Crediti API rimasti: {remaining}")


# ---------------------------------------------------------------- calcoli
def clean_probs(odds):
    inv = [1 / x for x in odds]
    s = sum(inv)
    return [x / s for x in inv]


def mean(xs):
    return sum(xs) / len(xs) if xs else None


def ref_at(series, target_ts):
    """ultima quota con ts <= target_ts (serie ordinata)"""
    best = None
    for t, o in series:
        if t <= target_ts:
            best = o
    return best


def changes(cur_books, books, last_ts, window, n):
    """variazione % media delle quote (per esito) e quota di bookmaker in calo"""
    per = [[] for _ in range(n)]
    for b in cur_books:
        series = books[b]
        ref = series[0][1] if window is None else ref_at(series, last_ts - window)
        if ref is None:
            continue
        cur = series[-1][1]
        for i in range(n):
            per[i].append(cur[i] / ref[i] - 1)
    if not per[0]:
        return [None] * n, [0.0] * n
    ch = [mean(p) for p in per]
    agree = [sum(1 for x in p if x < -0.01) / len(p) for p in per]
    return ch, agree


def analyze(books, names):
    """books: bookmaker -> [(ts, (quota_esito_1, ...))] ordinato. Restituisce il blocco per la dashboard."""
    n = len(names)
    last_ts = max(s[-1][0] for s in books.values())
    first_ts = min(s[0][0] for s in books.values())
    cur_books = [b for b, s in books.items() if s[-1][0] == last_ts]

    cur_odds = [[books[b][-1][1][i] for b in cur_books] for i in range(n)]
    open_odds = [[books[b][0][1][i] for b in cur_books] for i in range(n)]
    avg_cur = [mean(x) for x in cur_odds]
    avg_open = [mean(x) for x in open_odds]
    p_cur = clean_probs(avg_cur)
    p_open = clean_probs(avg_open)

    ch_open, agree = changes(cur_books, books, last_ts, None, n)
    ch_6h, _ = changes(cur_books, books, last_ts, 6 * 3600, n)
    ch_24h, _ = changes(cur_books, books, last_ts, 24 * 3600, n)

    pin = next((b for b in cur_books if "pinnacle" in b.lower()), None)
    pin_ch = None
    if pin:
        s = books[pin]
        pin_ch = [s[-1][1][i] / s[0][1][i] - 1 for i in range(n)]

    best = []
    for i in range(n):
        v, b = max((books[b][-1][1][i], b) for b in cur_books)
        best.append({"odds": v, "book": b})

    # esito con il calo maggiore dall'apertura
    k = min(range(n), key=lambda i: ch_open[i])
    drop = max(0.0, -ch_open[k])
    d6 = max(0.0, -(ch_6h[k] or 0.0))
    score = min(40, drop * 100 * 3.5) + min(20, d6 * 100 * 4) + agree[k] * 25
    if pin_ch and pin_ch[k] < -0.01:
        score += 15
    score = int(round(min(100, score)))
    nb = len(cur_books)
    conf = "alta" if nb >= 6 else "media" if nb >= 3 else "bassa"

    # storico probabilità pulite (media dei bookmaker per rilevazione)
    by_ts = defaultdict(list)
    for s in books.values():
        for t, o in s:
            by_ts[t].append(clean_probs(o))
    history = [
        [t] + [round(mean([p[i] for p in ps]), 4) for i in range(n)]
        for t, ps in sorted(by_ts.items())
    ][-200:]

    return {
        "books": nb, "confidence": conf, "score": score, "top": names[k],
        "first_ts": first_ts, "last_ts": last_ts,
        "outcomes": [{
            "name": names[i],
            "odds": round(avg_cur[i], 3), "odds_open": round(avg_open[i], 3),
            "prob": round(p_cur[i], 4), "prob_open": round(p_open[i], 4),
            "ch_open": ch_open[i], "ch_6h": ch_6h[i], "ch_24h": ch_24h[i],
            "agree": round(agree[i], 2), "best": best[i],
            "pinnacle": round(books[pin][-1][1][i], 3) if pin else None,
        } for i in range(n)],
        "history": history,
    }


def compute_two_way(conn, now):
    """{event_id: {"totals": blocco, "btts": blocco}}"""
    rows = conn.execute(
        "SELECT ts,event_id,market,book,line,o1,o2 FROM snap2 WHERE commence > ? ORDER BY ts", (now,)
    ).fetchall()
    grp = defaultdict(list)
    for r in rows:
        grp[(r[1], r[2])].append(r)
    res = defaultdict(dict)
    for (ev_id, market), rs in grp.items():
        if market == "totals":
            last = rs[-1][0]
            cnt = Counter(r[4] for r in rs if r[0] == last)
            line = max(cnt, key=lambda l: (cnt[l], -abs(l - 2.5)))  # linea più comune, a parità la più vicina a 2.5
            sel = [r for r in rs if r[4] == line]
            names = ["Over", "Under"]
        else:
            line, sel, names = None, rs, ["Goal", "NoGoal"]
        books = defaultdict(list)
        for r in sel:
            books[r[3]].append((r[0], (r[5], r[6])))
        if not books:
            continue
        blk = analyze(books, names)
        if line is not None:
            blk["line"] = line
        res[ev_id][market] = blk
    return res


def compute(conn, now=None):
    now = now or int(time.time())
    rows = conn.execute(
        "SELECT ts,event_id,league,home,away,commence,book,h,d,a FROM snapshots "
        "WHERE commence > ? ORDER BY ts", (now,)
    ).fetchall()
    meta = {}
    h2h = defaultdict(lambda: defaultdict(list))
    for r in rows:
        meta[r[1]] = r[2:6]
        h2h[r[1]][r[6]].append((r[0], (r[7], r[8], r[9])))
    extra = compute_two_way(conn, now)

    out = []
    for ev_id, books in h2h.items():
        league, home, away, commence = meta[ev_id]
        ev = {"id": ev_id, "league": LEAGUES.get(league, league),
              "home": home, "away": away, "commence": commence}
        ev.update(analyze(books, OUTCOMES))
        if ev_id in extra:
            ev["extra"] = extra[ev_id]
        out.append(ev)
    out.sort(key=lambda e: -e["score"])
    return out


def write_output(events, path):
    payload = {"generated": int(time.time()), "events": events}
    with open(path, "w", encoding="utf-8") as f:
        f.write("window.DATA = " + json.dumps(payload, ensure_ascii=False) + ";\n")
    print(f"Scritto {path}: {len(events)} partite")


# ---------------------------------------------------------------- demo
def make_demo(conn):
    random.seed(7)
    teams = ["Inter", "Milan", "Juventus", "Napoli", "Roma", "Lazio", "Atalanta", "Fiorentina",
             "Arsenal", "Chelsea", "Liverpool", "Man City", "Real Madrid", "Barcelona", "Sevilla",
             "Valencia", "Bayern", "Dortmund", "Leipzig", "Leverkusen", "PSG", "Marseille",
             "Lyon", "Monaco", "Ajax", "PSV", "Benfica", "Porto", "Torino", "Bologna",
             "Genoa", "Parma", "Cremonese", "Como", "Spezia", "Bari"]
    leagues = list(LEAGUES)
    books = ["Pinnacle", "Bet365", "Unibet", "Betfair", "William Hill", "Marathon",
             "Betclic", "888sport", "Betway", "LeoVegas"]
    now = int(time.time())
    steps = 72                       # 36 ore a passi di 30 minuti
    for m in range(24):
        h, a = random.sample(teams, 2)
        league = leagues[m % len(leagues)]
        commence = now + random.randint(2, 60) * 3600
        ph, pd = random.uniform(0.25, 0.6), random.uniform(0.2, 0.3)
        pa = 1 - ph - pd
        drift = random.choice([0, 0, 0, 0.0003, 0.0007, -0.0005]) * random.choice([1, 1, 2])
        po, pb = random.uniform(0.42, 0.62), random.uniform(0.45, 0.6)
        d_over = random.choice([0, 0, 0.0004, -0.0005, 0.0008])
        d_btts = random.choice([0, 0, 0.0003, -0.0004])
        nb = random.choice([3, 5, 8, 10])
        margins = {b: random.uniform(0.02, 0.07) for b in books[:nb]}
        eid = f"demo{m}"
        for s in range(steps):
            ts = now - (steps - 1 - s) * 1800
            ph_t = min(0.85, max(0.1, ph + drift * s))
            rest = 1 - ph_t
            pd_t = rest * pd / (pd + pa)
            pa_t = rest - pd_t
            po_t = min(0.85, max(0.15, po + d_over * s))
            pb_t = min(0.85, max(0.15, pb + d_btts * s))
            noise = lambda: random.uniform(-0.012, 0.012)
            for b in books[:nb]:
                ps = [ph_t + noise(), pd_t + noise(), pa_t + noise()]
                odds = [round(1 / (p * (1 + margins[b])), 2) for p in ps]
                conn.execute(
                    "INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (ts, eid, league, h, a, commence, b, *odds),
                )
                if nb >= 5:
                    o1 = round(1 / ((po_t + noise()) * (1 + margins[b])), 2)
                    o2 = round(1 / ((1 - po_t + noise()) * (1 + margins[b])), 2)
                    conn.execute("INSERT INTO snap2 VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                 (ts, eid, league, h, a, commence, "totals", b, 2.5, o1, o2))
            if m % 2 == 0:
                for b in books[:min(nb, 3)]:
                    o1 = round(1 / ((pb_t + noise()) * (1 + margins[b])), 2)
                    o2 = round(1 / ((1 - pb_t + noise()) * (1 + margins[b])), 2)
                    conn.execute("INSERT INTO snap2 VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                 (ts, eid, league, h, a, commence, "btts", b, 0.0, o1, o2))
    conn.commit()


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--leagues", help="chiavi separate da virgola (default: tutte quelle in LEAGUES)")
    ap.add_argument("--regions", default="eu", help="regioni bookmaker: eu, uk, us, au (default eu)")
    ap.add_argument("--markets", default="h2h",
                    help="mercati extra separati da virgola: totals (Over/Under), btts (Goal/NoGoal). Default: solo 1X2")
    ap.add_argument("--btts-max", type=int, default=10, help="max partite per giro per Goal/NoGoal (1 credito ciascuna)")
    ap.add_argument("--btts-hours", type=int, default=30, help="Goal/NoGoal solo per partite che iniziano entro N ore")
    ap.add_argument("--loop", type=int, metavar="MIN", help="ripeti ogni MIN minuti")
    ap.add_argument("--demo", action="store_true", help="usa dati finti (demo.db), nessuna chiave")
    ap.add_argument("--compute-only", action="store_true", help="ricalcola data.js senza chiamare l'API")
    args = ap.parse_args()

    if args.demo:
        if os.path.exists("demo.db"):
            os.remove("demo.db")
        conn = open_db("demo.db")
        make_demo(conn)
        write_output(compute(conn), OUT_FILE)
        return

    conn = open_db(DB_FILE)
    leagues = args.leagues.split(",") if args.leagues else list(LEAGUES)
    markets = {m.strip() for m in args.markets.split(",") if m.strip()}
    api_key = os.environ.get("ODDS_API_KEY")
    if not api_key and not args.compute_only:
        sys.exit("Manca la chiave: imposta la variabile ODDS_API_KEY (oppure prova --demo).")

    while True:
        if not args.compute_only:
            print(datetime.now().strftime("%H:%M:%S"), "rilevazione quote...")
            collect(conn, api_key, leagues, args.regions, markets, args.btts_max, args.btts_hours)
        write_output(compute(conn), OUT_FILE)
        if not args.loop:
            break
        time.sleep(args.loop * 60)


if __name__ == "__main__":
    main()

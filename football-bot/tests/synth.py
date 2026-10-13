"""Synthetic leagues in football-data.co.uk CSV format, for tests only.

Teams have latent attack/defence strengths, goals are Poisson, bookmakers
see the strengths with noise (less noise at closing) and add a margin.
"""
from __future__ import annotations

import csv
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
from scipy.stats import poisson

PRE_COLS = [
    "B365H", "B365D", "B365A", "PSH", "PSD", "PSA", "MaxH", "MaxD", "MaxA", "AvgH", "AvgD", "AvgA",
    "B365>2.5", "B365<2.5", "P>2.5", "P<2.5", "Max>2.5", "Max<2.5", "Avg>2.5", "Avg<2.5",
    "AHh", "B365AHH", "B365AHA", "PAHH", "PAHA", "MaxAHH", "MaxAHA", "AvgAHH", "AvgAHA",
]
CLOSE_COLS = ["PSCH", "PSCD", "PSCA", "AvgCH", "AvgCD", "AvgCA", "PC>2.5", "PC<2.5", "AvgC>2.5", "AvgC<2.5"]
MARGINS = {"PS": 1.025, "Avg": 1.055}


def _season_codes(today: date, n: int) -> list[str]:
    start = today.year if today.month >= 7 else today.year - 1
    return [f"{y % 100:02d}{(y + 1) % 100:02d}" for y in range(start - n + 1, start + 1)]


def _probs(lh: float, la: float) -> tuple[np.ndarray, float]:
    g = np.outer(poisson.pmf(np.arange(11), lh), poisson.pmf(np.arange(11), la))
    i, j = np.indices(g.shape)
    p = np.array([g[i > j].sum(), g[i == j].sum(), g[i < j].sum()])
    return p / p.sum(), float(g[i + j > 2.5].sum() / g.sum())


def _round_robin(teams: list[str]) -> list[list[tuple[str, str]]]:
    t = list(teams)
    n = len(t)
    rounds = []
    for r in range(n - 1):
        pairs = [(t[i], t[n - 1 - i]) for i in range(n // 2)]
        rounds.append([(a, b) if r % 2 else (b, a) for a, b in pairs])
        t = [t[0], t[-1], *t[1:-1]]
    return rounds + [[(b, a) for a, b in rnd] for rnd in rounds]


def generate(out_dir: Path, today: date, leagues=("E0", "D1"), n_seasons: int = 5,
             n_teams: int = 18, seed: int = 0, fixture_days: int = 8) -> None:
    rng = np.random.default_rng(seed)
    out_dir.mkdir(parents=True, exist_ok=True)
    fixtures = []
    for div in leagues:
        names = [f"{div} Club {i:02d}" for i in range(n_teams)]
        new_id = n_teams
        att = dict(zip(names, rng.normal(0, 0.25, n_teams)))
        dfn = dict(zip(names, rng.normal(0, 0.25, n_teams)))
        for code in _season_codes(today, n_seasons):
            year = 2000 + int(code[:2])
            first = date(year, 8, 8)
            first += timedelta(days=(5 - first.weekday()) % 7)  # Saturday
            rows = []
            for r_i, rnd in enumerate(_round_robin(names)):
                for m_i, (h, a) in enumerate(rnd):
                    day = first + timedelta(days=7 * r_i + (1 if m_i % 3 == 2 else 0))
                    hh, mm = [(12, 30), (15, 0), (17, 30), (20, 0)][m_i % 4]
                    ko = datetime(day.year, day.month, day.day, hh, mm)
                    lh = np.exp(0.15 + 0.25 + att[h] - dfn[a])
                    la = np.exp(0.15 + att[a] - dfn[h])
                    row = {"Div": div, "Date": day.strftime("%d/%m/%Y"), "Time": f"{hh:02d}:{mm:02d}",
                           "HomeTeam": h, "AwayTeam": a}
                    row.update(_odds(rng, lh, la, close=False))
                    if ko < datetime.combine(today, datetime.min.time()):
                        gh, ga = int(rng.poisson(lh)), int(rng.poisson(la))
                        row.update({"FTHG": gh, "FTAG": ga, "FTR": "H" if gh > ga else ("D" if gh == ga else "A")})
                        row.update(_odds(rng, lh, la, close=True))
                        rows.append(row)
                    elif (ko.date() - today).days <= fixture_days:
                        fixtures.append(row)
                # strengths drift during the season
                for t in names:
                    att[t] += rng.normal(0, 0.01)
                    dfn[t] += rng.normal(0, 0.01)
            if rows:
                cols = ["Div", "Date", "Time", "HomeTeam", "AwayTeam", "FTHG", "FTAG", "FTR", *PRE_COLS, *CLOSE_COLS]
                with open(out_dir / f"{code}_{div}.csv", "w", newline="", encoding="latin-1") as f:
                    w = csv.DictWriter(f, fieldnames=cols)
                    w.writeheader()
                    w.writerows(rows)
            # promotion / relegation: three weakest clubs replaced by new ones
            strength = sorted(names, key=lambda t: att[t] + dfn[t])
            for old in strength[:3]:
                names.remove(old)
                new = f"{div} Club {new_id:02d}"
                new_id += 1
                att[new], dfn[new] = rng.normal(-0.2, 0.1), rng.normal(-0.2, 0.1)
                names.append(new)
    cols = ["Div", "Date", "Time", "HomeTeam", "AwayTeam", *PRE_COLS]
    with open(out_dir / "fixtures.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(fixtures)


def _odds(rng, lh: float, la: float, close: bool) -> dict:
    noise = 0.035 if close else 0.08
    blh, bla = lh * np.exp(rng.normal(0, noise)), la * np.exp(rng.normal(0, noise))
    p, over = _probs(blh, bla)
    out = {}
    if close:
        for pre, m in (("PSC", 1.02), ("AvgC", 1.05)):
            o = np.round(1 / (p * m), 2)
            out.update({f"{pre}H": o[0], f"{pre}D": o[1], f"{pre}A": o[2]})
        out.update({"PC>2.5": round(1 / (over * 1.02), 2), "PC<2.5": round(1 / ((1 - over) * 1.02), 2),
                    "AvgC>2.5": round(1 / (over * 1.05), 2), "AvgC<2.5": round(1 / ((1 - over) * 1.05), 2)})
        return out
    # Pinnacle: sharp, low margin. Other books see the match with their own noise.
    o = np.round(1 / (p * MARGINS["PS"]), 2)
    out.update({"PSH": o[0], "PSD": o[1], "PSA": o[2],
                "P>2.5": round(1 / (over * MARGINS["PS"]), 2), "P<2.5": round(1 / ((1 - over) * MARGINS["PS"]), 2)})
    books = []
    for _ in range(6):
        bp, bo = _probs(blh * np.exp(rng.normal(0, 0.05)), bla * np.exp(rng.normal(0, 0.05)))
        books.append(np.r_[1 / (bp * MARGINS["Avg"]), 1 / (bo * MARGINS["Avg"]), 1 / ((1 - bo) * MARGINS["Avg"])])
    books = np.array(books)
    for pre, vals in (("Avg", books.mean(axis=0)), ("Max", books.max(axis=0)), ("B365", books[0])):
        vals = np.round(vals, 2)
        out.update({f"{pre}H": vals[0], f"{pre}D": vals[1], f"{pre}A": vals[2], f"{pre}>2.5": vals[3], f"{pre}<2.5": vals[4]})
    line = float(np.clip(-np.round(4 * (blh - bla) * 0.9) / 4, -2.5, 2.5))
    out["AHh"] = line
    for pre, base in (("B365AH", 1.90), ("PAH", 1.95), ("MaxAH", 2.0), ("AvgAH", 1.91)):
        out[f"{pre}H"] = round(base + rng.normal(0, 0.04), 2)
        out[f"{pre}A"] = round(base + rng.normal(0, 0.04), 2)
    return out

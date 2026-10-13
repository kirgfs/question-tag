"""Markets, odds columns of football-data.co.uk, margin removal and settlement."""
from __future__ import annotations

import numpy as np

MARKETS = {"1X2": "Исход матча", "OU25": "Тотал 2.5"}
SELECTIONS = {"1X2": ["H", "D", "A"], "OU25": ["O", "U"]}
SEL_LABEL = {
    ("1X2", "H"): "П1",
    ("1X2", "D"): "Х",
    ("1X2", "A"): "П2",
    ("OU25", "O"): "ТБ 2.5",
    ("OU25", "U"): "ТМ 2.5",
}

# Pre-match columns are present both in season files and in fixtures.csv,
# closing ("*_close") columns only in season files once the match is played.
ODDS_COLS: dict[str, dict[str, tuple[str, ...]]] = {
    "1X2": {
        "pinnacle": ("PSH", "PSD", "PSA"),
        "avg": ("AvgH", "AvgD", "AvgA"),
        "max": ("MaxH", "MaxD", "MaxA"),
        "b365": ("B365H", "B365D", "B365A"),
        "pinnacle_close": ("PSCH", "PSCD", "PSCA"),
        "avg_close": ("AvgCH", "AvgCD", "AvgCA"),
    },
    "OU25": {
        "pinnacle": ("P>2.5", "P<2.5"),
        "avg": ("Avg>2.5", "Avg<2.5"),
        "max": ("Max>2.5", "Max<2.5"),
        "b365": ("B365>2.5", "B365<2.5"),
        "pinnacle_close": ("PC>2.5", "PC<2.5"),
        "avg_close": ("AvgC>2.5", "AvgC<2.5"),
    },
}
AH_COLS = {
    "line": "AHh",
    "pinnacle": ("PAHH", "PAHA"),
    "avg": ("AvgAHH", "AvgAHA"),
    "max": ("MaxAHH", "MaxAHA"),
}


def all_odds_columns() -> list[str]:
    cols = [c for m in ODDS_COLS.values() for t in m.values() for c in t]
    cols += [AH_COLS["line"], *AH_COLS["pinnacle"], *AH_COLS["avg"], *AH_COLS["max"]]
    return cols


def shin_probs(odds: np.ndarray, iters: int = 60) -> np.ndarray:
    """Shin margin removal, vectorised over rows.

    ``odds`` is an (n, k) array of decimal odds. Rows with a missing or
    invalid price come back as NaN. Same result as
    ``penaltyblog.implied.calculate_implied(..., method="shin")``.
    """
    odds = np.asarray(odds, dtype=float)
    if odds.ndim == 1:
        return shin_probs(odds[None, :], iters)[0]
    out = np.full(odds.shape, np.nan)
    ok = np.all(np.isfinite(odds) & (odds > 1.0), axis=1)
    if not ok.any():
        return out
    pi = 1.0 / odds[ok]
    book = pi.sum(axis=1, keepdims=True)

    def probs(z):
        z = z[:, None]
        return (np.sqrt(z**2 + 4 * (1 - z) * pi**2 / book) - z) / (2 * (1 - z))

    lo = np.zeros(len(pi))
    hi = np.full(len(pi), 0.5)
    # sum(probs) decreases in z; bisection is robust for any number of outcomes
    for _ in range(iters):
        mid = (lo + hi) / 2
        too_big = probs(mid).sum(axis=1) > 1.0
        lo = np.where(too_big, mid, lo)
        hi = np.where(too_big, hi, mid)
    p = probs((lo + hi) / 2)
    # Books with no margin (sum of 1/odds <= 1) are just normalised
    flat = book[:, 0] <= 1.0
    p[flat] = pi[flat] / book[flat]
    out[ok] = p / p.sum(axis=1, keepdims=True)
    return out


def outcome_index(market: str, fthg: int, ftag: int) -> int:
    if market == "1X2":
        return 0 if fthg > ftag else (1 if fthg == ftag else 2)
    if market == "OU25":
        return 0 if fthg + ftag > 2.5 else 1
    raise ValueError(market)


def settle(market: str, selection: str, fthg: int, ftag: int) -> str:
    idx = SELECTIONS[market].index(selection)
    return "won" if outcome_index(market, fthg, ftag) == idx else "lost"


def profit_for(status: str, odds: float, stake: float) -> float:
    if status == "won":
        return round(stake * (odds - 1), 2)
    if status == "lost":
        return -round(stake, 2)
    return 0.0


def kelly_fraction(p: float, odds: float) -> float:
    """Full-Kelly share of bankroll for decimal ``odds`` and win probability ``p``."""
    if odds <= 1:
        return 0.0
    return max(0.0, (p * odds - 1) / (odds - 1))


def ah_win_lose(grid: np.ndarray, side: str, line: float) -> tuple[float, float]:
    """Stake-weighted win/lose probabilities of an Asian handicap bet.

    ``line`` is the handicap of the chosen side (football-data's AHh is the
    home line). Quarter lines are split in half between adjacent lines, so
    EV per unit = win * (odds - 1) - lose.
    """
    i, j = np.indices(grid.shape)
    gd = (i - j) if side == "home" else (j - i)

    def single(l):
        win = grid[gd + l > 0].sum()
        lose = grid[gd + l < 0].sum()
        return win, lose

    q = round(line * 4)
    if q % 2:  # quarter line
        a, b = single((q - 1) / 4), single((q + 1) / 4)
        return (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
    return single(line)

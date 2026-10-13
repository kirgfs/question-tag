"""Prediction models.

Layer 1 — penaltyblog: Dixon-Coles goal model (time-decayed) and Pi-ratings.
Layer 2 — stacked ensemble on top of layer-1 outputs plus the de-margined
market price: multinomial logistic regression and gradient boosting
(XGBoost if installed, otherwise scikit-learn) with isotonic calibration.

Everything is evaluated strictly out of sample: Dixon-Coles is refitted
every week on past matches only (walk-forward), Pi-ratings are updated
sequentially, and stackers are trained on earlier seasons and tested on the
next one. The model with the best log-loss becomes the "champion".
"""
from __future__ import annotations

import warnings
from typing import Callable

import numpy as np
import pandas as pd
import penaltyblog as pb
from scipy.optimize import minimize
from scipy.stats import norm
from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from .markets import ODDS_COLS, SELECTIONS, ah_win_lose, outcome_index, shin_probs

try:  # optional, falls back to scikit-learn's histogram gradient boosting
    from xgboost import XGBClassifier
except Exception:  # pragma: no cover - depends on the environment
    XGBClassifier = None

Log = Callable[[str], None]

XI = 0.0018               # Dixon-Coles time decay per day (half-life ≈ 385 days)
WINDOW_DAYS = 3 * 365     # matches older than this are ignored
MIN_TEAM_MATCHES = 6      # both teams need this many matches in the window
MAX_GOALS = 10
MIN_TRAIN_ROWS = 300      # minimum rows to train a stacker
DC_COLS = ["dc_h", "dc_d", "dc_a", "dc_over", "dc_lh", "dc_la"]
MODEL_NAMES = {
    "market": "Рынок (Pinnacle без маржи)",
    "dc": "Dixon-Coles",
    "pi": "Pi-рейтинг",
    "pool": "Ансамбль: взвешенное смешивание",
    "lr": "Ансамбль: логистическая регрессия",
    "gbm": "Ансамбль: XGBoost + изотоника" if XGBClassifier else "Ансамбль: бустинг + изотоника",
}
ENSEMBLES = ("pool", "lr", "gbm")
CANDIDATES = ("market", "dc", "pool", "lr", "gbm")   # models allowed to become champion


# ---------------------------------------------------------------- layer 1 ---

def fit_dc(train: pd.DataFrame, base_date: pd.Timestamp):
    """Fit a time-weighted Dixon-Coles model on matches before ``base_date``."""
    train = train[(train["date"] < base_date) & (train["date"] >= base_date - pd.Timedelta(days=WINDOW_DAYS))]
    if len(train) < 60:
        return None, set()
    counts = pd.concat([train["home"], train["away"]]).value_counts()
    teams_ok = set(counts[counts >= MIN_TEAM_MATCHES].index)
    weights = pb.models.dixon_coles_weights(list(train["date"]), XI, base_date)
    model = pb.models.DixonColesGoalModel(
        train["fthg"].to_numpy(), train["ftag"].to_numpy(),
        train["home"].to_numpy(), train["away"].to_numpy(), weights=weights,
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            model.fit()
        except Exception:
            return None, set()
    return model, teams_ok


def dc_grid(model, home: str, away: str) -> np.ndarray | None:
    """Score probability grid, or None if the fit is degenerate for this pair."""
    try:
        return np.asarray(model.predict(home, away, max_goals=MAX_GOALS).grid, dtype=float)
    except (ValueError, FloatingPointError):
        return None


def grid_summary(grid: np.ndarray) -> dict:
    i, j = np.indices(grid.shape)
    return {
        "dc_h": float(grid[i > j].sum()),
        "dc_d": float(grid[i == j].sum()),
        "dc_a": float(grid[i < j].sum()),
        "dc_over": float(grid[i + j > 2.5].sum()),
        "dc_lh": float((grid.sum(axis=1) * np.arange(grid.shape[0])).sum()),
        "dc_la": float((grid.sum(axis=0) * np.arange(grid.shape[1])).sum()),
    }


def walk_forward(hist: pd.DataFrame, done: set[str], log: Log = print) -> pd.DataFrame:
    """Out-of-sample Dixon-Coles predictions for played matches not in ``done``.

    For every league the first season only warms the model up. After that the
    model is refitted each week using matches strictly before that week.
    Matches that cannot be predicted (too few games for a team) get NaNs so
    they are not retried.
    """
    rows = []
    for div, g in hist.groupby("div"):
        seasons = sorted(g["season"].unique())
        if len(seasons) < 2:
            continue
        start = g.loc[g["season"] == seasons[1], "date"].min()
        todo = g[(g["date"] >= start) & ~g["key"].isin(done)]
        if todo.empty:
            continue
        week = (todo["date"] - start).dt.days // 7
        n_weeks = week.nunique()
        log(f"Dixon-Coles {div}: пошаговое обучение, недель: {n_weeks}")
        for w, chunk in todo.groupby(week):
            cutoff = start + pd.Timedelta(days=7 * int(w))
            model, teams_ok = fit_dc(g, cutoff)
            for r in chunk.itertuples():
                row = {"key": r.key, "div": div, **{c: np.nan for c in DC_COLS}}
                if model is not None and r.home in teams_ok and r.away in teams_ok:
                    grid = dc_grid(model, r.home, r.away)
                    if grid is not None:
                        row.update(grid_summary(grid))
                rows.append(row)
    return pd.DataFrame(rows, columns=["key", "div", *DC_COLS])


def pi_ratings(hist: pd.DataFrame, fixtures: pd.DataFrame) -> pd.DataFrame:
    """Pre-match Pi-rating expected goal difference for history and fixtures.

    Teams that appear after the first season (promoted) start from the
    average of the three weakest teams instead of zero.
    """
    out = []
    for div, g in hist.groupby("div"):
        g = g.sort_values("date")
        first_season = g["season"].iloc[0]
        system = pb.ratings.PiRatingSystem()

        def ensure(team: str, season: str):
            if team in system.team_ratings:
                return
            if season != first_season and len(system.team_ratings) >= 3:
                weakest = sorted(system.team_ratings.values(), key=lambda r: r["home"] + r["away"])[:3]
                system.team_ratings[team] = {
                    "home": float(np.mean([r["home"] for r in weakest])),
                    "away": float(np.mean([r["away"] for r in weakest])),
                }
            else:
                system.initialize_team(team)

        for r in g.itertuples():
            ensure(r.home, r.season)
            ensure(r.away, r.season)
            out.append((r.key, system.expected_goal_difference(r.home, r.away)))
            system.update_ratings(r.home, r.away, r.fthg - r.ftag)
        if not fixtures.empty:
            for r in fixtures[fixtures["div"] == div].itertuples():
                ensure(r.home, r.season)
                ensure(r.away, r.season)
                out.append((r.key, system.expected_goal_difference(r.home, r.away)))
    df = pd.DataFrame(out, columns=["key", "pi_gd"]).drop_duplicates("key", keep="last")
    gd = df["pi_gd"].to_numpy()
    # Same formula as PiRatingSystem.calculate_match_probabilities (sigma=1)
    df["pi_h"] = 1 - norm.cdf(0.5, loc=gd)
    df["pi_a"] = norm.cdf(-0.5, loc=gd)
    df["pi_d"] = 1 - df["pi_h"] - df["pi_a"]
    return df


# ------------------------------------------------------------ market data ---

def fair(df: pd.DataFrame, market: str, source: str) -> np.ndarray:
    cols = list(ODDS_COLS[market][source])
    return shin_probs(df[cols].to_numpy(dtype=float))


def reference_fair(df: pd.DataFrame, market: str, close: bool = False) -> np.ndarray:
    """De-margined probabilities: Pinnacle if quoted, otherwise market average."""
    suffix = "_close" if close else ""
    p = fair(df, market, "pinnacle" + suffix)
    alt = fair(df, market, "avg" + suffix)
    missing = np.isnan(p).any(axis=1)
    p[missing] = alt[missing]
    return p


def build_dataset(frame: pd.DataFrame, market: str, played: bool) -> pd.DataFrame:
    """Rows with layer-1 outputs, market probabilities, odds and the outcome."""
    k = len(SELECTIONS[market])
    ds = frame.copy()
    mk = reference_fair(ds, market)
    for i in range(k):
        ds[f"mk{i}"] = mk[:, i]
    if played:
        close = reference_fair(ds, market, close=True)
        for i in range(k):
            ds[f"close{i}"] = close[:, i]
        ds["y"] = [outcome_index(market, h, a) for h, a in zip(ds["fthg"], ds["ftag"])]
    need = DC_COLS + ["pi_gd"] + [f"mk{i}" for i in range(k)]
    return ds.dropna(subset=need).reset_index(drop=True)


def _logit(p):
    p = np.clip(np.asarray(p, dtype=float), 1e-4, 1 - 1e-4)
    return np.log(p / (1 - p))


def features(ds: pd.DataFrame, market: str) -> np.ndarray:
    """Stacker inputs in log-odds space, so the market price is reproducible exactly."""
    if market == "1X2":
        dch, dcd, dca = (ds[c].to_numpy(dtype=float).clip(1e-4, 1) for c in ("dc_h", "dc_d", "dc_a"))
        mkh, mkd, mka = (ds[c].to_numpy(dtype=float).clip(1e-4, 1) for c in ("mk0", "mk1", "mk2"))
        cols = [
            np.log(mkh / mkd), np.log(mka / mkd), np.log(dch / dcd), np.log(dca / dcd),
            ds["pi_gd"], ds["dc_lh"], ds["dc_la"], dch - mkh, dca - mka,
        ]
    else:
        cols = [
            _logit(ds["mk0"]), _logit(ds["dc_over"]),
            ds["dc_lh"] + ds["dc_la"], np.abs(ds["pi_gd"]),
            ds["dc_over"] - ds["mk0"],
        ]
    return np.column_stack([np.asarray(c, dtype=float) for c in cols])


def layer1_probs(ds: pd.DataFrame, market: str, name: str) -> np.ndarray | None:
    k = len(SELECTIONS[market])
    if name == "market":
        return ds[[f"mk{i}" for i in range(k)]].to_numpy(dtype=float)
    if name == "dc":
        if market == "1X2":
            p = ds[["dc_h", "dc_d", "dc_a"]].to_numpy(dtype=float)
            return p / p.sum(axis=1, keepdims=True)
        over = ds["dc_over"].to_numpy(dtype=float)
        return np.column_stack([over, 1 - over])
    if name == "pi" and market == "1X2":
        return ds[["pi_h", "pi_d", "pi_a"]].to_numpy(dtype=float)
    return None


class OpinionPool:
    """Logarithmic opinion pool: p ∝ Π source_i ** w_i, weights fitted by log-loss.

    Starts from "trust the market" (w = 1, 0, 0) and moves away from it only
    as far as the data supports.
    """

    def __init__(self, market: str):
        self.market = market
        self.sources = ["market", "dc"] + (["pi"] if market == "1X2" else [])
        self.weights = np.r_[1.0, np.zeros(len(self.sources) - 1)]

    def _logs(self, ds):
        return [np.log(np.clip(layer1_probs(ds, self.market, s), 1e-6, 1)) for s in self.sources]

    @staticmethod
    def _combine(w, logs):
        z = sum(wi * li for wi, li in zip(w, logs))
        z = z - z.max(axis=1, keepdims=True)
        p = np.exp(z)
        return p / p.sum(axis=1, keepdims=True)

    def fit(self, ds: pd.DataFrame, y: np.ndarray):
        logs = self._logs(ds)
        idx = np.arange(len(y))

        def nll(w):
            return -np.mean(np.log(self._combine(w, logs)[idx, y] + 1e-12))

        res = minimize(nll, self.weights, method="L-BFGS-B", bounds=[(-1, 3)] * len(self.sources))
        self.weights = res.x
        return self

    def predict(self, ds: pd.DataFrame) -> np.ndarray:
        return self._combine(self.weights, self._logs(ds))


def make_stacker(name: str):
    if name == "lr":
        return make_pipeline(StandardScaler(), LogisticRegression(C=1.0, max_iter=2000))
    if XGBClassifier is not None:
        base = XGBClassifier(
            n_estimators=250, max_depth=3, learning_rate=0.03, subsample=0.8,
            colsample_bytree=0.8, min_child_weight=25, reg_lambda=5.0,
            n_jobs=2, verbosity=0,
        )
    else:
        base = HistGradientBoostingClassifier(
            max_depth=3, learning_rate=0.04, max_iter=250,
            l2_regularization=1.0, min_samples_leaf=50,
        )
    return CalibratedClassifierCV(base, method="isotonic", cv=3)


def fit_ensemble(name: str, ds: pd.DataFrame, market: str):
    y = ds["y"].to_numpy().astype(int)
    if name == "pool":
        return OpinionPool(market).fit(ds, y)
    return make_stacker(name).fit(features(ds, market), y)


def predict_ensemble(fitted, ds: pd.DataFrame, market: str) -> np.ndarray:
    if isinstance(fitted, OpinionPool):
        return fitted.predict(ds)
    k = len(SELECTIONS[market])
    raw = fitted.predict_proba(features(ds, market))
    p = np.full((len(ds), k), 1e-6)
    for col, cls in enumerate(fitted.classes_):
        p[:, int(cls)] = raw[:, col]
    p = np.clip(p, 1e-6, 1)
    return p / p.sum(axis=1, keepdims=True)


def predict_with(name: str, fitted, ds: pd.DataFrame, market: str) -> np.ndarray | None:
    if name in ENSEMBLES:
        return predict_ensemble(fitted, ds, market) if fitted is not None else None
    return layer1_probs(ds, market, name)


# ------------------------------------------------------------- evaluation ---

def log_loss(p: np.ndarray, y: np.ndarray) -> float:
    return float(-np.mean(np.log(np.clip(p[np.arange(len(y)), y], 1e-12, 1))))


def brier(p: np.ndarray, y: np.ndarray) -> float:
    onehot = np.eye(p.shape[1])[y]
    return float(np.mean(((p - onehot) ** 2).sum(axis=1)))


def rps(p: np.ndarray, y: np.ndarray) -> float:
    """Ranked probability score (ordered outcomes), lower is better."""
    k = p.shape[1]
    if k < 3:
        return brier(p, y) / 2
    onehot = np.eye(k)[y]
    cp, co = np.cumsum(p, axis=1)[:, :-1], np.cumsum(onehot, axis=1)[:, :-1]
    return float(np.mean(((cp - co) ** 2).sum(axis=1) / (k - 1)))


def evaluate(ds: pd.DataFrame, market: str, log: Log = print) -> pd.DataFrame:
    """Season-by-season out-of-sample predictions of every model (long format)."""
    k = len(SELECTIONS[market])
    seasons = sorted(ds["season"].unique())
    parts = []
    for s in seasons:
        train, test = ds[ds["season"] < s], ds[ds["season"] == s]
        if len(train) < MIN_TRAIN_ROWS or test.empty:
            continue
        log(f"Проверка {market}: сезон {s[:2]}/{s[2:]}, обучение на {len(train)} матчах")
        probs = {n: layer1_probs(test, market, n) for n in ("market", "dc", "pi")}
        for name in ENSEMBLES:
            probs[name] = predict_ensemble(fit_ensemble(name, train, market), test, market)
        for name, p in probs.items():
            if p is None:
                continue
            part = test[["key", "div", "season", "date", "y"] + [f"close{i}" for i in range(k)]].copy()
            for src in ("avg", "max", "pinnacle", "b365"):
                cols = ODDS_COLS[market][src]
                for i in range(k):
                    part[f"{src}{i}"] = test[cols[i]].to_numpy()
            part["market"], part["model"] = market, name
            for i in range(k):
                part[f"p{i}"] = p[:, i]
            parts.append(part)
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def metrics(evals: pd.DataFrame) -> list[dict]:
    """Quality of every model on the same out-of-sample matches.

    ``delta``/``delta_se``: mean per-match log-loss difference to the market
    and its standard error (negative = better than the market).
    """
    out = []
    if evals.empty:
        return out
    for market, gm in evals.groupby("market"):
        k = len(SELECTIONS[market])
        common = set.intersection(*(set(g["key"]) for _, g in gm.groupby("model")))
        rows, losses = [], {}
        for name, g in gm.groupby("model"):
            g = g[g["key"].isin(common)].drop_duplicates("key").sort_values("key")
            p = g[[f"p{i}" for i in range(k)]].to_numpy(dtype=float)
            y = g["y"].to_numpy().astype(int)
            losses[name] = -np.log(np.clip(p[np.arange(len(y)), y], 1e-12, 1))
            rows.append({
                "market": market, "model": name, "title": MODEL_NAMES[name], "n": int(len(g)),
                "logloss": log_loss(p, y), "brier": brier(p, y), "rps": rps(p, y),
                "accuracy": float(np.mean(p.argmax(axis=1) == y)),
            })
        base = losses.get("market")
        for r in rows:
            d = losses[r["model"]] - base if base is not None else None
            r["delta"] = float(d.mean()) if d is not None and len(d) else None
            r["delta_se"] = float(d.std(ddof=1) / np.sqrt(len(d))) if d is not None and len(d) > 1 else None
        out.extend(rows)
    return out


def choose_champion(market_metrics: list[dict]) -> str:
    """Best log-loss, but a model replaces the market only if it beats it by more
    than one standard error — otherwise we would be chasing noise."""
    cands = [
        m for m in market_metrics
        if m["model"] in CANDIDATES and (
            m["model"] == "market"
            or (m.get("delta") is not None and m.get("delta_se") is not None and m["delta"] + m["delta_se"] < 0)
        )
    ]
    if not cands:
        return "market"
    return min(cands, key=lambda m: m["logloss"])["model"]


# ------------------------------------------------------------- production ---

def train_production(ds: pd.DataFrame, market: str, champion: str):
    if champion in ENSEMBLES and len(ds) >= MIN_TRAIN_ROWS:
        return fit_ensemble(champion, ds, market)
    return None


def predict_upcoming(hist: pd.DataFrame, fixtures: pd.DataFrame, now: pd.Timestamp, log: Log = print):
    """Dixon-Coles outputs for upcoming fixtures, fitted on all played matches."""
    rows, extras = [], {}
    cutoff = now.tz_convert("UTC").tz_localize(None).normalize() + pd.Timedelta(days=1)
    for div, fx in fixtures.groupby("div"):
        model, teams_ok = fit_dc(hist[hist["div"] == div], cutoff)
        if model is None:
            log(f"⚠ {div}: мало данных для Dixon-Coles")
        for r in fx.itertuples():
            row = {"key": r.key, **{c: np.nan for c in DC_COLS}}
            if model is not None and r.home in teams_ok and r.away in teams_ok:
                grid = dc_grid(model, r.home, r.away)
                if grid is not None:
                    row.update(grid_summary(grid))
                    extras[r.key] = grid_extras(grid, getattr(r, "AHh", np.nan))
            rows.append(row)
    return pd.DataFrame(rows, columns=["key", *DC_COLS]), extras


def grid_extras(grid: np.ndarray, ah_line: float) -> dict:
    """Likely scores and fair Asian handicap odds for the match card."""
    flat = [(float(grid[h, a]), h, a) for h in range(grid.shape[0]) for a in range(grid.shape[1])]
    top = sorted(flat, reverse=True)[:5]
    extras = {"scores": [{"score": f"{h}:{a}", "p": p} for p, h, a in top]}
    i, j = np.indices(grid.shape)
    extras["btts"] = float(grid[(i > 0) & (j > 0)].sum())
    if ah_line is not None and np.isfinite(ah_line):
        ah = {"line": float(ah_line)}
        for side, line in (("home", ah_line), ("away", -ah_line)):
            win, lose = ah_win_lose(grid, side, line)
            ah[side] = {"win": win, "lose": lose, "fair_odds": (1 + lose / win) if win > 0 else None}
        extras["ah"] = ah
    return extras


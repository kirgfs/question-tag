"""Statistics of the user's bets, the bot's paper signals and the historical backtest."""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import config
from .markets import SEL_LABEL, SELECTIONS

ODDS_BUCKETS = [(1.0, 1.7), (1.7, 2.2), (2.2, 3.0), (3.0, 5.0), (5.0, 1000.0)]


def _group(items: list[dict], keyfn) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for it in items:
        groups.setdefault(keyfn(it), []).append(it)
    out = []
    for name, g in groups.items():
        s = _basic(g)
        s["name"] = name
        out.append(s)
    return sorted(out, key=lambda s: -s["n"])


def _basic(items: list[dict]) -> dict:
    settled = [b for b in items if b["status"] in ("won", "lost")]
    staked = sum(b["stake"] for b in settled)
    profit = sum(b["profit"] or 0 for b in settled)
    won = sum(b["status"] == "won" for b in settled)
    return {
        "n": len(settled), "won": won, "staked": staked, "profit": profit,
        "roi": profit / staked if staked else None,
        "hit_rate": won / len(settled) if settled else None,
    }


def summarize(items: list[dict], start_bankroll: float | None = None) -> dict:
    """Aggregate a list of bets: dicts with odds, stake, status, profit, ev, clv, kickoff..."""
    settled = [b for b in items if b["status"] in ("won", "lost", "void")]
    decided = [b for b in settled if b["status"] != "void"]
    open_ = [b for b in items if b["status"] == "open"]
    s = _basic(items)
    with_clv = [b for b in decided if b.get("clv") is not None]
    with_ev = [b for b in decided if b.get("ev") is not None]
    returns = [(b["profit"] or 0) / b["stake"] for b in decided if b["stake"]]
    s.update({
        "roi_se": float(np.std(returns) / np.sqrt(len(returns))) if len(returns) > 1 else None,
        "total": len(items),
        "open": len(open_),
        "open_stake": sum(b["stake"] for b in open_),
        "void": sum(b["status"] == "void" for b in settled),
        "lost": sum(b["status"] == "lost" for b in settled),
        "avg_odds": float(np.mean([b["odds"] for b in decided])) if decided else None,
        "avg_ev": float(np.mean([b["ev"] for b in with_ev])) if with_ev else None,
        "expected_profit": float(sum(b["ev"] * b["stake"] for b in with_ev)) if with_ev else None,
        "avg_clv": float(np.mean([b["clv"] for b in with_clv])) if with_clv else None,
        "beat_close": float(np.mean([b["clv"] > 0 for b in with_clv])) if with_clv else None,
        "n_clv": len(with_clv),
    })
    ordered = sorted(settled, key=lambda b: (b["kickoff"] or "", b.get("id") or 0))
    level = start_bankroll or 0.0
    peak, max_dd = level, 0.0
    curve = [{"t": None, "v": level}]
    for b in ordered:
        level += b["profit"] or 0
        peak = max(peak, level)
        max_dd = max(max_dd, peak - level)
        curve.append({"t": b["kickoff"], "v": round(level, 2)})
    s["curve"] = curve
    s["max_drawdown"] = max_dd
    s["max_drawdown_pct"] = max_dd / peak if peak > 0 else None
    s["by_league"] = _group(decided, lambda b: config.LEAGUES.get(b["div"], b["div"]))
    s["by_selection"] = _group(decided, lambda b: SEL_LABEL.get((b["market"], b["selection"]), b["selection"]))
    s["by_odds"] = _group(decided, _odds_bucket)
    return s


def _odds_bucket(b: dict) -> str:
    for lo, hi in ODDS_BUCKETS:
        if lo <= b["odds"] < hi:
            return f"{lo:.1f}–{hi:.1f}" if hi < 100 else f"{lo:.1f}+"
    return "?"


# --------------------------------------------------------------- backtest ---

def _pick(evals: pd.DataFrame, market: str, settings: dict, min_ev: float) -> pd.DataFrame:
    """Flat 1-unit bets the strategy would have placed on historical matches."""
    k = len(SELECTIONS[market])
    p = evals[[f"p{i}" for i in range(k)]].to_numpy(dtype=float)
    src = settings["price_source"]
    price = evals[[f"{src}{i}" for i in range(k)]].to_numpy(dtype=float)
    avg = evals[[f"avg{i}" for i in range(k)]].to_numpy(dtype=float)
    price = np.where(np.isfinite(price) & (price > 1), price, avg)
    ev = p * price - 1
    ok = np.isfinite(ev) & (ev >= min_ev) & (price >= settings["min_odds"]) & (price <= settings["max_odds"])
    ev_masked = np.where(ok, ev, -np.inf)
    best = ev_masked.argmax(axis=1)
    has = np.isfinite(ev_masked.max(axis=1))
    rows = np.arange(len(evals))[has]
    sel = best[has]
    out = pd.DataFrame({
        "date": evals["date"].to_numpy()[rows],
        "season": evals["season"].to_numpy()[rows],
        "div": evals["div"].to_numpy()[rows],
        "market": market,
        "odds": price[rows, sel],
        "ev": ev[rows, sel],
        "won": evals["y"].to_numpy()[rows].astype(int) == sel,
    })
    close = evals[[f"close{i}" for i in range(k)]].to_numpy(dtype=float)
    out["clv"] = out["odds"].to_numpy() * close[rows, sel] - 1
    out["profit"] = np.where(out["won"], out["odds"] - 1, -1.0)
    return out


def _agg(b: pd.DataFrame) -> dict:
    if b.empty:
        return {"n": 0, "profit": 0.0, "roi": None, "roi_se": None, "hit_rate": None,
                "avg_odds": None, "avg_ev": None, "avg_clv": None}
    clv = b["clv"].dropna()
    return {
        "n": int(len(b)), "profit": float(b["profit"].sum()), "roi": float(b["profit"].mean()),
        "roi_se": float(b["profit"].std() / np.sqrt(len(b))) if len(b) > 1 else None,
        "hit_rate": float(b["won"].mean()), "avg_odds": float(b["odds"].mean()),
        "avg_ev": float(b["ev"].mean()), "avg_clv": float(clv.mean()) if len(clv) else None,
    }


def backtest(evals: pd.DataFrame, settings: dict, champions: dict[str, str]) -> dict:
    """Out-of-sample backtest of the current staking rules (flat 1 unit per bet)."""
    if evals.empty:
        return {"available": False}
    evals = evals[evals["div"].isin(settings["leagues"])]
    markets = [m for m in settings["markets"] if m in champions]

    def bets_for(model_of, min_ev):
        parts = []
        for m in markets:
            e = evals[(evals["market"] == m) & (evals["model"] == model_of(m))]
            if not e.empty:
                parts.append(_pick(e, m, settings, min_ev))
        return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(
            columns=["date", "season", "div", "market", "odds", "ev", "won", "clv", "profit"])

    champ = bets_for(lambda m: champions[m], settings["min_ev"])
    champ = champ.sort_values("date").reset_index(drop=True)
    result = {"available": True, "summary": _agg(champ)}
    result["by_market"] = [{"name": m, **_agg(champ[champ["market"] == m])} for m in markets]
    result["by_season"] = [{"name": f"{s[:2]}/{s[2:]}", **_agg(g)} for s, g in champ.groupby("season")]
    result["by_league"] = [{"name": config.LEAGUES.get(d, d), **_agg(g)} for d, g in champ.groupby("div")]
    result["thresholds"] = [
        {"min_ev": t, **_agg(bets_for(lambda m: champions[m], t))} for t in (0.0, 0.02, 0.05, 0.08, 0.12)
    ]
    result["by_model"] = [
        {"model": name, **_agg(bets_for(lambda m, name=name: name, settings["min_ev"]))}
        for name in ("market", "dc", "pool", "lr", "gbm")
    ]
    cum = champ["profit"].cumsum().to_numpy()
    step = max(1, len(cum) // 300)
    result["curve"] = [{"t": str(champ["date"].iloc[i]), "v": round(float(cum[i]), 2)} for i in range(0, len(cum), step)]
    if len(cum) and (len(cum) - 1) % step:
        result["curve"].append({"t": str(champ["date"].iloc[-1]), "v": round(float(cum[-1]), 2)})
    return result

"""Update pipeline: data → models → fixtures → signals → settlement."""
from __future__ import annotations

import json
import threading
import traceback
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from . import config, data, db, model
from .markets import AH_COLS, MARKETS, ODDS_COLS, SELECTIONS, kelly_fraction, profit_for, settle

_lock = threading.Lock()
STATUS: dict = {"running": False, "step": "", "log": [], "error": None, "started_at": None, "finished_at": None}
ECHO = False  # also print progress to the console (CLI mode)


def _log(msg: str) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    STATUS["step"] = msg
    STATUS["log"] = (STATUS["log"] + [f"{stamp} {msg}"])[-200:]
    if ECHO:
        print(f"{stamp} {msg}", flush=True)


def utcnow() -> pd.Timestamp:
    return pd.Timestamp(datetime.now(timezone.utc))


def start_update_thread() -> bool:
    """Run :func:`run_update` in the background. False if one is already running."""
    if STATUS["running"]:
        return False
    threading.Thread(target=run_update, daemon=True).start()
    return True


def run_update() -> bool:
    if not _lock.acquire(blocking=False):
        return False
    STATUS.update(running=True, error=None, log=[], started_at=utcnow().isoformat(), finished_at=None)
    try:
        _run(_log)
        _log("Готово")
        return True
    except Exception as e:  # shown in the UI
        STATUS["error"] = f"{type(e).__name__}: {e}"
        _log("Ошибка: " + STATUS["error"])
        traceback.print_exc()
        return False
    finally:
        STATUS.update(running=False, finished_at=utcnow().isoformat())
        _lock.release()


def _run(log) -> None:
    db.init()
    now = utcnow()
    with db.session() as conn:
        settings = db.get_settings(conn)
        cached = pd.read_sql_query("SELECT * FROM dc_cache", conn)
    leagues = [d for d in settings["leagues"] if d in config.LEAGUES]
    if not leagues:
        raise RuntimeError("Не выбрано ни одной лиги в настройках")

    log("Загрузка истории матчей с football-data.co.uk")
    hist = data.load_history(leagues, now.date(), log)
    if hist.empty:
        raise RuntimeError("Нет исторических данных — проверьте интернет и список лиг")
    log("Загрузка ближайших матчей (fixtures.csv)")
    fixtures = data.load_fixtures(leagues, log)

    new = model.walk_forward(hist, set(cached["key"]), log)
    if not new.empty:
        with db.session() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO dc_cache VALUES (?,?,?,?,?,?,?,?)",
                [tuple(None if pd.isna(v) else v for v in r) for r in new[["key", "div", *model.DC_COLS]].itertuples(index=False)],
            )
    dc = pd.concat([cached, new], ignore_index=True).drop_duplicates("key", keep="last").drop(columns="div")
    dc[model.DC_COLS] = dc[model.DC_COLS].astype(float)
    log("Pi-рейтинги")
    pi = model.pi_ratings(hist, fixtures)
    played = hist.merge(dc, on="key", how="left").merge(pi, on="key", how="left")

    report = {
        "generated_at": now.isoformat(),
        "xgboost": model.XGBClassifier is not None,
        "markets": {},
        "leagues": _league_report(hist, fixtures),
    }
    evals, production = [], {}
    for market in MARKETS:
        ds = model.build_dataset(played, market, played=True)
        ev = model.evaluate(ds, market, log)
        evals.append(ev)
        mets = model.metrics(ev)
        champion = model.choose_champion(mets)
        log(f"{MARKETS[market]}: лучшая модель — {model.MODEL_NAMES[champion]}")
        production[market] = (champion, model.train_production(ds, market, champion))
        report["markets"][market] = {"metrics": mets, "champion": champion, "n_rows": int(len(ds))}
        if len(ds) >= model.MIN_TRAIN_ROWS:
            pool = model.fit_ensemble("pool", ds, market)
            report["markets"][market]["pool_weights"] = dict(zip(pool.sources, pool.weights.round(3).tolist()))

    log("Прогноз ближайших матчей")
    records = []
    if not fixtures.empty:
        fx_dc, extras = model.predict_upcoming(hist, fixtures, now, log)
        upcoming = fixtures.merge(fx_dc, on="key", how="left").merge(pi, on="key", how="left")
        records = fixture_records(upcoming, production, extras)

    evals_df = pd.concat([e for e in evals if not e.empty], ignore_index=True) if any(not e.empty for e in evals) else pd.DataFrame()
    with db.session() as conn:
        conn.execute("DELETE FROM fixtures")
        conn.executemany(
            "INSERT INTO fixtures(key, div, kickoff, home, away, data) VALUES (?,?,?,?,?,?)",
            [(r["key"], r["div"], r["kickoff"], r["home"], r["away"], json.dumps(r, ensure_ascii=False)) for r in records],
        )
        conn.execute("DROP TABLE IF EXISTS eval_preds")
        if not evals_df.empty:
            out = evals_df.copy()
            out["date"] = out["date"].dt.strftime("%Y-%m-%d")
            out.to_sql("eval_preds", conn, index=False)
        db.set_meta(conn, "report", report)
        db.set_meta(conn, "last_update", now.isoformat())
        refresh_tips(conn, db.get_settings(conn), now)  # settings may have changed meanwhile
        settle_open(conn, hist, now)
    log(f"Матчей впереди: {len(records)}")


def _league_report(hist: pd.DataFrame, fixtures: pd.DataFrame) -> list[dict]:
    out = []
    for div, g in hist.groupby("div"):
        out.append({
            "div": div, "name": config.LEAGUES.get(div, div), "matches": int(len(g)),
            "seasons": sorted(g["season"].unique().tolist()),
            "last_result": g["date"].max().strftime("%Y-%m-%d"),
            "upcoming": int((fixtures["div"] == div).sum()) if not fixtures.empty else 0,
        })
    return out


def _clean(x):
    if x is None:
        return None
    x = float(x)
    return None if not np.isfinite(x) else x


def fixture_records(upcoming: pd.DataFrame, production: dict, extras: dict) -> list[dict]:
    """JSON-ready fixture cards with model probabilities and odds per market."""
    final: dict[str, dict[str, list]] = {}
    for market, (champion, fitted) in production.items():
        ds = model.build_dataset(upcoming, market, played=False)
        if ds.empty:
            final[market] = {}
            continue
        p = model.predict_with(champion, fitted, ds, market)
        if p is None:
            p = model.layer1_probs(ds, market, "market")
        final[market] = {key: p[i].tolist() for i, key in enumerate(ds["key"])}

    fair = {m: model.reference_fair(upcoming, m) for m in MARKETS}
    records = []
    for pos, r in enumerate(upcoming.to_dict("records")):
        has_dc = pd.notna(r.get("dc_h"))
        rec = {
            "key": r["key"], "div": r["div"], "league": config.LEAGUES.get(r["div"], r["div"]),
            "kickoff": r["kickoff"].isoformat(), "home": r["home"], "away": r["away"],
            "dc": {c[3:]: _clean(r[c]) for c in model.DC_COLS} if has_dc else None,
            "pi_gd": _clean(r.get("pi_gd")),
            "markets": {},
            "extras": extras.get(r["key"], {}),
        }
        for market in MARKETS:
            dc_p = None
            if has_dc:
                dc_p = [r["dc_h"], r["dc_d"], r["dc_a"]] if market == "1X2" else [r["dc_over"], 1 - r["dc_over"]]
                dc_p = [float(v) / sum(dc_p) for v in dc_p]
            fair_row = fair[market][pos]
            rec["markets"][market] = {
                "model": production[market][0],
                "probs": final[market].get(r["key"]),
                "dc_probs": dc_p,
                "fair": None if np.isnan(fair_row).any() else fair_row.tolist(),
                "odds": {
                    src: [_clean(r.get(c)) for c in ODDS_COLS[market][src]]
                    for src in ("avg", "max", "pinnacle", "b365")
                },
            }
        if pd.notna(r.get(AH_COLS["line"])):
            rec["ah_odds"] = {src: [_clean(r.get(c)) for c in AH_COLS[src]] for src in ("avg", "max", "pinnacle")}
        records.append(rec)
    return records


# ---------------------------------------------------------------- signals ---

def pick_price(odds: dict, source: str, i: int) -> tuple[float | None, str | None]:
    for src in (source, "avg"):
        vals = odds.get(src) or []
        if i < len(vals) and vals[i] and vals[i] > 1:
            return float(vals[i]), src
    return None, None


def evaluate_fixture(rec: dict, market: str, settings: dict) -> dict | None:
    """Best qualifying selection of a fixture in ``market`` or None."""
    m = rec["markets"].get(market)
    if not m or not m.get("probs"):
        return None
    best = None
    for i, sel in enumerate(SELECTIONS[market]):
        price, src = pick_price(m["odds"], settings["price_source"], i)
        if price is None:
            continue
        p = m["probs"][i]
        ev = p * price - 1
        if ev < settings["min_ev"] or not (settings["min_odds"] <= price <= settings["max_odds"]):
            continue
        if best is None or ev > best["ev"]:
            best = {
                "market": market, "selection": sel, "model_prob": p,
                "market_prob": (m["fair"] or [None] * 3)[i], "price": price, "price_source": src,
                "ev": ev, "kelly": kelly_fraction(p, price),
            }
    return best


def refresh_tips(conn, settings: dict, now: pd.Timestamp) -> None:
    """Upsert currently qualifying signals; deactivate the ones that no longer qualify."""
    stamp = now.isoformat()
    keep: list[int] = []
    for row in conn.execute("SELECT data FROM fixtures WHERE kickoff > ?", (stamp,)).fetchall():
        rec = json.loads(row["data"])
        if rec["div"] not in settings["leagues"]:
            continue
        for market in settings["markets"]:
            tip = evaluate_fixture(rec, market, settings)
            if tip is None:
                continue
            conn.execute(
                """INSERT INTO tips(key, div, kickoff, home, away, market, selection, model_prob, market_prob,
                       price, price_source, ev, kelly, first_seen, last_seen, active)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)
                   ON CONFLICT(key, market, selection) DO UPDATE SET
                       kickoff=excluded.kickoff, model_prob=excluded.model_prob, market_prob=excluded.market_prob,
                       price=excluded.price, price_source=excluded.price_source, ev=excluded.ev,
                       kelly=excluded.kelly, last_seen=excluded.last_seen, active=1""",
                (rec["key"], rec["div"], rec["kickoff"], rec["home"], rec["away"], market, tip["selection"],
                 tip["model_prob"], tip["market_prob"], tip["price"], tip["price_source"], tip["ev"],
                 tip["kelly"], stamp, stamp),
            )
            keep.append(conn.execute(
                "SELECT id FROM tips WHERE key=? AND market=? AND selection=?",
                (rec["key"], market, tip["selection"]),
            ).fetchone()["id"])
    marks = ",".join("?" * len(keep))
    keep_clause = f" AND id NOT IN ({marks})" if keep else ""
    conn.execute(f"UPDATE tips SET active=0 WHERE kickoff > ?{keep_clause}", (stamp, *keep))


def current_bankroll(conn, settings: dict) -> float:
    profit = conn.execute("SELECT COALESCE(SUM(profit), 0) FROM bets WHERE status IN ('won','lost','void')").fetchone()[0]
    return float(settings["bankroll"]) + float(profit)


def stake_for(kelly: float, bankroll: float, settings: dict) -> float:
    share = min(settings["kelly_fraction"] * kelly, settings["max_stake_pct"])
    stake = max(0.0, bankroll * share)
    step = 10 if stake >= 100 else 1
    return float(round(stake / step) * step)


# ------------------------------------------------------------- settlement ---

def _result_index(hist: pd.DataFrame) -> dict:
    close = {m: model.reference_fair(hist, m, close=True) for m in MARKETS}
    index: dict[tuple, list] = {}
    for pos, r in enumerate(hist[["div", "home", "away", "date", "fthg", "ftag"]].itertuples(index=False)):
        index.setdefault((r.div, r.home, r.away), []).append((r.date, pos, int(r.fthg), int(r.ftag)))
    return {"index": index, "close": close, "hist": hist}


def find_result(res: dict, div: str, home: str, away: str, kickoff: str):
    ko = pd.Timestamp(kickoff).tz_convert("UTC").tz_localize(None).normalize()
    for date, pos, h, a in res["index"].get((div, home, away), []):
        if ko - pd.Timedelta(days=1) <= date <= ko + pd.Timedelta(days=60):
            return pos, h, a
    return None


def _closing(res: dict, pos: int, market: str, selection: str):
    i = SELECTIONS[market].index(selection)
    p = res["close"][market][pos, i]
    row = res["hist"].iloc[pos]
    raw = None
    for src in ("pinnacle_close", "avg_close"):
        v = row[ODDS_COLS[market][src][i]]
        if pd.notna(v):
            raw = float(v)
            break
    return (None if np.isnan(p) else float(p)), raw


def settle_open(conn, hist: pd.DataFrame, now: pd.Timestamp) -> int:
    """Settle the user's bets and the bot's signals whose results are known."""
    res = _result_index(hist)
    stamp = now.isoformat()
    n = 0
    bets = db.rows(conn, "SELECT * FROM bets WHERE (status='open' OR close_prob IS NULL) AND kickoff < ?", (stamp,))
    for b in bets:
        found = find_result(res, b["div"], b["home"], b["away"], b["kickoff"])
        if not found:
            continue
        pos, h, a = found
        close_p, close_odds = _closing(res, pos, b["market"], b["selection"])
        clv = b["odds"] * close_p - 1 if close_p else None
        if b["status"] == "open":
            status = settle(b["market"], b["selection"], h, a)
            conn.execute(
                "UPDATE bets SET status=?, fthg=?, ftag=?, profit=?, settled_at=?, close_prob=?, close_odds=?, clv=? WHERE id=?",
                (status, h, a, profit_for(status, b["odds"], b["stake"]), stamp, close_p, close_odds, clv, b["id"]),
            )
            n += 1
        else:
            conn.execute(
                "UPDATE bets SET fthg=COALESCE(fthg, ?), ftag=COALESCE(ftag, ?), close_prob=?, close_odds=?, clv=? WHERE id=?",
                (h, a, close_p, close_odds, clv, b["id"]),
            )
    for t in db.rows(conn, "SELECT * FROM tips WHERE status='open' AND kickoff < ?", (stamp,)):
        found = find_result(res, t["div"], t["home"], t["away"], t["kickoff"])
        if not found:
            continue
        pos, h, a = found
        close_p, _ = _closing(res, pos, t["market"], t["selection"])
        status = settle(t["market"], t["selection"], h, a)
        conn.execute(
            "UPDATE tips SET status=?, fthg=?, ftag=?, profit=?, close_prob=?, clv=? WHERE id=?",
            (status, h, a, profit_for(status, t["price"], 1.0), close_p,
             t["price"] * close_p - 1 if close_p else None, t["id"]),
        )
    return n

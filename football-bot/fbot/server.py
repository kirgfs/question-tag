"""Local web app: JSON API + single-page UI."""
from __future__ import annotations

import json
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, db, engine, model, stats
from .markets import MARKETS, SEL_LABEL, SELECTIONS, kelly_fraction, profit_for

STATIC = Path(__file__).parent / "static"
RETRY_AFTER_ERROR_MIN = 30


def _scheduler() -> None:
    """Refresh data every ``auto_update_hours``; the first run happens right away."""
    last_attempt = 0.0
    time.sleep(2)
    while True:
        try:
            with db.session() as conn:
                s = db.get_settings(conn)
                last = db.get_meta(conn, "last_update")
            hours = float(s.get("auto_update_hours") or 0)
            age_h = (engine.utcnow() - pd.Timestamp(last)).total_seconds() / 3600 if last else None
            due = age_h is None or (hours > 0 and age_h >= hours)
            cooled = time.time() - last_attempt > RETRY_AFTER_ERROR_MIN * 60
            if due and cooled and not engine.STATUS["running"]:
                last_attempt = time.time()
                engine.run_update()
        except Exception as e:  # keep the scheduler alive
            print("scheduler:", e)
        time.sleep(60)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init()
    threading.Thread(target=_scheduler, daemon=True).start()
    yield


app = FastAPI(title="Football Value Bot", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


# ------------------------------------------------------------------ helpers ---

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fixtures(conn, upcoming_only: bool = True) -> list[dict]:
    sql = "SELECT data FROM fixtures" + (" WHERE kickoff > ?" if upcoming_only else "") + " ORDER BY kickoff"
    params = (_now_iso(),) if upcoming_only else ()
    return [json.loads(r["data"]) for r in conn.execute(sql, params)]


def _selection_views(rec: dict, settings: dict, bankroll: float) -> dict:
    """Per-selection numbers shown in the UI: probability, prices, EV, stake."""
    views = {}
    for market in MARKETS:
        m = rec["markets"].get(market) or {}
        items = []
        for i, sel in enumerate(SELECTIONS[market]):
            p = (m.get("probs") or [None] * 3)[i]
            price, src = engine.pick_price(m.get("odds") or {}, settings["price_source"], i)
            ev = p * price - 1 if p and price else None
            kelly = kelly_fraction(p, price) if p and price else 0.0
            items.append({
                "selection": sel, "label": SEL_LABEL[(market, sel)], "prob": p,
                "dc_prob": (m.get("dc_probs") or [None] * 3)[i],
                "market_prob": (m.get("fair") or [None] * 3)[i],
                "price": price, "price_source": src, "ev": ev,
                "min_odds": (1 + settings["min_ev"]) / p if p else None,
                "fair_odds": 1 / p if p else None,
                "stake": engine.stake_for(kelly, bankroll, settings) if ev and ev > 0 else 0.0,
                "odds": {s: (v[i] if v and i < len(v) else None) for s, v in (m.get("odds") or {}).items()},
            })
        views[market] = {"model": m.get("model"), "items": items}
    return views


# ------------------------------------------------------------------- routes ---

@app.get("/api/status")
def status():
    with db.session() as conn:
        settings = db.get_settings(conn)
        bankroll = engine.current_bankroll(conn, settings)
        now = _now_iso()
        counts = {
            "fixtures": conn.execute("SELECT COUNT(*) FROM fixtures WHERE kickoff > ?", (now,)).fetchone()[0],
            "signals": conn.execute("SELECT COUNT(*) FROM tips WHERE active=1 AND kickoff > ?", (now,)).fetchone()[0],
            "open_bets": conn.execute("SELECT COUNT(*) FROM bets WHERE status='open'").fetchone()[0],
            "open_stake": conn.execute("SELECT COALESCE(SUM(stake),0) FROM bets WHERE status='open'").fetchone()[0],
        }
        profit = conn.execute("SELECT COALESCE(SUM(profit),0) FROM bets WHERE status IN ('won','lost','void')").fetchone()[0]
        staked = conn.execute("SELECT COALESCE(SUM(stake),0) FROM bets WHERE status IN ('won','lost')").fetchone()[0]
        last = db.get_meta(conn, "last_update")
        report = db.get_meta(conn, "report")
    st = engine.STATUS
    return {
        "last_update": last, "running": st["running"], "step": st["step"], "error": st["error"],
        "log": st["log"][-40:], "counts": counts, "bankroll": bankroll,
        "start_bankroll": settings["bankroll"], "currency": settings["currency"],
        "profit": profit, "roi": profit / staked if staked else None,
        "models": engine.report_summary(report),
    }


@app.post("/api/update")
def update():
    return {"started": engine.start_update_thread()}


@app.get("/api/signals")
def signals():
    with db.session() as conn:
        settings = db.get_settings(conn)
        bankroll = engine.current_bankroll(conn, settings)
        recs = {r["key"]: r for r in _fixtures(conn)}
        tips = db.rows(conn, "SELECT * FROM tips WHERE active=1 AND kickoff > ? ORDER BY kickoff, id", (_now_iso(),))
        placed = {r["tip_id"]: r["n"] for r in conn.execute(
            "SELECT tip_id, COUNT(*) n FROM bets WHERE tip_id IS NOT NULL GROUP BY tip_id")}
    out = []
    for t in tips:
        rec = recs.get(t["key"])
        if rec is None:
            continue
        t["league"] = config.LEAGUES.get(t["div"], t["div"])
        t["label"] = SEL_LABEL[(t["market"], t["selection"])]
        t["market_name"] = MARKETS[t["market"]]
        t["stake"] = engine.stake_for(t["kelly"], bankroll, settings)
        t["min_odds"] = (1 + settings["min_ev"]) / t["model_prob"]
        t["placed"] = placed.get(t["id"], 0)
        i = SELECTIONS[t["market"]].index(t["selection"])
        t["odds"] = {s: (v[i] if v else None) for s, v in rec["markets"][t["market"]]["odds"].items()}
        out.append(t)
    return {"signals": out, "bankroll": bankroll, "settings": settings}


@app.get("/api/fixtures")
def fixtures():
    with db.session() as conn:
        settings = db.get_settings(conn)
        bankroll = engine.current_bankroll(conn, settings)
        recs = _fixtures(conn)
        tips = {(r["key"], r["market"], r["selection"]): r["id"] for r in db.rows(
            conn, "SELECT id, key, market, selection FROM tips WHERE active=1 AND kickoff > ?", (_now_iso(),))}
    for rec in recs:
        rec["view"] = _selection_views(rec, settings, bankroll)
        for market, v in rec["view"].items():
            for it in v["items"]:
                it["tip_id"] = tips.get((rec["key"], market, it["selection"]))
    return {"fixtures": recs, "settings": settings, "bankroll": bankroll}


class BetIn(BaseModel):
    key: str
    market: Literal["1X2", "OU25"]
    selection: str
    odds: float = Field(gt=1.0, lt=1000)
    stake: float = Field(gt=0)
    bookmaker: Optional[str] = Field(default=None, max_length=60)
    note: Optional[str] = Field(default=None, max_length=300)
    tip_id: Optional[int] = None


class BetPatch(BaseModel):
    odds: Optional[float] = Field(default=None, gt=1.0, lt=1000)
    stake: Optional[float] = Field(default=None, gt=0)
    bookmaker: Optional[str] = Field(default=None, max_length=60)
    note: Optional[str] = Field(default=None, max_length=300)
    status: Optional[Literal["open", "won", "lost", "void"]] = None


@app.get("/api/bets")
def list_bets(status: Literal["open", "settled", "all"] = "all"):
    where = {"open": "WHERE status='open'", "settled": "WHERE status!='open'", "all": ""}[status]
    with db.session() as conn:
        bets = db.rows(conn, f"SELECT * FROM bets {where} ORDER BY kickoff DESC, id DESC")
    for b in bets:
        b["league"] = config.LEAGUES.get(b["div"], b["div"])
        b["label"] = SEL_LABEL.get((b["market"], b["selection"]), b["selection"])
    return {"bets": bets}


@app.post("/api/bets")
def create_bet(bet: BetIn):
    if bet.selection not in SELECTIONS[bet.market]:
        raise HTTPException(400, "Неизвестный исход")
    with db.session() as conn:
        row = conn.execute("SELECT data FROM fixtures WHERE key=?", (bet.key,)).fetchone()
        if row is None:
            raise HTTPException(404, "Матч не найден — обновите данные")
        rec = json.loads(row["data"])
        m = rec["markets"].get(bet.market) or {}
        i = SELECTIONS[bet.market].index(bet.selection)
        p = (m.get("probs") or [None] * 3)[i]
        q = (m.get("fair") or [None] * 3)[i]
        by_tip = 0
        if bet.tip_id is not None:
            tip = conn.execute("SELECT key, market, selection FROM tips WHERE id=?", (bet.tip_id,)).fetchone()
            by_tip = int(tip is not None and tuple(tip) == (bet.key, bet.market, bet.selection))
        cur = conn.execute(
            """INSERT INTO bets(created_at, tip_id, by_tip, key, div, kickoff, home, away, market, selection,
                   odds, stake, model_prob, market_prob, ev, bookmaker, note)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (_now_iso(), bet.tip_id if by_tip else None, by_tip, rec["key"], rec["div"], rec["kickoff"],
             rec["home"], rec["away"], bet.market, bet.selection, bet.odds, bet.stake, p, q,
             p * bet.odds - 1 if p else None, (bet.bookmaker or "").strip() or None, (bet.note or "").strip() or None),
        )
        return {"id": cur.lastrowid}


@app.patch("/api/bets/{bet_id}")
def patch_bet(bet_id: int, patch: BetPatch):
    with db.session() as conn:
        b = conn.execute("SELECT * FROM bets WHERE id=?", (bet_id,)).fetchone()
        if b is None:
            raise HTTPException(404, "Ставка не найдена")
        b = dict(b)
        odds = patch.odds or b["odds"]
        stake = patch.stake or b["stake"]
        fields = {"odds": odds, "stake": stake,
                  "ev": b["model_prob"] * odds - 1 if b["model_prob"] else None,
                  "clv": odds * b["close_prob"] - 1 if b["close_prob"] else None}
        if patch.bookmaker is not None:
            fields["bookmaker"] = patch.bookmaker.strip() or None
        if patch.note is not None:
            fields["note"] = patch.note.strip() or None
        status = patch.status or b["status"]
        if patch.status is not None:
            fields.update(status=status, manual=int(status != "open"),
                          settled_at=_now_iso() if status != "open" else None)
        fields["profit"] = profit_for(status, odds, stake) if status != "open" else None
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE bets SET {sets} WHERE id=?", (*fields.values(), bet_id))
    return {"ok": True}


@app.delete("/api/bets/{bet_id}")
def delete_bet(bet_id: int):
    with db.session() as conn:
        conn.execute("DELETE FROM bets WHERE id=?", (bet_id,))
    return {"ok": True}


@app.get("/api/stats")
def get_stats():
    with db.session() as conn:
        settings = db.get_settings(conn)
        bets = db.rows(conn, "SELECT * FROM bets")
        tips = db.rows(conn, "SELECT * FROM tips WHERE active=1 AND kickoff <= ?", (_now_iso(),))
    paper = [{**t, "odds": t["price"], "stake": 1.0} for t in tips]
    start = float(settings["bankroll"])
    return {
        "currency": settings["currency"],
        "all": stats.summarize(bets, start),
        "by_tip": stats.summarize([b for b in bets if b["by_tip"]], start),
        "own": stats.summarize([b for b in bets if not b["by_tip"]], start),
        "paper": stats.summarize(paper, 0.0),
    }


@app.get("/api/model")
def get_model():
    with db.session() as conn:
        settings = db.get_settings(conn)
        report = db.get_meta(conn, "report")
        has_evals = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='eval_preds'").fetchone()
        evals = pd.read_sql_query("SELECT * FROM eval_preds", conn) if has_evals else pd.DataFrame()
    if report is None:
        return {"report": None}
    champions = {m: r["champion"] for m, r in report["markets"].items()}
    return {
        "report": report,
        "names": model.MODEL_NAMES,
        "summary": engine.report_summary(report),
        "backtest": stats.backtest(evals, settings, champions),
        "grid": stats.backtest_grid(evals, settings, champions),
        "settings": settings,
    }


class SettingsIn(BaseModel):
    bankroll: float = Field(gt=0)
    currency: str = Field(min_length=1, max_length=5)
    kelly_fraction: float = Field(gt=0, le=1)
    max_stake_pct: float = Field(gt=0, le=0.5)
    min_ev: float = Field(ge=-0.5, le=1)
    min_odds: float = Field(gt=1, le=100)
    max_odds: float = Field(gt=1, le=1000)
    price_source: Literal["avg", "max", "pinnacle", "b365"]
    markets: list[Literal["1X2", "OU25"]]
    leagues: list[str]
    auto_update_hours: float = Field(ge=0, le=168)


@app.get("/api/settings")
def get_settings():
    with db.session() as conn:
        settings = db.get_settings(conn)
    return {"settings": settings, "leagues": config.LEAGUES, "price_sources": config.PRICE_SOURCES,
            "markets": MARKETS}


@app.put("/api/settings")
def put_settings(new: SettingsIn):
    values = new.model_dump()
    values["leagues"] = [d for d in values["leagues"] if d in config.LEAGUES]
    if not values["leagues"]:
        raise HTTPException(400, "Выберите хотя бы одну лигу")
    if values["min_odds"] >= values["max_odds"]:
        raise HTTPException(400, "Минимальный кэф должен быть меньше максимального")
    with db.session() as conn:
        old = db.get_settings(conn)
        db.save_settings(conn, values)
        engine.refresh_tips(conn, values, engine.utcnow())
    need_update = set(values["leagues"]) - set(old["leagues"])
    if need_update:
        engine.start_update_thread()
    return {"settings": values, "update_started": bool(need_update)}

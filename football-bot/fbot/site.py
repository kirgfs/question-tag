"""Static build of the site for GitHub Pages: use the bot from a phone, no computer needed.

GitHub Actions runs the usual update and writes plain files:

* ``data/fixtures.json`` — upcoming matches with model probabilities and odds,
* ``data/results.json``  — recent results with closing prices (to settle bets and compute CLV),
* ``data/model.json``    — model quality, backtest and the bot's paper record,

plus the same web UI. In the browser ``static-api.js`` answers the UI's API calls from those
files and keeps the user's bets and settings in the phone's local storage.
"""
from __future__ import annotations

import json
import math
import os
import shutil
from pathlib import Path

import numpy as np
import pandas as pd

from . import config, data, db, engine, model, stats
from .markets import MARKETS, SELECTIONS

STATIC = Path(__file__).parent / "static"
RESULTS_DAYS = 150          # results kept for settling bets
UPDATE_HOURS = 3            # schedule of the GitHub Actions workflow
ASSETS = ("app.js", "style.css", "static-api.js", "icon.svg", "icon-192.png", "icon-512.png", "icon-180.png")


def _clean(obj):
    """JSON-safe copy: NaN/inf become null, numpy scalars become Python numbers."""
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        x = float(obj)
        return x if math.isfinite(x) else None
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if obj is pd.NaT:
        return None
    return obj


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(_clean(payload), ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    path.write_text(text, encoding="utf-8")


def results_payload(hist: pd.DataFrame, now: pd.Timestamp) -> list[dict]:
    """Recent results, oldest first, with de-margined and raw closing prices."""
    since = now.tz_convert("UTC").tz_localize(None).normalize() - pd.Timedelta(days=RESULTS_DAYS)
    recent = hist[hist["date"] >= since].reset_index(drop=True)
    close = {m: model.reference_fair(recent, m, close=True) for m in MARKETS}
    out = []
    for pos, r in enumerate(recent.to_dict("records")):
        out.append({
            "k": r["key"], "d": r["div"], "t": r["date"].strftime("%Y-%m-%d"),
            "h": r["home"], "a": r["away"], "hg": int(r["fthg"]), "ag": int(r["ftag"]),
            "c": {m: [round(float(v), 6) if np.isfinite(v) else None for v in close[m][pos]] for m in MARKETS},
            "o": {m: [engine.closing_odds(r, m, i) for i in range(len(SELECTIONS[m]))] for m in MARKETS},
        })
    return out


def _repo_url() -> str | None:
    repo = os.environ.get("GITHUB_REPOSITORY")
    return f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{repo}" if repo else None


def index_html() -> str:
    """The server's index.html with relative asset paths and the static data layer."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    replacements = [
        ('href="/static/style.css"', 'href="style.css"'),
        ('<script src="/static/app.js"></script>',
         '<script src="static-api.js"></script>\n  <script src="app.js"></script>'),
        ("</head>",
         '  <link rel="manifest" href="manifest.webmanifest">\n'
         '  <link rel="apple-touch-icon" href="icon-180.png">\n'
         '  <meta name="apple-mobile-web-app-capable" content="yes">\n'
         '  <meta name="mobile-web-app-capable" content="yes">\n</head>'),
    ]
    for old, new in replacements:
        if old not in html:
            raise RuntimeError(f"index.html template changed, cannot find {old!r}")
        html = html.replace(old, new, 1)
    return html


MANIFEST = {
    "name": "Value Bot · футбол",
    "short_name": "Value Bot",
    "start_url": "./",
    "scope": "./",
    "display": "standalone",
    "background_color": "#f4f5f7",
    "theme_color": "#1f7a4d",
    "icons": [
        {"src": "icon-192.png", "sizes": "192x192", "type": "image/png"},
        {"src": "icon-512.png", "sizes": "512x512", "type": "image/png"},
        {"src": "icon.svg", "sizes": "any", "type": "image/svg+xml"},
    ],
}


def backtest_grid(evals: pd.DataFrame, settings: dict, champions: dict) -> list[dict]:
    """Backtest of the default rules for every price source and a few EV thresholds."""
    rows = []
    for src in ("avg", "max", "pinnacle", "b365"):
        for min_ev in (0.0, 0.03, 0.05, 0.10):
            bt = stats.backtest(evals, {**settings, "price_source": src, "min_ev": min_ev}, champions)
            if bt.get("available"):
                rows.append({"price_source": src, "min_ev": min_ev, **bt["summary"],
                             "by_market": bt["by_market"]})
    return rows


def summary_markdown(report: dict, backtest: dict, n_fixtures: int, n_signals: int,
                     grid: list[dict] | None = None) -> str:
    """Short run report for the console and the GitHub Actions job summary."""
    lines = ["## Value Bot", "", f"Матчей впереди: **{n_fixtures}**, сигналов (настройки по умолчанию): **{n_signals}**", ""]
    lines += ["| Рынок | Модель | Log-loss | Рынок | Δ ± SE | Матчей |", "|---|---|---|---|---|---|"]
    for market, r in report["markets"].items():
        mets = {m["model"]: m for m in r["metrics"]}
        champ, base = mets.get(r["champion"]), mets.get("market")
        if not champ or not base:
            continue
        delta = "—" if r["champion"] == "market" else f"{champ['delta']:+.4f} ± {champ['delta_se']:.4f}"
        lines.append(f"| {MARKETS[market]} | {model.MODEL_NAMES[r['champion']]} | {champ['logloss']:.4f} "
                     f"| {base['logloss']:.4f} | {delta} | {champ['n']} |")
    if backtest.get("available"):
        b = backtest["summary"]
        roi = "—" if b["roi"] is None else f"{b['roi']:+.1%} ± {(b['roi_se'] or 0):.1%}"
        clv = "—" if b["avg_clv"] is None else f"{b['avg_clv']:+.1%}"
        lines += ["", f"Бэктест вне выборки: ставок {b['n']}, ROI {roi}, прибыль {b['profit']:+.1f} ед., CLV {clv}"]
    if grid:
        fmt = lambda v, f: "—" if v is None else format(v, f)  # noqa: E731
        lines += ["", "| Кэф | EV ≥ | Ставок | ROI ± SE | CLV | 1X2: ставок / ROI / CLV | Тотал: ставок / ROI / CLV |",
                  "|---|---|---|---|---|---|---|"]
        for g in grid:
            per = {m["name"]: m for m in g["by_market"]}
            cells = [f"{per[m]['n']} / {fmt(per[m]['roi'], '+.1%')} / {fmt(per[m]['avg_clv'], '+.1%')}" if m in per else "—"
                     for m in ("1X2", "OU25")]
            lines.append(f"| {g['price_source']} | {g['min_ev']:.0%} | {g['n']} | {fmt(g['roi'], '+.1%')} ± {fmt(g['roi_se'], '.1%')} "
                         f"| {fmt(g['avg_clv'], '+.1%')} | {cells[0]} | {cells[1]} |")
    return "\n".join(lines) + "\n"


def build(out: Path, leagues: list[str] | None = None, update: bool = True) -> None:
    """Refresh the data (unless ``update`` is False) and write the static site to ``out``."""
    db.init()
    if leagues:
        with db.session() as conn:
            db.save_settings(conn, {"leagues": leagues})
    if update and not engine.run_update():
        raise RuntimeError(engine.STATUS["error"] or "update failed")

    now = engine.utcnow()
    stamp = now.isoformat()
    with db.session() as conn:
        settings = db.get_settings(conn)
        report = db.get_meta(conn, "report")
        last_update = db.get_meta(conn, "last_update")
        records = [json.loads(r["data"]) for r in conn.execute(
            "SELECT data FROM fixtures WHERE kickoff > ? ORDER BY kickoff", (stamp,))]
        tips = db.rows(conn, "SELECT * FROM tips WHERE active=1 AND kickoff <= ?", (stamp,))
        has_evals = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='eval_preds'").fetchone()
        evals = pd.read_sql_query("SELECT * FROM eval_preds", conn) if has_evals else pd.DataFrame()
    if report is None:
        raise RuntimeError("Нет обученной модели — обновление ещё не выполнялось")

    leagues_map = {d: config.LEAGUES[d] for d in settings["leagues"] if d in config.LEAGUES}
    defaults = {k: v for k, v in settings.items() if k != "auto_update_hours"}
    hist = data.load_history(list(leagues_map), now.date(), log=lambda _m: None)
    champions = {m: r["champion"] for m, r in report["markets"].items()}

    out = Path(out)
    if out.exists():
        shutil.rmtree(out)
    (out / "data").mkdir(parents=True)
    _write_json(out / "data" / "fixtures.json", {
        "generated_at": stamp,
        "last_update": last_update,
        "update_hours": UPDATE_HOURS,
        "repo_url": _repo_url(),
        "leagues": leagues_map,
        "defaults": defaults,
        "models": engine.report_summary(report),
        "fixtures": records,
    })
    _write_json(out / "data" / "results.json", {
        "generated_at": stamp,
        "results": results_payload(hist, now) if not hist.empty else [],
    })
    backtest = stats.backtest(evals, settings, champions)
    _write_json(out / "data" / "model.json", {
        "report": report,
        "names": model.MODEL_NAMES,
        "summary": engine.report_summary(report),
        "backtest": backtest,
        "backtest_settings": settings,
        "paper": stats.summarize([{**t, "odds": t["price"], "stake": 1.0} for t in tips], 0.0),
    })

    (out / "index.html").write_text(index_html(), encoding="utf-8")
    for name in ASSETS:
        shutil.copyfile(STATIC / name, out / name)
    _write_json(out / "manifest.webmanifest", MANIFEST)
    (out / ".nojekyll").write_text("", encoding="utf-8")

    n_signals = sum(
        1 for rec in records if rec["div"] in settings["leagues"]
        for m in settings["markets"] if engine.evaluate_fixture(rec, m, settings)
    )
    text = summary_markdown(report, backtest, len(records), n_signals, backtest_grid(evals, settings, champions))
    print(text, flush=True)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(text)

"""Static build for GitHub Pages: files, and parity of static-api.js with the Python server.

The browser tests need Playwright with Chromium; they are skipped when it is not installed.
"""
import functools
import glob
import json
import threading
from datetime import date
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from fbot import data, db, engine, server, site


@pytest.fixture(scope="module")
def built(env, tmp_path_factory):
    out = tmp_path_factory.mktemp("site") / "site"
    site.build(out, update=False)
    return out


def load(out, name):
    return json.loads((out / "data" / name).read_text(encoding="utf-8"))


def test_build_writes_site(built):
    for name in ("index.html", "app.js", "static-api.js", "style.css", "manifest.webmanifest",
                 "icon-180.png", "icon-512.png", ".nojekyll"):
        assert (built / name).exists(), name
    html = (built / "index.html").read_text(encoding="utf-8")
    assert "/static/" not in html
    assert html.index("static-api.js") < html.index('src="app.js"')
    fx = load(built, "fixtures.json")
    assert fx["fixtures"] and set(fx["leagues"]) == {"E0", "D1"}
    assert fx["defaults"]["price_source"] == "max" and "auto_update_hours" not in fx["defaults"]
    res = load(built, "results.json")["results"]
    assert res and {"k", "d", "t", "h", "a", "hg", "ag", "c", "o"} <= set(res[0])
    assert any(r["c"]["1X2"][0] is not None for r in res)
    assert [r["t"] for r in res] == sorted(r["t"] for r in res)
    m = load(built, "model.json")
    assert m["backtest"]["available"] and "paper" in m and m["report"]["markets"]


def test_index_template_guard():
    html = site.index_html()
    assert 'href="style.css"' in html and "manifest.webmanifest" in html


# ------------------------------------------------------------------ browser ---

def _launch(p):
    try:
        return p.chromium.launch()
    except Exception:
        for path in sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome")):
            return p.chromium.launch(executable_path=path)
        raise


@pytest.fixture(scope="module")
def page(built):
    sync_api = pytest.importorskip("playwright.sync_api")
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(built))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    with sync_api.sync_playwright() as p:
        try:
            browser = _launch(p)
        except Exception as e:  # pragma: no cover - depends on the machine
            pytest.skip(f"no Chromium: {e}")
        pg = browser.new_page(viewport={"width": 390, "height": 844})
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(f"http://127.0.0.1:{httpd.server_address[1]}/#signals")
        pg.wait_for_function("document.querySelector('#view').children.length > 0")
        pg.errors = errors
        yield pg
        browser.close()
    httpd.shutdown()


def call(pg, path, method="GET", body=None):
    opts = {"method": method}
    if body is not None:
        opts["body"] = json.dumps(body)
    return pg.evaluate("([p, o]) => FBOT_STATIC_API(p, o)", [path, opts])


def test_signals_match_server(env, page):
    py = TestClient(server.app).get("/api/signals").json()["signals"]
    js = call(page, "/api/signals")["signals"]
    assert py, "expected signals in the synthetic data"
    pyd = {(t["key"], t["market"], t["selection"]): t for t in py}
    jsd = {(t["key"], t["market"], t["selection"]): t for t in js}
    assert pyd.keys() == jsd.keys()
    for k, a in pyd.items():
        b = jsd[k]
        for f in ("model_prob", "price", "ev", "min_odds", "stake", "kelly"):
            assert b[f] == pytest.approx(a[f]), (k, f)
        assert b["price_source"] == a["price_source"] and b["label"] == a["label"]


def test_fixture_views_match_server(env, page):
    py = {f["key"]: f for f in TestClient(server.app).get("/api/fixtures").json()["fixtures"]}
    js = {f["key"]: f for f in call(page, "/api/fixtures")["fixtures"]}
    assert py.keys() == js.keys()
    for key, f in py.items():
        for market, v in f["view"].items():
            for a, b in zip(v["items"], js[key]["view"][market]["items"]):
                for fld in ("prob", "price", "ev", "min_odds", "stake", "market_prob"):
                    assert b[fld] == pytest.approx(a[fld]), (key, market, fld)
                assert bool(a["tip_id"]) == bool(b["tip_id"])


def test_settlement_and_stats_match_server(env, page):
    hist = data.load_history(["E0", "D1"], date.today(), lambda _m: None)
    played = hist.iloc[-14:]
    specs = [("1X2", "H"), ("1X2", "D"), ("1X2", "A"), ("OU25", "O"), ("OU25", "U")]
    bets = []
    for n, r in enumerate(played.to_dict("records")):
        market, sel = specs[n % len(specs)]
        bets.append({
            "id": n + 1, "key": r["key"], "div": r["div"], "kickoff": r["kickoff"].isoformat(),
            "home": r["home"], "away": r["away"], "market": market, "selection": sel,
            "odds": round(1.6 + 0.17 * n, 2), "stake": 100 + 10 * n, "by_tip": n % 3 != 0,
            "model_prob": 0.4, "ev": 0.4 * round(1.6 + 0.17 * n, 2) - 1,
        })
    with db.session() as conn:
        conn.execute("DELETE FROM bets")
        for b in bets:
            conn.execute(
                "INSERT INTO bets(id, created_at, key, div, kickoff, home, away, market, selection, odds, stake,"
                " by_tip, model_prob, ev) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (b["id"], "t", b["key"], b["div"], b["kickoff"], b["home"], b["away"], b["market"],
                 b["selection"], b["odds"], b["stake"], int(b["by_tip"]), b["model_prob"], b["ev"]),
            )
        engine.settle_open(conn, hist, engine.utcnow())
    client = TestClient(server.app)
    client.patch("/api/bets/2", json={"status": "void"})

    js_bets = [{**b, "by_tip": int(b["by_tip"]), "status": "open", "close_prob": None, "tip_id": None} for b in bets]
    page.evaluate("b => localStorage.setItem('fbot.bets.v1', JSON.stringify(b))", js_bets)
    call(page, "/api/update", "POST")          # reload data -> settles
    call(page, "/api/bets/2", "PATCH", {"status": "void"})

    py_bets = {b["id"]: b for b in client.get("/api/bets").json()["bets"]}
    for b in call(page, "/api/bets")["bets"]:
        a = py_bets[b["id"]]
        assert b["status"] == a["status"], b["id"]
        assert (b["fthg"], b["ftag"]) == (a["fthg"], a["ftag"])
        for f in ("profit", "clv", "close_prob", "close_odds"):
            # closing probabilities are published with 6 decimals
            tol = 1e-5 if f in ("clv", "close_prob") else 1e-9
            assert (b[f] is None) == (a[f] is None) and (a[f] is None or b[f] == pytest.approx(a[f], abs=tol)), (b["id"], f)

    py_stats, js_stats = client.get("/api/stats").json(), call(page, "/api/stats")
    for scope in ("all", "by_tip", "own", "paper"):
        a, b = py_stats[scope], js_stats[scope]
        for f in ("n", "won", "lost", "void", "open", "total", "staked", "profit", "roi", "roi_se", "hit_rate",
                  "avg_odds", "avg_ev", "expected_profit", "avg_clv", "beat_close", "max_drawdown", "max_drawdown_pct"):
            tol = 1e-5 if f == "avg_clv" else 1e-9
            assert (a[f] is None) == (b[f] is None) and (a[f] is None or b[f] == pytest.approx(a[f], abs=tol)), (scope, f)
        assert [p["v"] for p in a["curve"]] == pytest.approx([p["v"] for p in b["curve"]])
        for g in ("by_league", "by_selection", "by_odds"):
            assert [(x["name"], x["n"]) for x in a[g]] == [(x["name"], x["n"]) for x in b[g]], (scope, g)
    assert call(page, "/api/status")["bankroll"] == pytest.approx(client.get("/api/status").json()["bankroll"])
    with db.session() as conn:
        conn.execute("DELETE FROM bets")
    page.evaluate("localStorage.removeItem('fbot.bets.v1')")


def test_ui_flow_on_phone(page):
    page.goto(page.url.split("#")[0] + "#signals")
    page.wait_for_selector("[data-sig]")
    page.click("[data-sig='0']")
    page.fill("#dlg-odds", "2.5")
    page.fill("#dlg-stake", "150")
    page.click("#dlg-save")
    page.wait_for_function("JSON.parse(localStorage.getItem('fbot.bets.v1') || '[]').length === 1")
    bet = page.evaluate("JSON.parse(localStorage.getItem('fbot.bets.v1'))[0]")
    assert bet["odds"] == 2.5 and bet["stake"] == 150 and bet["by_tip"] == 1 and bet["status"] == "open"

    page.goto(page.url.split("#")[0] + "#bets")
    page.wait_for_selector("table.rt")
    assert page.evaluate("document.documentElement.scrollWidth") <= 390

    backup = call(page, "/api/backup")
    assert backup["app"] == "fbot" and len(backup["bets"]) == 1
    call(page, "/api/backup", "POST", {"app": "fbot", "version": 1, "bets": []})
    assert call(page, "/api/bets")["bets"] == []
    call(page, "/api/backup", "POST", backup)
    assert len(call(page, "/api/bets")["bets"]) == 1
    with pytest.raises(Exception):
        call(page, "/api/backup", "POST", {"bets": "nope"})

    settings = call(page, "/api/settings")["settings"]
    with pytest.raises(Exception):
        call(page, "/api/settings", "PUT", {**settings, "min_odds": 5, "max_odds": 2})
    call(page, "/api/settings", "PUT", {**settings, "min_ev": 0.9})
    assert call(page, "/api/signals")["signals"] == []
    call(page, "/api/settings", "PUT", settings)

    for tab in ("fixtures", "stats", "model", "settings"):
        page.goto(page.url.split("#")[0] + "#" + tab)
        page.wait_for_function("document.querySelector('#view').children.length > 0")
        assert page.evaluate("document.documentElement.scrollWidth") <= 390, tab
    assert not page.errors, page.errors

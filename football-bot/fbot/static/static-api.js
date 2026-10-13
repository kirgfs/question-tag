"use strict";
// Static mode (GitHub Pages): answers the UI's /api/* calls in the browser.
// Data comes from data/*.json published by GitHub Actions; the user's bets and
// settings live in this browser's localStorage. The logic mirrors fbot/engine.py,
// fbot/server.py and fbot/stats.py — keep them in sync (tests/test_static.py checks it).
(function () {
  const KEYS = { bets: "fbot.bets.v1", settings: "fbot.settings.v1", seq: "fbot.seq.v1" };
  const RELOAD_MS = 15 * 60 * 1000;
  const SELECTIONS = { "1X2": ["H", "D", "A"], OU25: ["O", "U"] };
  const MARKETS = { "1X2": "Исход матча", OU25: "Тотал 2.5" };
  const SEL_LABEL = { "1X2|H": "П1", "1X2|D": "Х", "1X2|A": "П2", "OU25|O": "ТБ 2.5", "OU25|U": "ТМ 2.5" };
  const PRICE_SOURCES = { avg: "Средний по рынку", max: "Лучший по рынку", pinnacle: "Pinnacle", b365: "Bet365" };
  const ODDS_BUCKETS = [[1.0, 1.7], [1.7, 2.2], [2.2, 3.0], [3.0, 5.0], [5.0, 1000.0]];
  const DAY = 86400000;

  let site = null, results = null, model = null, loadedAt = 0, resultIndex = new Map();

  class ApiError extends Error {}
  const fail = (msg) => { throw new ApiError(msg); };

  // ---------------------------------------------------------------- storage ---
  function readLS(key, fallback) {
    try {
      const raw = localStorage.getItem(key);
      return raw ? JSON.parse(raw) : fallback;
    } catch {
      return fallback;
    }
  }
  function writeLS(key, value) {
    try {
      localStorage.setItem(key, JSON.stringify(value));
    } catch (e) {
      fail("Браузер не дал сохранить данные: " + e.message);
    }
  }
  const getBets = () => readLS(KEYS.bets, []);
  const saveBets = (bets) => writeLS(KEYS.bets, bets);
  function nextId(bets) {
    const seq = Math.max(readLS(KEYS.seq, 0), ...bets.map((b) => b.id || 0)) + 1;
    writeLS(KEYS.seq, seq);
    return seq;
  }

  // ------------------------------------------------------------------- data ---
  async function getJSON(path) {
    const res = await fetch(`${path}?t=${Date.now()}`, { cache: "no-store" });
    if (!res.ok) fail(`Не удалось загрузить ${path}: HTTP ${res.status}`);
    return res.json();
  }

  async function load(force) {
    if (site && !force && Date.now() - loadedAt < RELOAD_MS) return;
    const [s, r] = await Promise.all([getJSON("data/fixtures.json"), getJSON("data/results.json")]);
    site = s;
    results = r;
    model = null;
    loadedAt = Date.now();
    resultIndex = new Map();
    for (const x of results.results) {
      const k = `${x.d}|${x.h}|${x.a}`;
      if (!resultIndex.has(k)) resultIndex.set(k, []);
      resultIndex.get(k).push(x);
    }
    settleAll();
  }

  async function loadModel() {
    if (!model) model = await getJSON("data/model.json");
    return model;
  }

  function settings() {
    const s = { ...site.defaults, ...readLS(KEYS.settings, {}) };
    s.leagues = (s.leagues || []).filter((d) => d in site.leagues);
    if (!s.leagues.length) s.leagues = Object.keys(site.leagues);
    s.markets = (s.markets || []).filter((m) => m in MARKETS);
    return s;
  }

  const nowIso = () => new Date().toISOString();
  const upcoming = () => site.fixtures.filter((f) => Date.parse(f.kickoff) > Date.now());

  // ----------------------------------------------------------- money logic ---
  // engine.pick_price
  function pickPrice(odds, source, i) {
    for (const src of [source, "avg"]) {
      const vals = (odds || {})[src] || [];
      if (i < vals.length && vals[i] && vals[i] > 1) return [vals[i], src];
    }
    return [null, null];
  }
  // markets.kelly_fraction
  const kellyFraction = (p, odds) => (odds <= 1 ? 0 : Math.max(0, (p * odds - 1) / (odds - 1)));
  // engine.stake_for
  function stakeFor(kelly, bankroll, s) {
    const share = Math.min(s.kelly_fraction * kelly, s.max_stake_pct);
    const stake = Math.max(0, bankroll * share);
    const step = stake >= 100 ? 10 : 1;
    return Math.round(stake / step) * step;
  }
  // markets.profit_for
  function profitFor(status, odds, stake) {
    if (status === "won") return Math.round(stake * (odds - 1) * 100) / 100;
    if (status === "lost") return -Math.round(stake * 100) / 100;
    return 0;
  }
  function settle(market, selection, hg, ag) {
    const idx = SELECTIONS[market].indexOf(selection);
    const out = market === "1X2" ? (hg > ag ? 0 : hg === ag ? 1 : 2) : hg + ag > 2.5 ? 0 : 1;
    return out === idx ? "won" : "lost";
  }
  function bankroll(s, bets) {
    return Number(s.bankroll) + bets
      .filter((b) => ["won", "lost", "void"].includes(b.status))
      .reduce((a, b) => a + (b.profit || 0), 0);
  }

  // engine.evaluate_fixture
  function evaluateFixture(rec, market, s) {
    const m = rec.markets[market];
    if (!m || !m.probs) return null;
    let best = null;
    SELECTIONS[market].forEach((sel, i) => {
      const [price, src] = pickPrice(m.odds, s.price_source, i);
      if (price === null) return;
      const p = m.probs[i];
      const ev = p * price - 1;
      if (ev < s.min_ev || !(s.min_odds <= price && price <= s.max_odds)) return;
      if (best === null || ev > best.ev) {
        best = {
          market, selection: sel, model_prob: p, market_prob: (m.fair || [])[i] ?? null,
          price, price_source: src, ev, kelly: kellyFraction(p, price),
        };
      }
    });
    return best;
  }
  const tipId = (key, market, sel) => `${key}|${market}|${sel}`;

  function currentTips(s) {
    const tips = [];
    for (const rec of upcoming()) {
      if (!s.leagues.includes(rec.div)) continue;
      for (const market of s.markets) {
        const t = evaluateFixture(rec, market, s);
        if (t) tips.push({ ...t, rec, id: tipId(rec.key, market, t.selection) });
      }
    }
    return tips;
  }

  // server._selection_views
  function selectionViews(rec, s, bank) {
    const views = {};
    for (const market of Object.keys(MARKETS)) {
      const m = rec.markets[market] || {};
      const items = SELECTIONS[market].map((sel, i) => {
        const p = (m.probs || [])[i] ?? null;
        const [price, src] = pickPrice(m.odds, s.price_source, i);
        const ev = p && price ? p * price - 1 : null;
        const kelly = p && price ? kellyFraction(p, price) : 0;
        const odds = {};
        for (const [k, v] of Object.entries(m.odds || {})) odds[k] = v && i < v.length ? v[i] : null;
        return {
          selection: sel, label: SEL_LABEL[`${market}|${sel}`], prob: p,
          dc_prob: (m.dc_probs || [])[i] ?? null, market_prob: (m.fair || [])[i] ?? null,
          price, price_source: src, ev,
          min_odds: p ? (1 + s.min_ev) / p : null, fair_odds: p ? 1 / p : null,
          stake: ev && ev > 0 ? stakeFor(kelly, bank, s) : 0, odds,
        };
      });
      views[market] = { model: m.model, items };
    }
    return views;
  }

  // -------------------------------------------------------------- settling ---
  // engine.find_result: same teams, played from a day before to 60 days after kick-off
  function findResult(div, home, away, kickoff) {
    const ko = Date.parse(new Date(kickoff).toISOString().slice(0, 10));
    for (const r of resultIndex.get(`${div}|${home}|${away}`) || []) {
      const t = Date.parse(r.t);
      if (ko - DAY <= t && t <= ko + 60 * DAY) return r;
    }
    return null;
  }

  // engine.settle_open (user's bets)
  function settleAll() {
    const bets = getBets();
    let changed = false;
    const now = Date.now();
    for (const b of bets) {
      if (!(b.status === "open" || b.close_prob == null) || Date.parse(b.kickoff) >= now) continue;
      const r = findResult(b.div, b.home, b.away, b.kickoff);
      if (!r) continue;
      const i = SELECTIONS[b.market].indexOf(b.selection);
      const closeP = (r.c[b.market] || [])[i] ?? null;
      const closeOdds = (r.o[b.market] || [])[i] ?? null;
      b.close_prob = closeP;
      b.close_odds = closeOdds;
      b.clv = closeP ? b.odds * closeP - 1 : null;
      if (b.status === "open") {
        b.status = settle(b.market, b.selection, r.hg, r.ag);
        b.fthg = r.hg;
        b.ftag = r.ag;
        b.profit = profitFor(b.status, b.odds, b.stake);
        b.settled_at = nowIso();
      } else {
        if (b.fthg == null) b.fthg = r.hg;
        if (b.ftag == null) b.ftag = r.ag;
      }
      changed = true;
    }
    if (changed) saveBets(bets);
  }

  // ------------------------------------------------------------------ stats ---
  const sum = (xs) => xs.reduce((a, x) => a + x, 0);
  const mean = (xs) => sum(xs) / xs.length;

  function basic(items) {
    const settled = items.filter((b) => b.status === "won" || b.status === "lost");
    const staked = sum(settled.map((b) => b.stake));
    const profit = sum(settled.map((b) => b.profit || 0));
    const won = settled.filter((b) => b.status === "won").length;
    return {
      n: settled.length, won, staked, profit,
      roi: staked ? profit / staked : null,
      hit_rate: settled.length ? won / settled.length : null,
    };
  }

  function group(items, keyFn) {
    const groups = new Map();
    for (const it of items) {
      const k = keyFn(it);
      if (!groups.has(k)) groups.set(k, []);
      groups.get(k).push(it);
    }
    return [...groups.entries()].map(([name, g]) => ({ ...basic(g), name })).sort((a, b) => b.n - a.n);
  }

  function oddsBucket(b) {
    for (const [lo, hi] of ODDS_BUCKETS) {
      if (lo <= b.odds && b.odds < hi) return hi < 100 ? `${lo.toFixed(1)}–${hi.toFixed(1)}` : `${lo.toFixed(1)}+`;
    }
    return "?";
  }

  // stats.summarize
  function summarize(items, startBankroll) {
    const settled = items.filter((b) => ["won", "lost", "void"].includes(b.status));
    const decided = settled.filter((b) => b.status !== "void");
    const open = items.filter((b) => b.status === "open");
    const s = basic(items);
    const withClv = decided.filter((b) => b.clv != null);
    const withEv = decided.filter((b) => b.ev != null);
    const returns = decided.filter((b) => b.stake).map((b) => (b.profit || 0) / b.stake);
    const m = returns.length ? mean(returns) : 0;
    const std = returns.length ? Math.sqrt(mean(returns.map((r) => (r - m) ** 2))) : 0;
    Object.assign(s, {
      roi_se: returns.length > 1 ? std / Math.sqrt(returns.length) : null,
      total: items.length,
      open: open.length,
      open_stake: sum(open.map((b) => b.stake)),
      void: settled.filter((b) => b.status === "void").length,
      lost: settled.filter((b) => b.status === "lost").length,
      avg_odds: decided.length ? mean(decided.map((b) => b.odds)) : null,
      avg_ev: withEv.length ? mean(withEv.map((b) => b.ev)) : null,
      expected_profit: withEv.length ? sum(withEv.map((b) => b.ev * b.stake)) : null,
      avg_clv: withClv.length ? mean(withClv.map((b) => b.clv)) : null,
      beat_close: withClv.length ? mean(withClv.map((b) => (b.clv > 0 ? 1 : 0))) : null,
      n_clv: withClv.length,
    });
    const ordered = [...settled].sort((a, b) =>
      (a.kickoff || "") < (b.kickoff || "") ? -1 : (a.kickoff || "") > (b.kickoff || "") ? 1 : (a.id || 0) - (b.id || 0));
    let level = startBankroll || 0, peak = level, maxDd = 0;
    const curve = [{ t: null, v: level }];
    for (const b of ordered) {
      level += b.profit || 0;
      peak = Math.max(peak, level);
      maxDd = Math.max(maxDd, peak - level);
      curve.push({ t: b.kickoff, v: Math.round(level * 100) / 100 });
    }
    s.curve = curve;
    s.max_drawdown = maxDd;
    s.max_drawdown_pct = peak > 0 ? maxDd / peak : null;
    s.by_league = group(decided, (b) => site.leagues[b.div] || b.div);
    s.by_selection = group(decided, (b) => SEL_LABEL[`${b.market}|${b.selection}`] || b.selection);
    s.by_odds = group(decided, oddsBucket);
    return s;
  }

  // ----------------------------------------------------------------- routes ---
  function status() {
    const s = settings();
    const bets = getBets();
    const open = bets.filter((b) => b.status === "open");
    const settledBets = bets.filter((b) => ["won", "lost", "void"].includes(b.status));
    const profit = sum(settledBets.map((b) => b.profit || 0));
    const staked = sum(bets.filter((b) => b.status === "won" || b.status === "lost").map((b) => b.stake));
    const ageH = site.last_update ? (Date.now() - Date.parse(site.last_update)) / 3600000 : null;
    return {
      static: true,
      last_update: site.last_update, running: false, step: "", error: null, log: [],
      stale_hours: ageH !== null && ageH > 3 * site.update_hours + 3 ? ageH : null,
      repo_url: site.repo_url,
      counts: {
        fixtures: upcoming().length, signals: currentTips(s).length,
        open_bets: open.length, open_stake: sum(open.map((b) => b.stake)),
      },
      bankroll: bankroll(s, bets), start_bankroll: s.bankroll, currency: s.currency,
      profit, roi: staked ? profit / staked : null,
      models: site.models,
    };
  }

  function signals() {
    const s = settings();
    const bets = getBets();
    const bank = bankroll(s, bets);
    const out = currentTips(s).map((t) => ({
      id: t.id, key: t.rec.key, div: t.rec.div, kickoff: t.rec.kickoff, home: t.rec.home, away: t.rec.away,
      market: t.market, selection: t.selection, model_prob: t.model_prob, market_prob: t.market_prob,
      price: t.price, price_source: t.price_source, ev: t.ev, kelly: t.kelly,
      league: site.leagues[t.rec.div] || t.rec.div,
      label: SEL_LABEL[`${t.market}|${t.selection}`], market_name: MARKETS[t.market],
      stake: stakeFor(t.kelly, bank, s), min_odds: (1 + s.min_ev) / t.model_prob,
      placed: bets.filter((b) => b.tip_id === t.id).length,
      odds: Object.fromEntries(Object.entries(t.rec.markets[t.market].odds)
        .map(([k, v]) => [k, v ? v[SELECTIONS[t.market].indexOf(t.selection)] : null])),
    }));
    return { signals: out, bankroll: bank, settings: s };
  }

  function fixtures() {
    const s = settings();
    const bank = bankroll(s, getBets());
    const tipIds = new Set(currentTips(s).map((t) => t.id));
    const recs = upcoming().filter((r) => s.leagues.includes(r.div)).map((rec) => {
      const view = selectionViews(rec, s, bank);
      for (const [market, v] of Object.entries(view)) {
        for (const it of v.items) {
          const id = tipId(rec.key, market, it.selection);
          it.tip_id = tipIds.has(id) ? id : null;
        }
      }
      return { ...rec, view };
    });
    return { fixtures: recs, settings: s, bankroll: bank };
  }

  function listBets(statusFilter) {
    let bets = getBets();
    if (statusFilter === "open") bets = bets.filter((b) => b.status === "open");
    if (statusFilter === "settled") bets = bets.filter((b) => b.status !== "open");
    bets.sort((a, b) => (a.kickoff < b.kickoff ? 1 : a.kickoff > b.kickoff ? -1 : (b.id || 0) - (a.id || 0)));
    return {
      bets: bets.map((b) => ({
        ...b, league: site.leagues[b.div] || b.div, label: SEL_LABEL[`${b.market}|${b.selection}`] || b.selection,
      })),
    };
  }

  function createBet(body) {
    const market = body.market;
    if (!(market in SELECTIONS)) fail("Неизвестный рынок");
    if (!SELECTIONS[market].includes(body.selection)) fail("Неизвестный исход");
    const odds = Number(body.odds), stake = Number(body.stake);
    if (!(odds > 1 && odds < 1000)) fail("Кэф должен быть больше 1");
    if (!(stake > 0)) fail("Сумма должна быть больше 0");
    const rec = site.fixtures.find((f) => f.key === body.key);
    if (!rec) fail("Матч не найден — обновите данные");
    const m = rec.markets[market] || {};
    const i = SELECTIONS[market].indexOf(body.selection);
    const p = (m.probs || [])[i] ?? null;
    const byTip = body.tip_id != null && body.tip_id === tipId(rec.key, market, body.selection) ? 1 : 0;
    const bets = getBets();
    const bet = {
      id: nextId(bets), created_at: nowIso(), tip_id: byTip ? body.tip_id : null, by_tip: byTip,
      key: rec.key, div: rec.div, kickoff: rec.kickoff, home: rec.home, away: rec.away,
      market, selection: body.selection, odds, stake,
      model_prob: p, market_prob: (m.fair || [])[i] ?? null, ev: p ? p * odds - 1 : null,
      bookmaker: (body.bookmaker || "").trim() || null, note: (body.note || "").trim() || null,
      status: "open", fthg: null, ftag: null, profit: null, settled_at: null, manual: 0,
      close_odds: null, close_prob: null, clv: null,
    };
    bets.push(bet);
    saveBets(bets);
    settleAll();
    return { id: bet.id };
  }

  // server.patch_bet
  function patchBet(id, patch) {
    const bets = getBets();
    const b = bets.find((x) => x.id === id);
    if (!b) fail("Ставка не найдена");
    if (patch.odds != null && !(patch.odds > 1 && patch.odds < 1000)) fail("Кэф должен быть больше 1");
    if (patch.stake != null && !(patch.stake > 0)) fail("Сумма должна быть больше 0");
    if (patch.status != null && !["open", "won", "lost", "void"].includes(patch.status)) fail("Неизвестный статус");
    b.odds = patch.odds || b.odds;
    b.stake = patch.stake || b.stake;
    b.ev = b.model_prob ? b.model_prob * b.odds - 1 : null;
    b.clv = b.close_prob ? b.odds * b.close_prob - 1 : null;
    if (patch.bookmaker != null) b.bookmaker = patch.bookmaker.trim() || null;
    if (patch.note != null) b.note = patch.note.trim() || null;
    if (patch.status != null) {
      b.status = patch.status;
      b.manual = patch.status !== "open" ? 1 : 0;
      b.settled_at = patch.status !== "open" ? nowIso() : null;
    }
    b.profit = b.status !== "open" ? profitFor(b.status, b.odds, b.stake) : null;
    saveBets(bets);
    return { ok: true };
  }

  function deleteBet(id) {
    saveBets(getBets().filter((b) => b.id !== id));
    return { ok: true };
  }

  async function getStats() {
    const s = settings();
    const bets = getBets();
    const m = await loadModel();
    return {
      currency: s.currency,
      all: summarize(bets, Number(s.bankroll)),
      by_tip: summarize(bets.filter((b) => b.by_tip), Number(s.bankroll)),
      own: summarize(bets.filter((b) => !b.by_tip), Number(s.bankroll)),
      paper: m.paper,
    };
  }

  async function getModel() {
    const m = await loadModel();
    return { ...m, settings: m.backtest_settings, static: true };
  }

  function getSettings() {
    return {
      settings: settings(), leagues: site.leagues, price_sources: PRICE_SOURCES, markets: MARKETS, static: true,
      update_hours: site.update_hours, repo_url: site.repo_url,
    };
  }

  // server.SettingsIn + put_settings
  function putSettings(v) {
    const num = (x) => typeof x === "number" && isFinite(x);
    const checks = [
      [num(v.bankroll) && v.bankroll > 0, "Банк должен быть больше 0"],
      [typeof v.currency === "string" && v.currency.length >= 1 && v.currency.length <= 5, "Валюта: 1–5 символов"],
      [num(v.kelly_fraction) && v.kelly_fraction > 0 && v.kelly_fraction <= 1, "Доля Келли: от 0 до 1"],
      [num(v.max_stake_pct) && v.max_stake_pct > 0 && v.max_stake_pct <= 0.5, "Макс. ставка: до 50% банка"],
      [num(v.min_ev) && v.min_ev >= -0.5 && v.min_ev <= 1, "Мин. EV: от −50% до 100%"],
      [num(v.min_odds) && v.min_odds > 1 && v.min_odds <= 100, "Мин. кэф больше 1"],
      [num(v.max_odds) && v.max_odds > 1 && v.max_odds <= 1000, "Макс. кэф больше 1"],
      [v.price_source in PRICE_SOURCES, "Неизвестный источник кэфов"],
    ];
    for (const [ok, msg] of checks) if (!ok) fail(msg);
    const values = {
      bankroll: v.bankroll, currency: v.currency, kelly_fraction: v.kelly_fraction, max_stake_pct: v.max_stake_pct,
      min_ev: v.min_ev, min_odds: v.min_odds, max_odds: v.max_odds, price_source: v.price_source,
      markets: (v.markets || []).filter((m) => m in MARKETS),
      leagues: (v.leagues || []).filter((d) => d in site.leagues),
    };
    if (!values.leagues.length) fail("Выберите хотя бы одну лигу");
    if (values.min_odds >= values.max_odds) fail("Минимальный кэф должен быть меньше максимального");
    writeLS(KEYS.settings, values);
    return { settings: settings(), update_started: false };
  }

  function exportBackup() {
    return { app: "fbot", version: 1, exported_at: nowIso(), bets: getBets(), settings: readLS(KEYS.settings, {}) };
  }

  function importBackup(data) {
    if (!data || data.app !== "fbot" || !Array.isArray(data.bets)) fail("Это не файл резервной копии Value Bot");
    const need = ["id", "key", "div", "kickoff", "home", "away", "market", "selection", "odds", "stake", "status"];
    for (const b of data.bets) {
      if (!need.every((k) => k in b)) fail("Файл повреждён: у ставки нет обязательных полей");
    }
    saveBets(data.bets);
    if (data.settings && typeof data.settings === "object") writeLS(KEYS.settings, data.settings);
    writeLS(KEYS.seq, Math.max(0, ...data.bets.map((b) => b.id || 0)));
    settleAll();
    return { ok: true, bets: data.bets.length };
  }

  async function staticApi(path, opts = {}) {
    const method = (opts.method || "GET").toUpperCase();
    const body = opts.body ? JSON.parse(opts.body) : null;
    const [route, query] = path.split("?");
    const params = new URLSearchParams(query || "");
    if (route === "/api/update") {
      await load(true);
      return { started: false, reloaded: true };
    }
    await load(false);
    const betMatch = route.match(/^\/api\/bets\/(\d+)$/);
    if (route === "/api/status") return status();
    if (route === "/api/signals") return signals();
    if (route === "/api/fixtures") return fixtures();
    if (route === "/api/bets" && method === "GET") return listBets(params.get("status") || "all");
    if (route === "/api/bets" && method === "POST") return createBet(body);
    if (betMatch && method === "PATCH") return patchBet(Number(betMatch[1]), body);
    if (betMatch && method === "DELETE") return deleteBet(Number(betMatch[1]));
    if (route === "/api/stats") return getStats();
    if (route === "/api/model") return getModel();
    if (route === "/api/settings" && method === "GET") return getSettings();
    if (route === "/api/settings" && method === "PUT") return putSettings(body);
    if (route === "/api/backup" && method === "GET") return exportBackup();
    if (route === "/api/backup" && method === "POST") return importBackup(body);
    fail(`Неизвестный запрос ${method} ${route}`);
  }

  window.FBOT_STATIC_API = staticApi;
  // Ask the browser not to evict our storage (honoured for installed / frequently used sites).
  if (navigator.storage && navigator.storage.persist) navigator.storage.persist().catch(() => {});
})();

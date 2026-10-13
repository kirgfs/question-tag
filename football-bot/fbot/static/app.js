"use strict";

// ------------------------------------------------------------------ utils ---
const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
const view = $("#view");

const state = {
  tab: (location.hash || "#signals").slice(1),
  status: null,
  wasRunning: false,
  statsScope: "all",
  betsFilter: "open",
  fxLeague: "",
  fxSearch: "",
  fxOpen: new Set(),
  settings: null,
  bankroll: 0,
};

async function api(path, opts = {}) {
  // GitHub Pages build: answered in the browser by static-api.js
  if (window.FBOT_STATIC_API) return window.FBOT_STATIC_API(path, opts);
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  let body = null;
  try { body = await res.json(); } catch { /* empty */ }
  if (!res.ok) {
    let msg = res.statusText;
    if (body && body.detail) {
      msg = typeof body.detail === "string" ? body.detail
        : body.detail.map((d) => `${(d.loc || []).slice(-1)[0]}: ${d.msg}`).join("; ");
    }
    throw new Error(msg);
  }
  return body;
}

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const cur = () => (state.status && state.status.currency) || "₽";
const isNum = (v) => typeof v === "number" && isFinite(v);
const fmtNum = (v, d = 0) => isNum(v) ? v.toLocaleString("ru-RU", { minimumFractionDigits: d, maximumFractionDigits: d }) : "—";
const money = (v, sign = false) => isNum(v) ? `${sign && v > 0 ? "+" : ""}${fmtNum(v, Math.abs(v) < 100 && v % 1 ? 2 : 0)} ${cur()}` : "—";
const units = (v) => isNum(v) ? `${v > 0 ? "+" : ""}${fmtNum(v, 1)} ед.` : "—";
const pct = (v, d = 1, sign = false) => isNum(v) ? `${sign && v > 0 ? "+" : ""}${fmtNum(v * 100, d)}%` : "—";
const prob = (v) => isNum(v) ? `${fmtNum(v * 100, 1)}%` : "—";
const odds = (v) => isNum(v) ? v.toFixed(2) : "—";
const cls = (v) => !isNum(v) || Math.abs(v) < 1e-9 ? "" : v > 0 ? "pos" : "neg";
const dt = (iso) => iso ? new Date(iso).toLocaleString("ru-RU", { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "—";
const dday = (iso) => iso ? new Date(iso).toLocaleDateString("ru-RU", { day: "numeric", month: "short", year: "2-digit" }) : "—";
const tm = (iso) => iso ? new Date(iso).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" }) : "";
const SRC = { avg: "средний", max: "лучший", pinnacle: "Pinnacle", b365: "Bet365" };
const STATUS_RU = { open: "открыта", won: "выигрыш", lost: "проигрыш", void: "возврат" };

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => t.classList.remove("show"), 3200);
}

function empty(title, text) {
  return `<div class="panel"><div class="empty"><b>${esc(title)}</b>${text}</div></div>`;
}

function roiWithSe(roi, se) {
  if (!isNum(roi)) return "—";
  return `<span class="${cls(roi)}">${pct(roi, 1, true)}</span>${isNum(se) ? ` <small class="muted">± ${pct(se, 1)}</small>` : ""}`;
}

// ------------------------------------------------------------------ chart ---
function lineChart(points, { fmt = (v) => fmtNum(v), baseline = null, height = 240 } = {}) {
  const pts = points.filter((p) => isNum(p.v));
  if (pts.length < 2) return `<div class="empty small">Недостаточно данных для графика</div>`;
  const W = 800, H = height, L = 64, R = 14, T = 14, B = 26;
  const vs = pts.map((p) => p.v).concat(baseline ?? []);
  let lo = Math.min(...vs), hi = Math.max(...vs);
  if (hi - lo < 1e-9) { hi += 1; lo -= 1; }
  const pad = (hi - lo) * 0.08; lo -= pad; hi += pad;
  const x = (i) => L + (i / (pts.length - 1)) * (W - L - R);
  const y = (v) => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);
  const path = pts.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.v).toFixed(1)}`).join("");
  const last = pts[pts.length - 1].v;
  const up = last >= (baseline ?? pts[0].v);
  const color = up ? "var(--pos)" : "var(--neg)";
  let grid = "";
  for (let k = 0; k <= 4; k++) {
    const v = lo + ((hi - lo) * k) / 4;
    grid += `<line class="grid" x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}"/>` +
      `<text class="axis" x="${L - 8}" y="${y(v) + 4}" text-anchor="end">${esc(fmt(v))}</text>`;
  }
  const firstT = pts.find((p) => p.t)?.t, lastT = pts[pts.length - 1].t;
  const xl = `<text class="axis" x="${L}" y="${H - 6}">${esc(dday(firstT))}</text>` +
    `<text class="axis" x="${W - R}" y="${H - 6}" text-anchor="end">${esc(dday(lastT))}</text>`;
  const base = baseline !== null ? `<line x1="${L}" x2="${W - R}" y1="${y(baseline)}" y2="${y(baseline)}" stroke="var(--muted)" stroke-dasharray="4 4"/>` : "";
  const area = `${path}L${x(pts.length - 1)},${H - B}L${x(0)},${H - B}Z`;
  const id = "c" + Math.random().toString(36).slice(2, 8);
  setTimeout(() => attachChartHover(id, pts, x, y, W, H, fmt), 0);
  return `<div class="chart" id="${id}"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="график">
    ${grid}${base}<path d="${area}" fill="${color}" opacity=".08"/>
    <path d="${path}" fill="none" stroke="${color}" stroke-width="2" stroke-linejoin="round"/>
    <circle cx="${x(pts.length - 1)}" cy="${y(last)}" r="3.5" fill="${color}"/>
    <circle class="hover-dot" r="4" fill="${color}" style="display:none"/>
    ${xl}<rect x="${L}" y="${T}" width="${W - L - R}" height="${H - T - B}" fill="transparent"/>
  </svg><div class="tip"></div></div>`;
}

function attachChartHover(id, pts, x, y, W, H, fmt) {
  const box = document.getElementById(id);
  if (!box) return;
  const svg = $("svg", box), tip = $(".tip", box), dot = $(".hover-dot", box);
  svg.addEventListener("mousemove", (e) => {
    const r = svg.getBoundingClientRect();
    const sx = ((e.clientX - r.left) / r.width) * W;
    let i = Math.round(((sx - x(0)) / (x(pts.length - 1) - x(0))) * (pts.length - 1));
    i = Math.max(0, Math.min(pts.length - 1, i));
    const p = pts[i];
    dot.setAttribute("cx", x(i)); dot.setAttribute("cy", y(p.v)); dot.style.display = "";
    tip.style.display = "block";
    tip.style.left = `${(x(i) / W) * r.width}px`;
    tip.style.top = `${(y(p.v) / H) * r.height}px`;
    tip.textContent = `${p.t ? dday(p.t) + " · " : ""}${fmt(p.v)}`;
  });
  svg.addEventListener("mouseleave", () => { tip.style.display = "none"; dot.style.display = "none"; });
}

// ----------------------------------------------------------------- status ---
async function refreshStatus() {
  let s;
  try { s = await api("/api/status"); } catch { return; }
  state.status = s;
  $("#kpis").innerHTML = [
    ["Банк", money(s.bankroll)],
    ["Прибыль", `<span class="${cls(s.profit)}">${money(s.profit, true)}</span>`],
    ["ROI", `<span class="${cls(s.roi)}">${pct(s.roi, 1, true)}</span>`],
    ["В игре", `${s.counts.open_bets} · ${money(s.counts.open_stake)}`],
  ].map(([k, v]) => `<div class="kpi"><span>${k}</span><b>${v}</b></div>`).join("");
  $("#badge-signals").textContent = s.counts.signals || "";
  $("#badge-open").textContent = s.counts.open_bets || "";
  $("#upd-text").innerHTML = s.running ? `<span class="spinner"></span>обновляю…`
    : s.last_update ? `${s.static ? "данные от" : "обновлено"}<br>${esc(dt(s.last_update))}` : "данных ещё нет";
  $("#btn-update").disabled = s.running;

  const prog = $("#progress");
  if (s.running || s.error) {
    prog.hidden = false;
    prog.innerHTML = `<div class="box ${s.error && !s.running ? "warn" : ""}">
      ${s.running ? `<span class="spinner"></span><b>Обновление:</b> ${esc(s.step)}` : `<b>Ошибка обновления:</b> ${esc(s.error)}`}
      <pre>${esc(s.log.slice(-12).join("\n"))}</pre></div>`;
  } else if (s.stale_hours) {
    prog.hidden = false;
    prog.innerHTML = `<div class="box warn"><b>Данные не обновлялись ${fmtNum(s.stale_hours, 0)} ч.</b>
      Автопересчёт на GitHub мог остановиться${s.repo_url ? ` — открой <a href="${esc(s.repo_url)}/actions" target="_blank" rel="noopener">GitHub Actions</a> и запусти его заново` : ""}.</div>`;
  } else {
    prog.hidden = true;
  }
  if (state.wasRunning && !s.running) {
    toast(s.error ? "Обновление завершилось с ошибкой" : "Данные обновлены");
    render();
  }
  state.wasRunning = s.running;
  clearTimeout(refreshStatus.timer);
  refreshStatus.timer = setTimeout(refreshStatus, s.running ? 2500 : s.static ? 60000 : 30000);
}

$("#btn-update").addEventListener("click", async () => {
  let r;
  try { r = await api("/api/update", { method: "POST" }); } catch (e) { toast("Ошибка: " + e.message); return; }
  if (r.reloaded) {
    toast("Загружены свежие данные");
    await refreshStatus();
    render();
    return;
  }
  toast(r.started ? "Обновление запущено" : "Обновление уже идёт");
  state.wasRunning = true;
  refreshStatus();
});

// ------------------------------------------------------------- bet dialog ---
const dlg = $("#bet-dialog");
let dlgBet = null;

function openBet(b) {
  dlgBet = b;
  $("#dlg-title").textContent = `${b.label} · ${b.home} — ${b.away}`;
  $("#dlg-sub").textContent = `${b.league} · ${dt(b.kickoff)}${b.tip_id ? " · по сигналу бота" : " · без сигнала бота"}`;
  $("#dlg-odds").value = b.price ? b.price.toFixed(2) : "";
  $("#dlg-stake").value = b.stake || "";
  $("#dlg-book").value = localStorage.getItem("fbot.book") || "";
  $("#dlg-note").value = "";
  calcBet();
  dlg.showModal();
  $("#dlg-odds").focus();
}

function calcBet() {
  if (!dlgBet) return;
  const o = parseFloat($("#dlg-odds").value), st = parseFloat($("#dlg-stake").value);
  const p = dlgBet.prob, s = state.settings;
  const rows = [];
  if (isNum(p)) {
    rows.push(["Вероятность по модели", prob(p)]);
    if (isNum(dlgBet.market_prob)) rows.push(["Вероятность рынка (без маржи)", prob(dlgBet.market_prob)]);
    rows.push(["Мин. кэф для ставки", odds((1 + s.min_ev) / p)]);
    if (isNum(o) && o > 1) {
      const ev = p * o - 1;
      const kelly = Math.max(0, ev / (o - 1));
      const raw = Math.max(0, state.bankroll * Math.min(s.kelly_fraction * kelly, s.max_stake_pct));
      const rec = raw >= 100 ? Math.round(raw / 10) * 10 : Math.round(raw);  // same rounding as the server
      rows.push(["Ожидаемая доходность (EV)", `<b class="${cls(ev)}">${pct(ev, 1, true)}</b>`]);
      rows.push(["Рекомендуемая сумма", money(rec)]);
      if (isNum(st)) rows.push(["Ожидаемая прибыль", `<span class="${cls(ev)}">${money(st * ev, true)}</span>`]);
      if (ev < 0) rows.push(["", `<span class="neg">По этому кэфу ставка невыгодна</span>`]);
    }
  } else {
    rows.push(["", "Нет оценки модели для этого исхода"]);
  }
  $("#dlg-calc").innerHTML = rows.map(([k, v]) => `<div><span class="muted">${k}</span><span class="num">${v}</span></div>`).join("");
}
["#dlg-odds", "#dlg-stake"].forEach((s) => $(s).addEventListener("input", calcBet));

$("#bet-form").addEventListener("submit", async (e) => {
  if (e.submitter && e.submitter.value !== "save") return;
  e.preventDefault();
  const book = $("#dlg-book").value.trim();
  try {
    await api("/api/bets", {
      method: "POST",
      body: JSON.stringify({
        key: dlgBet.key, market: dlgBet.market, selection: dlgBet.selection,
        odds: parseFloat($("#dlg-odds").value), stake: parseFloat($("#dlg-stake").value),
        bookmaker: book || null, note: $("#dlg-note").value.trim() || null, tip_id: dlgBet.tip_id || null,
      }),
    });
    if (book) localStorage.setItem("fbot.book", book);
    dlg.close();
    toast("Ставка записана");
    refreshStatus();
    render();
  } catch (err) {
    toast("Ошибка: " + err.message);
  }
});

// ---------------------------------------------------------------- signals ---
function modelNotes(markets) {
  const s = state.status;
  if (!s || !s.models) return "";
  const byText = new Map();  // markets with the same verdict share one line
  for (const m of markets) {
    const r = s.models[m];
    if (!r) continue;
    let text = null;
    if (r.champion === "market") {
      text = `на исторической проверке (${fmtNum(r.n)} матчей) ни одна модель не точнее честной цены Pinnacle. Сигналы здесь — места, где кэф букмекеров выше честной цены рынка.`;
    } else if (isNum(r.logloss) && isNum(r.market_logloss)) {
      text = `«${esc(r.title)}» точнее рынка на ${fmtNum(r.n)} матчах вне выборки (log-loss ${r.logloss.toFixed(4)} против ${r.market_logloss.toFixed(4)}).`;
    }
    if (text) byText.set(text, [...(byText.get(text) || []), m === "1X2" ? "Исход матча" : "Тотал 2.5"]);
  }
  const out = [...byText].map(([text, names]) => `<p><b>${names.join(" и ")}:</b> ${text}</p>`);
  return out.length ? `<div class="note">${out.join("")}<p class="muted small">Подробности и бэктест — на вкладке «Модель». Кэфы в fixtures.csv — снимок на момент выгрузки: перед ставкой сверь кэф у своей конторы с колонкой «мин. кэф».</p></div>` : "";
}

async function renderSignals() {
  const d = await api("/api/signals");
  state.settings = d.settings; state.bankroll = d.bankroll;
  const s = d.settings;
  if (!state.status?.last_update) {
    view.innerHTML = empty("Данные ещё не загружены", "<p>Первое обновление запускается автоматически и занимает 1–3 минуты.</p>");
    return;
  }
  const head = modelNotes(s.markets);
  if (!d.signals.length) {
    view.innerHTML = head + empty("Сейчас нет ставок с перевесом",
      `<p>Порог: EV ≥ ${pct(s.min_ev, 0)}, кэфы ${odds(s.min_odds)}–${odds(s.max_odds)}, цена — ${esc(SRC[s.price_source])}.</p>
       <p>Вероятности и честные кэфы по всем матчам — на вкладке «Матчи».</p>`);
    return;
  }
  const rows = d.signals.map((t, i) => `
    <tr>
      <td class="nowrap full t-time">${esc(dt(t.kickoff))}</td>
      <td class="match full"><b>${esc(t.home)} — ${esc(t.away)}</b><div class="sub">${esc(t.league)}</div></td>
      <td data-l="Ставка"><span class="pick">${esc(t.label)}</span></td>
      <td class="r num" data-l="Модель">${prob(t.model_prob)}</td>
      <td class="r num muted" data-l="Рынок">${prob(t.market_prob)}</td>
      <td class="r num" data-l="Кэф"><b>${odds(t.price)}</b><div class="small muted">${esc(SRC[t.price_source] || "")}</div></td>
      <td class="r num" data-l="Мин. кэф">${odds(t.min_odds)}</td>
      <td class="r num pos" data-l="EV"><b>${pct(t.ev, 1, true)}</b></td>
      <td class="r num" data-l="Сумма"><b>${money(t.stake)}</b></td>
      <td class="r act2">${t.placed ? `<span class="chip won">поставлено${t.placed > 1 ? " ×" + t.placed : ""}</span>`
        : `<button class="btn primary sm" data-sig="${i}">Поставил</button>`}</td>
    </tr>`).join("");
  view.innerHTML = head + `<div class="panel">
    <div class="panel-head"><h2>Ставки с перевесом</h2><span class="muted small">EV ≥ ${pct(s.min_ev, 0)} · ${esc(SRC[s.price_source])} кэф · ${s.kelly_fraction} Келли, не больше ${pct(s.max_stake_pct, 0)} банка</span></div>
    <div class="table-wrap"><table class="rt">
      <thead><tr><th>Начало</th><th>Матч</th><th>Ставка</th><th class="r">Модель</th><th class="r">Рынок</th>
      <th class="r">Кэф</th><th class="r">Мин. кэф</th><th class="r">EV</th><th class="r">Сумма</th><th></th></tr></thead>
      <tbody>${rows}</tbody></table></div></div>`;
  $$("[data-sig]").forEach((btn) => btn.addEventListener("click", () => {
    const t = d.signals[+btn.dataset.sig];
    openBet({ ...t, prob: t.model_prob, tip_id: t.id });
  }));
}

// --------------------------------------------------------------- fixtures ---
async function renderFixtures() {
  const d = await api("/api/fixtures");
  state.settings = d.settings; state.bankroll = d.bankroll;
  if (!d.fixtures.length) {
    view.innerHTML = empty("Нет ближайших матчей", "<p>fixtures.csv обновляется на football-data.co.uk обычно по вторникам и пятницам.</p>");
    return;
  }
  const leagues = [...new Set(d.fixtures.map((f) => f.league))].sort();
  const q = state.fxSearch.toLowerCase();
  const list = d.fixtures.filter((f) => (!state.fxLeague || f.league === state.fxLeague) &&
    (!q || (f.home + " " + f.away).toLowerCase().includes(q)));

  const cell = (f, market, it) => {
    if (!isNum(it.prob)) return `<div class="cell na"><div class="l">${esc(it.label)}</div><div class="p">—</div><div class="o">${odds(it.price)}</div></div>`;
    return `<div class="cell ${it.tip_id ? "sig" : ""}" data-key="${esc(f.key)}" data-m="${market}" data-s="${it.selection}" title="Мин. кэф для ставки: ${odds(it.min_odds)}">
      <div class="l">${esc(it.label)}</div><div class="p">${prob(it.prob)}</div>
      <div class="o">${odds(it.price)} <span class="${cls(it.ev)}">${isNum(it.ev) ? pct(it.ev, 0, true) : ""}</span></div></div>`;
  };
  const rows = list.map((f) => {
    const v = f.view;
    const open = state.fxOpen.has(f.key);
    return `<div class="fx">
      <div class="fx-row">
        <div class="fx-time"><b>${esc(tm(f.kickoff))}</b>${esc(new Date(f.kickoff).toLocaleDateString("ru-RU", { weekday: "short", day: "numeric", month: "short" }))}</div>
        <div class="match"><b>${esc(f.home)} — ${esc(f.away)}</b><div class="sub">${esc(f.league)}${f.dc ? ` · xG ${f.dc.lh.toFixed(2)}:${f.dc.la.toFixed(2)}` : " · мало данных для модели"}</div></div>
        <div class="odds-grid">${v["1X2"].items.map((it) => cell(f, "1X2", it)).join("")}<span></span>${v.OU25.items.map((it) => cell(f, "OU25", it)).join("")}</div>
        <button class="expand" data-exp="${esc(f.key)}" aria-label="подробнее">${open ? "▴" : "▾"}</button>
      </div>
      ${open ? fixtureDetail(f) : ""}
    </div>`;
  }).join("");
  view.innerHTML = `<div class="toolbar">
      <select id="fx-league"><option value="">Все лиги</option>${leagues.map((l) => `<option ${l === state.fxLeague ? "selected" : ""}>${esc(l)}</option>`).join("")}</select>
      <input type="search" id="fx-search" placeholder="Команда…" value="${esc(state.fxSearch)}">
      <span class="grow"></span>
      <span class="muted small">В ячейке: вероятность модели, кэф (${esc(SRC[d.settings.price_source])}) и EV. Зелёная рамка — сигнал. Нажми на исход, чтобы записать ставку.</span>
    </div>
    <div class="panel">${rows || `<div class="empty">Ничего не найдено</div>`}</div>`;

  $("#fx-league").addEventListener("change", (e) => { state.fxLeague = e.target.value; renderFixtures(); });
  $("#fx-search").addEventListener("input", (e) => {
    state.fxSearch = e.target.value;
    clearTimeout(renderFixtures.t);
    renderFixtures.t = setTimeout(() => renderFixtures().then(() => { const el = $("#fx-search"); el.focus(); el.setSelectionRange(el.value.length, el.value.length); }), 250);
  });
  $$("[data-exp]").forEach((b) => b.addEventListener("click", () => {
    const k = b.dataset.exp;
    state.fxOpen.has(k) ? state.fxOpen.delete(k) : state.fxOpen.add(k);
    renderFixtures();
  }));
  $$(".cell[data-key]").forEach((c) => c.addEventListener("click", () => {
    const f = d.fixtures.find((x) => x.key === c.dataset.key);
    const it = f.view[c.dataset.m].items.find((x) => x.selection === c.dataset.s);
    openBet({ key: f.key, market: c.dataset.m, selection: it.selection, label: it.label, home: f.home, away: f.away,
      league: f.league, kickoff: f.kickoff, prob: it.prob, market_prob: it.market_prob, price: it.price,
      stake: it.stake, tip_id: it.tip_id });
  }));
}

function fixtureDetail(f) {
  const blocks = [];
  const mk = (m) => f.view[m].items;
  const probRows = (m) => mk(m).map((it) => `<tr><td>${esc(it.label)}</td><td class="r num">${prob(it.prob)}</td><td class="r num">${prob(it.dc_prob)}</td><td class="r num">${prob(it.market_prob)}</td><td class="r num">${odds(it.fair_odds)}</td></tr>`).join("");
  blocks.push(`<div><h4>Вероятности</h4><table><thead><tr><th></th><th class="r">Итог</th><th class="r">Dixon-Coles</th><th class="r">Рынок</th><th class="r">Честный кэф</th></tr></thead>
    <tbody>${probRows("1X2")}${probRows("OU25")}</tbody></table></div>`);
  const src = ["avg", "max", "pinnacle", "b365"];
  const oddsRows = ["1X2", "OU25"].flatMap((m) => mk(m).map((it) => `<tr><td>${esc(it.label)}</td>${src.map((s) => `<td class="r num">${odds(it.odds[s])}</td>`).join("")}</tr>`)).join("");
  blocks.push(`<div><h4>Кэфы букмекеров</h4><table><thead><tr><th></th>${src.map((s) => `<th class="r">${SRC[s]}</th>`).join("")}</tr></thead><tbody>${oddsRows}</tbody></table></div>`);
  const ex = f.extras || {};
  const extra = [];
  if (ex.scores) extra.push(`<h4>Вероятные счета</h4><table><tbody>${ex.scores.map((s) => `<tr><td>${esc(s.score)}</td><td class="r num">${prob(s.p)}</td></tr>`).join("")}</tbody></table>`);
  if (isNum(ex.btts)) extra.push(`<p class="small">Обе забьют: <b>${prob(ex.btts)}</b> (честный кэф ${odds(1 / ex.btts)})</p>`);
  if (ex.ah) {
    const a = ex.ah, line = (v) => (v > 0 ? "+" : "") + v;
    const mo = f.ah_odds || {};
    extra.push(`<h4>Азиатская фора (справочно)</h4><table><thead><tr><th></th><th class="r">Честный кэф</th><th class="r">Pinnacle</th><th class="r">Средний</th></tr></thead><tbody>
      <tr><td>Ф1 (${line(a.line)})</td><td class="r num">${odds(a.home.fair_odds)}</td><td class="r num">${odds(mo.pinnacle?.[0])}</td><td class="r num">${odds(mo.avg?.[0])}</td></tr>
      <tr><td>Ф2 (${line(-a.line)})</td><td class="r num">${odds(a.away.fair_odds)}</td><td class="r num">${odds(mo.pinnacle?.[1])}</td><td class="r num">${odds(mo.avg?.[1])}</td></tr></tbody></table>`);
  }
  if (isNum(f.pi_gd)) extra.push(`<p class="small muted">Pi-рейтинг: ожидаемая разница мячей ${f.pi_gd > 0 ? "+" : ""}${f.pi_gd.toFixed(2)}</p>`);
  blocks.push(`<div>${extra.join("") || '<span class="muted">Нет данных Dixon-Coles</span>'}</div>`);
  return `<div class="fx-detail">${blocks.join("")}</div>`;
}

// ------------------------------------------------------------------- bets ---
async function renderBets() {
  const d = await api(`/api/bets?status=${state.betsFilter}`);
  const seg = [["open", "Открытые"], ["settled", "Рассчитанные"], ["all", "Все"]]
    .map(([k, l]) => `<button class="${state.betsFilter === k ? "active" : ""}" data-f="${k}">${l}</button>`).join("");
  const rows = d.bets.map((b) => `
    <tr>
      <td class="nowrap full t-time">${esc(dt(b.kickoff))}</td>
      <td class="match full"><b>${esc(b.home)} — ${esc(b.away)}</b><div class="sub">${esc(b.league)}${b.bookmaker ? " · " + esc(b.bookmaker) : ""}${b.note ? " · " + esc(b.note) : ""}</div></td>
      <td data-l="Ставка"><span class="pick">${esc(b.label)}</span>${b.by_tip ? "" : ' <span class="chip" title="Ставка не по сигналу бота">свой</span>'}</td>
      <td class="r num" data-l="Кэф"><a href="#" class="edit" data-edit-odds="${b.id}" title="изменить">${odds(b.odds)}</a></td>
      <td class="r num" data-l="Сумма"><a href="#" class="edit" data-edit-stake="${b.id}" title="изменить">${money(b.stake)}</a></td>
      <td class="r num ${cls(b.ev)}" data-l="EV">${pct(b.ev, 1, true)}</td>
      <td class="r num ${cls(b.clv)}" data-l="CLV" title="${isNum(b.close_odds) ? "закрытие: " + odds(b.close_odds) : ""}">${pct(b.clv, 1, true)}</td>
      <td class="c" data-l="Статус"><span class="chip ${b.status}">${STATUS_RU[b.status]}</span>${isNum(b.fthg) ? ` <span class="small muted">${b.fthg}:${b.ftag}</span>` : ""}</td>
      <td class="r num ${cls(b.profit)}" data-l="Прибыль"><b>${b.status === "open" ? "—" : money(b.profit, true)}</b></td>
      <td class="r nowrap act2">${b.status === "open"
        ? `<button class="btn sm" data-set="${b.id}:won" title="Отметить выигрыш">✓</button><button class="btn sm" data-set="${b.id}:lost" title="Отметить проигрыш">✗</button><button class="btn sm" data-set="${b.id}:void" title="Возврат">↺</button>`
        : `<button class="btn sm ghost" data-set="${b.id}:open" title="Снова открыть">вернуть</button>`}
        <button class="btn sm ghost danger" data-del="${b.id}" title="Удалить">удалить</button></td>
    </tr>`).join("");
  view.innerHTML = `<div class="toolbar"><div class="seg">${seg}</div><span class="grow"></span>
      <span class="muted small">Ставки рассчитываются автоматически, когда в данных появляется результат. Если матч перенесли — отметь вручную.</span></div>
    ${d.bets.length ? `<div class="panel"><div class="table-wrap"><table class="rt">
      <thead><tr><th>Начало</th><th>Матч</th><th>Ставка</th><th class="r">Кэф</th><th class="r">Сумма</th><th class="r">EV</th>
      <th class="r" title="Доходность к честной цене закрытия Pinnacle">CLV</th><th class="c">Статус</th><th class="r">Прибыль</th><th></th></tr></thead>
      <tbody>${rows}</tbody></table></div></div>`
      : empty("Ставок нет", "<p>Нажми «Поставил» на вкладке «Сигналы» или на исходе во вкладке «Матчи».</p>")}`;

  $$("[data-f]").forEach((b) => b.addEventListener("click", () => { state.betsFilter = b.dataset.f; renderBets(); }));
  const patch = async (id, body) => {
    try { await api(`/api/bets/${id}`, { method: "PATCH", body: JSON.stringify(body) }); refreshStatus(); renderBets(); }
    catch (e) { toast("Ошибка: " + e.message); }
  };
  $$("[data-set]").forEach((b) => b.addEventListener("click", () => {
    const [id, st] = b.dataset.set.split(":");
    patch(id, { status: st });
  }));
  $$("[data-del]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("Удалить ставку?")) return;
    await api(`/api/bets/${b.dataset.del}`, { method: "DELETE" });
    refreshStatus(); renderBets();
  }));
  $$("[data-edit-odds]").forEach((a) => a.addEventListener("click", (e) => {
    e.preventDefault();
    const v = prompt("Кэф", a.textContent);
    if (v) patch(a.dataset.editOdds, { odds: parseFloat(v.replace(",", ".")) });
  }));
  $$("[data-edit-stake]").forEach((a) => a.addEventListener("click", (e) => {
    e.preventDefault();
    const v = prompt("Сумма ставки", a.textContent.replace(/[^\d.,]/g, "").replace(",", "."));
    if (v) patch(a.dataset.editStake, { stake: parseFloat(v.replace(",", ".")) });
  }));
}

// ------------------------------------------------------------------ stats ---
function groupTable(title, groups, isUnits) {
  if (!groups || !groups.length) return "";
  const m = isUnits ? units : (v) => money(v, true);
  return `<div class="panel"><h2>${title}</h2><div class="table-wrap"><table>
    <thead><tr><th></th><th class="r">Ставок</th><th class="r">Выигрыш</th><th class="r">Прибыль</th><th class="r">ROI</th></tr></thead>
    <tbody>${groups.map((g) => `<tr><td>${esc(g.name)}</td><td class="r num">${g.n}</td><td class="r num">${pct(g.hit_rate, 0)}</td>
      <td class="r num ${cls(g.profit)}">${m(g.profit)}</td><td class="r num ${cls(g.roi)}">${pct(g.roi, 1, true)}</td></tr>`).join("")}</tbody>
  </table></div></div>`;
}

async function renderStats() {
  const d = await api("/api/stats");
  const scopes = [["all", "Все мои ставки"], ["by_tip", "По сигналам бота"], ["own", "Свои (без сигнала)"], ["paper", "Все сигналы бота · 1 ед."]];
  const s = d[state.statsScope];
  const isUnits = state.statsScope === "paper";
  const m = isUnits ? units : (v) => money(v, true);
  const seg = scopes.map(([k, l]) => `<button class="${state.statsScope === k ? "active" : ""}" data-sc="${k}">${l}</button>`).join("");
  const tiles = [
    ["Рассчитано", `${s.n}`, `открыто: ${s.open}${s.void ? ` · возвратов: ${s.void}` : ""}`],
    ["Прибыль", `<span class="${cls(s.profit)}">${m(s.profit)}</span>`, isNum(s.expected_profit) ? `ожидалось: ${m(s.expected_profit)}` : ""],
    ["ROI", roiWithSe(s.roi, s.roi_se), isUnits ? "на 1 ед. ставки" : `поставлено: ${money(s.staked)}`],
    ["Выигрышных", pct(s.hit_rate, 1), `${s.won} из ${s.n}`],
    ["Средний кэф", odds(s.avg_odds), isNum(s.avg_ev) ? `средний EV: ${pct(s.avg_ev, 1, true)}` : ""],
    ["CLV", `<span class="${cls(s.avg_clv)}">${pct(s.avg_clv, 1, true)}</span>`, isNum(s.beat_close) ? `лучше закрытия: ${pct(s.beat_close, 0)}` : "нет данных закрытия"],
    ["Макс. просадка", isUnits ? units(-s.max_drawdown) : money(-s.max_drawdown), isNum(s.max_drawdown_pct) && !isUnits ? pct(s.max_drawdown_pct, 1) + " от пика" : ""],
  ].map(([k, v, sub]) => `<div class="tile"><span>${k}</span><b>${v}</b><small>${sub}</small></div>`).join("");
  const explain = s.n < 300 ? `<div class="note"><p>Выборка маленькая: при ${s.n} ставках ROI колеблется примерно на ± ${pct(s.roi_se ?? 0.1, 0)} просто из-за везения.
      Надёжнее смотреть на <b>CLV</b>: если ты в среднем берёшь кэф выше закрытия Pinnacle, перевес реальный, даже когда ROI пока минусовой.</p></div>` : "";
  view.innerHTML = `<div class="toolbar"><div class="seg">${seg}</div></div>
    ${s.total ? `<div class="tiles">${tiles}</div>${explain}
    <div class="panel"><h2>${isUnits ? "Прибыль сигналов бота, единицы" : "Банк"}</h2><div class="panel-body">
      ${lineChart(s.curve, { fmt: (v) => isUnits ? fmtNum(v, 1) : fmtNum(v), baseline: s.curve[0]?.v ?? 0 })}</div></div>
    <div class="grid2 wide">${groupTable("По лигам", s.by_league, isUnits)}${groupTable("По типу ставки", s.by_selection, isUnits)}${groupTable("По кэфу", s.by_odds, isUnits)}</div>`
      : empty("Пока нет ставок в этом разделе", isUnits ? "<p>Здесь копятся все сигналы бота после начала матча — как будто на каждый поставили 1 единицу.</p>" : "<p>Записывай ставки кнопкой «Поставил» — статистика появится после расчёта первых матчей.</p>")}`;
  $$("[data-sc]").forEach((b) => b.addEventListener("click", () => { state.statsScope = b.dataset.sc; renderStats(); }));
}

// ------------------------------------------------------------------ model ---
async function renderModel() {
  const d = await api("/api/model");
  if (!d.report) {
    view.innerHTML = empty("Модель ещё не обучена", "<p>Дождись первого обновления данных.</p>");
    return;
  }
  const r = d.report, bt = d.backtest, s = d.settings;
  const marketCards = Object.entries(r.markets).map(([m, info]) => {
    const rows = [...info.metrics].sort((a, b) => a.logloss - b.logloss).map((x) => `
      <tr class="${x.model === info.champion ? "hl" : ""}"><td>${esc(x.title)}${x.model === info.champion ? " ★" : ""}</td>
      <td class="r num">${x.logloss.toFixed(4)}</td><td class="r num ${x.model === "market" ? "" : cls(-x.delta)}">${x.model === "market" || !isNum(x.delta) ? "—" : `${x.delta > 0 ? "+" : "−"}${Math.abs(x.delta).toFixed(4)} <small class="muted">± ${x.delta_se.toFixed(4)}</small>`}</td>
      <td class="r num hide-m">${x.rps.toFixed(4)}</td><td class="r num hide-m">${pct(x.accuracy, 1)}</td></tr>`).join("");
    const w = info.pool_weights ? Object.entries(info.pool_weights).map(([k, v]) => `${{ market: "рынок", dc: "Dixon-Coles", pi: "Pi" }[k]} ${v.toFixed(2)}`).join(" · ") : "";
    const n = info.metrics[0]?.n || 0;
    return `<div class="panel"><div class="panel-head"><h2>${m === "1X2" ? "Исход матча (1X2)" : "Тотал 2.5"}</h2><span class="muted small">${fmtNum(n)} матчей вне выборки</span></div>
      <div class="table-wrap"><table><thead><tr><th>Модель</th><th class="r">Log-loss</th><th class="r" title="Разница log-loss с рынком ± стандартная ошибка">Δ к рынку</th><th class="r hide-m">RPS</th><th class="r hide-m">Угадано</th></tr></thead><tbody>${rows}</tbody></table></div>
      <div class="panel-body small muted">★ — используется для прогнозов. Меньше — лучше. Модель заменяет рынок, только если обыгрывает его больше чем на одну стандартную ошибку.${w ? `<br>Веса смешивания: ${w}` : ""}</div></div>`;
  }).join("");

  let btHtml = `<div class="panel"><div class="panel-body muted">Для бэктеста нужно хотя бы два сезона истории.</div></div>`;
  if (bt.available) {
    const sm = bt.summary;
    const tbl = (rows, first) => `<div class="table-wrap"><table><thead><tr><th>${first}</th><th class="r">Ставок</th><th class="r">Средний кэф</th><th class="r">Выигрыш</th><th class="r">Прибыль</th><th class="r">ROI</th><th class="r">CLV</th></tr></thead>
      <tbody>${rows.map((x) => `<tr><td>${x.label}</td><td class="r num">${x.n}</td><td class="r num">${odds(x.avg_odds)}</td><td class="r num">${pct(x.hit_rate, 0)}</td>
      <td class="r num ${cls(x.profit)}">${units(x.profit)}</td><td class="r num">${roiWithSe(x.roi, x.roi_se)}</td><td class="r num ${cls(x.avg_clv)}">${pct(x.avg_clv, 1, true)}</td></tr>`).join("")}</tbody></table></div>`;
    btHtml = `<div class="tiles">
        <div class="tile"><span>Ставок в бэктесте</span><b>${sm.n}</b><small>${esc(SRC[s.price_source])} кэф, EV ≥ ${pct(s.min_ev, 0)}</small></div>
        <div class="tile"><span>ROI</span><b>${roiWithSe(sm.roi, sm.roi_se)}</b><small>плоская ставка 1 ед.</small></div>
        <div class="tile"><span>Прибыль</span><b class="${cls(sm.profit)}">${units(sm.profit)}</b><small>выигрыш ${pct(sm.hit_rate, 0)}</small></div>
        <div class="tile"><span>CLV</span><b class="${cls(sm.avg_clv)}">${pct(sm.avg_clv, 1, true)}</b><small>к закрытию Pinnacle</small></div>
      </div>
      <div class="panel"><h2>Бэктест текущих настроек вне выборки, единицы</h2><div class="panel-body">${lineChart(bt.curve, { fmt: (v) => fmtNum(v, 1), baseline: 0 })}</div></div>
      <div class="grid2 wide">
        <div class="panel"><h2>Порог EV (лучшая модель)</h2>${tbl(bt.thresholds.map((x) => ({ ...x, label: "≥ " + pct(x.min_ev, 0) })), "Порог")}</div>
        <div class="panel"><h2>Если ставить по одной модели</h2>${tbl(bt.by_model.map((x) => ({ ...x, label: esc(d.names[x.model]) })), "Модель")}</div>
        <div class="panel"><h2>По сезонам</h2>${tbl(bt.by_season.map((x) => ({ ...x, label: esc(x.name) })), "Сезон")}</div>
        <div class="panel"><h2>По лигам</h2>${tbl(bt.by_league.map((x) => ({ ...x, label: esc(x.name) })), "Лига")}</div>
      </div>`;
  }

  const lg = r.leagues.map((l) => `<tr><td>${esc(l.name)}</td><td class="r num">${fmtNum(l.matches)}</td><td>${l.seasons.map((x) => x.slice(0, 2) + "/" + x.slice(2)).join(", ")}</td>
    <td class="r num">${esc(dday(l.last_result))}</td><td class="r num">${l.upcoming}</td></tr>`).join("");
  view.innerHTML = `<div class="note"><p>Как проверяется модель: Dixon-Coles переобучается каждую неделю только на прошлых матчах, Pi-рейтинги обновляются по ходу сезона,
      ансамбли учатся на предыдущих сезонах и проверяются на следующем. Метрики ниже — на матчах, которых модель не видела.
      «Рынок» — вероятности Pinnacle без маржи (метод Шина) на момент выгрузки, главный ориентир.</p>
      <p class="muted small">Ансамбль-бустинг: ${r.xgboost ? "XGBoost" : "XGBoost не установлен, используется бустинг из scikit-learn"} · отчёт от ${esc(dt(r.generated_at))}</p>
      ${d.static ? `<p class="muted small">Бэктест посчитан на GitHub для настроек по умолчанию (EV ≥ ${pct(s.min_ev, 0)}, ${esc(SRC[s.price_source])} кэф, ${odds(s.min_odds)}–${odds(s.max_odds)}), а не для твоих.</p>` : ""}</div>
    <div class="grid2">${marketCards}</div>
    <h3 style="margin:22px 0 12px">Историческая проверка стратегии</h3>
    ${btHtml}
    <div class="panel"><h2>Данные</h2><div class="table-wrap"><table><thead><tr><th>Лига</th><th class="r">Матчей</th><th>Сезоны</th><th class="r">Последний результат</th><th class="r">Впереди</th></tr></thead><tbody>${lg}</tbody></table></div></div>`;
}

// --------------------------------------------------------------- settings ---
async function renderSettings() {
  const d = await api("/api/settings");
  const s = d.settings;
  const f = (id, label, value, attrs = "", hint = "") => `<div class="field"><label for="${id}">${label}</label><input id="${id}" value="${esc(value)}" ${attrs}>${hint ? `<div class="hint">${hint}</div>` : ""}</div>`;
  view.innerHTML = `<form id="settings-form" class="stack">
    <div class="panel"><h2>Банк и размер ставок</h2><div class="panel-body form">
      ${f("s-bank", "Стартовый банк", s.bankroll, 'type="number" min="1" step="1" required', "Текущий банк = стартовый + результат ставок")}
      ${f("s-cur", "Валюта", s.currency, 'maxlength="5" required')}
      ${f("s-kelly", "Доля Келли", s.kelly_fraction, 'type="number" min="0.01" max="1" step="0.01" required', "0.25 = четверть Келли (рекомендуется)")}
      ${f("s-cap", "Макс. ставка, % банка", +(s.max_stake_pct * 100).toFixed(2), 'type="number" min="0.1" max="50" step="0.1" required')}
    </div></div>
    <div class="panel"><h2>Когда давать сигнал</h2><div class="panel-body form">
      ${f("s-ev", "Мин. EV, %", +(s.min_ev * 100).toFixed(2), 'type="number" min="-50" max="100" step="0.5" required', "Ожидаемая доходность ставки")}
      ${f("s-minodds", "Мин. кэф", s.min_odds, 'type="number" min="1.01" step="0.01" required')}
      ${f("s-maxodds", "Макс. кэф", s.max_odds, 'type="number" min="1.02" step="0.01" required')}
      <div class="field"><label for="s-src">Кэф для расчёта EV</label><select id="s-src">${Object.entries(d.price_sources).map(([k, v]) => `<option value="${k}" ${k === s.price_source ? "selected" : ""}>${esc(v)}</option>`).join("")}</select>
        <div class="hint">«Лучший» — максимум по рынку, у твоей конторы может быть ниже</div></div>
      ${d.static ? "" : f("s-auto", "Автообновление, часов", s.auto_update_hours, 'type="number" min="0" max="168" step="1" required', "0 — только вручную")}
    </div>
    <div class="panel-body"><div class="checks">${Object.entries(d.markets).map(([k, v]) => `<label><input type="checkbox" name="mk" value="${k}" ${s.markets.includes(k) ? "checked" : ""}> ${esc(v)}</label>`).join("")}</div></div></div>
    <div class="panel"><h2>Лиги</h2><div class="panel-body"><div class="checks">
      ${Object.entries(d.leagues).map(([k, v]) => `<label><input type="checkbox" name="lg" value="${k}" ${s.leagues.includes(k) ? "checked" : ""}> ${esc(v)}</label>`).join("")}
    </div><p class="muted small">${d.static ? "Отмеченные лиги показываются в сигналах и матчах." : "Новые лиги скачаются и обучатся автоматически после сохранения (1–2 минуты на лигу)."}</p></div></div>
    <div><button class="btn primary" type="submit">Сохранить</button></div>
  </form>
  ${d.static ? backupPanel(d) : ""}`;
  if (d.static) bindBackup();
  $("#settings-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const num = (id) => parseFloat($(id).value.replace(",", "."));
    const body = {
      bankroll: num("#s-bank"), currency: $("#s-cur").value.trim(), kelly_fraction: num("#s-kelly"),
      max_stake_pct: num("#s-cap") / 100, min_ev: num("#s-ev") / 100, min_odds: num("#s-minodds"),
      max_odds: num("#s-maxodds"), price_source: $("#s-src").value, auto_update_hours: $("#s-auto") ? num("#s-auto") : 0,
      markets: $$('input[name="mk"]:checked').map((x) => x.value),
      leagues: $$('input[name="lg"]:checked').map((x) => x.value),
    };
    try {
      const r = await api("/api/settings", { method: "PUT", body: JSON.stringify(body) });
      toast(r.update_started ? "Сохранено, загружаю новые лиги…" : "Сохранено");
      if (r.update_started) state.wasRunning = true;
      refreshStatus();
    } catch (err) {
      toast("Ошибка: " + err.message);
    }
  });
}

function backupPanel(d) {
  const run = d.repo_url ? `${esc(d.repo_url)}/actions/workflows/football-bot.yml` : null;
  return `<div class="panel" style="margin-top:16px"><h2>Данные на этом телефоне</h2><div class="panel-body stack">
    <p>Ставки и настройки хранятся только в этом браузере. Сохраняй резервную копию: если очистить данные браузера или сменить телефон, без неё они пропадут.</p>
    <div class="toolbar">
      <button type="button" class="btn" id="bk-export">Сохранить копию</button>
      <label class="btn">Восстановить из файла<input type="file" id="bk-import" accept="application/json,.json" hidden></label>
      <button type="button" class="btn danger" id="bk-reset">Удалить все ставки</button>
    </div>
    <p class="muted small">Прогнозы пересчитываются на GitHub автоматически каждые ${d.update_hours} ч.${run ? ` Пересчитать сейчас: <a href="${run}" target="_blank" rel="noopener">GitHub Actions → Run workflow</a>.` : ""}</p>
  </div></div>`;
}

function bindBackup() {
  $("#bk-export").addEventListener("click", async () => {
    const data = await api("/api/backup");
    const name = `value-bot-${new Date().toISOString().slice(0, 10)}.json`;
    const file = new File([JSON.stringify(data)], name, { type: "application/json" });
    if (navigator.canShare && navigator.canShare({ files: [file] })) {
      try { await navigator.share({ files: [file], title: "Value Bot — резервная копия" }); return; } catch (e) { if (e.name === "AbortError") return; }
    }
    const a = document.createElement("a");
    a.href = URL.createObjectURL(file);
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 10000);
  });
  $("#bk-import").addEventListener("change", async (e) => {
    const f = e.target.files[0];
    e.target.value = "";
    if (!f) return;
    try {
      const data = JSON.parse(await f.text());
      const n = Array.isArray(data.bets) ? data.bets.length : 0;
      if (!confirm(`Заменить текущие ставки на ${n} из файла?`)) return;
      const r = await api("/api/backup", { method: "POST", body: JSON.stringify(data) });
      toast(`Восстановлено ставок: ${r.bets}`);
      refreshStatus();
      render();
    } catch (err) {
      toast("Ошибка: " + err.message);
    }
  });
  $("#bk-reset").addEventListener("click", async () => {
    if (!confirm("Удалить все ставки с этого телефона? Это нельзя отменить.")) return;
    await api("/api/backup", { method: "POST", body: JSON.stringify({ app: "fbot", version: 1, bets: [] }) });
    toast("Ставки удалены");
    refreshStatus();
  });
}

// ----------------------------------------------------------------- router ---
const RENDER = { signals: renderSignals, fixtures: renderFixtures, bets: renderBets, stats: renderStats, model: renderModel, settings: renderSettings };

async function render() {
  if (!RENDER[state.tab]) state.tab = "signals";
  $$("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === state.tab));
  try {
    await RENDER[state.tab]();
  } catch (e) {
    view.innerHTML = empty("Не удалось загрузить данные", `<p>${esc(e.message)}</p>`);
  }
}

$$("#tabs button").forEach((b) => b.addEventListener("click", () => { location.hash = b.dataset.tab; }));
window.addEventListener("hashchange", () => { state.tab = location.hash.slice(1); render(); });

(async () => {
  try { state.settings = (await api("/api/settings")).settings; } catch { /* server not ready */ }
  await refreshStatus();
  render();
})();

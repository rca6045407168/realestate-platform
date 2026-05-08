"""Self-contained HTML report.

Pyodide analog: instead of running Python in the browser, we ship a single
HTML file with the rankings baked in + a vanilla-JS underwriting
calculator. Drop it on a thumbdrive, email it, or open from disk — no
server, no install. The JS implements the same formulas as
`underwriting.py` so an investor can A/B properties live.
"""
from __future__ import annotations
import html
import json
from pathlib import Path
import pandas as pd

UNDERWRITING_JS = r"""
function amortizingPayment(principal, annualRate, termYears) {
  if (principal <= 0) return 0;
  const r = annualRate / 12, n = termYears * 12;
  if (r === 0) return principal / n;
  return principal * (r * Math.pow(1 + r, n)) / (Math.pow(1 + r, n) - 1);
}

function annualNoi(a) {
  const grossRent = a.monthlyRent * 12;
  const eff = grossRent * (1 - a.vacancy);
  const opex = eff * a.opexRatio + a.propTax * a.purchasePrice
             + a.insurance + a.hoaMonthly * 12;
  return eff - opex;
}

function endingLoanBalance(loan, rate, term, paid) {
  const r = rate / 12, n = term * 12;
  if (r === 0) return loan * (1 - paid / n);
  return loan * (Math.pow(1 + r, n) - Math.pow(1 + r, paid)) / (Math.pow(1 + r, n) - 1);
}

function underwrite(a) {
  const noi = annualNoi(a);
  const loan = a.purchasePrice * a.ltv;
  const monthlyPI = amortizingPayment(loan, a.mortgageRate, a.termYears);
  const debtService = monthlyPI * 12;
  const cashFlow = noi - debtService;
  const equity = a.purchasePrice * (1 - a.ltv) + a.purchasePrice * a.closingCostPct + a.rehabCost;
  const capRate = noi / a.purchasePrice;
  const cocr = cashFlow / equity;
  const dscr = noi / debtService;

  // BRRRR refi
  let brrrr = null;
  if (a.arv) {
    const newLoan = a.arv * a.refiLtv;
    const payoff = a.purchasePrice * a.ltv;
    const cashOut = newLoan - payoff;
    const equityLeft = Math.max((a.purchasePrice * (1 + a.closingCostPct) + a.rehabCost) - newLoan, 0);
    brrrr = { newLoan, cashOut, equityLeft, infiniteReturn: equityLeft <= 0 };
  }

  // 5-yr IRR
  let rent = a.monthlyRent, expenses = noi - cashFlow;  // initial opex including taxes/insurance
  expenses = a.opexRatio * (a.monthlyRent * 12 * (1 - a.vacancy))
           + a.propTax * a.purchasePrice + a.insurance + a.hoaMonthly * 12;
  const cf = [];
  for (let yr = 1; yr <= a.holdYears; yr++) {
    const eff = rent * 12 * (1 - a.vacancy);
    const noiYr = eff - expenses;
    cf.push(noiYr - debtService);
    rent *= 1 + a.rentGrowth;
    expenses *= 1 + a.expenseGrowth;
  }
  const terminalNoi = cf[cf.length - 1] / (1 - a.vacancy) * (1 + a.rentGrowth);  // approx
  const salePrice = (cf[cf.length - 1] + debtService) * (1 + a.rentGrowth) / a.exitCap;
  const balance = endingLoanBalance(loan, a.mortgageRate, a.termYears, a.holdYears * 12);
  const netSale = salePrice * (1 - a.sellingCostPct) - balance;
  cf[cf.length - 1] += netSale;

  // bisect IRR
  const npv = (rate) => -equity + cf.reduce((s, c, i) => s + c / Math.pow(1 + rate, i + 1), 0);
  let lo = -0.99, hi = 5.0;
  for (let i = 0; i < 200; i++) {
    const mid = (lo + hi) / 2;
    if (npv(mid) > 0) lo = mid; else hi = mid;
  }
  const irr = (lo + hi) / 2;
  const equityMultiple = cf.reduce((s, c) => s + c, 0) / equity;

  return {noi, capRate, cashFlow, dscr, cocr, equity, brrrr, irr, equityMultiple, salePrice, balance};
}
"""

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>reip — Real Estate Investment Platform</title>
<style>
  :root {
    --bg: #0d1117; --card: #161b22; --line: #30363d; --text: #c9d1d9;
    --muted: #8b949e; --accent: #58a6ff;
    --green: #3fb950; --yellow: #d29922; --red: #f85149; --cyan: #79c0ff;
    --magenta: #d2a8ff; --blue: #58a6ff;
  }
  body { background: var(--bg); color: var(--text); font: 14px/1.4 -apple-system, ui-monospace, monospace; margin: 0; padding: 24px; }
  h1 { color: var(--accent); margin: 0 0 4px; }
  h2 { color: var(--text); border-bottom: 1px solid var(--line); padding-bottom: 6px; margin-top: 32px; }
  .meta { color: var(--muted); margin-bottom: 20px; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }
  table { border-collapse: collapse; width: 100%; font-size: 13px; }
  th { text-align: left; color: var(--muted); padding: 6px 10px; border-bottom: 1px solid var(--line); }
  td { padding: 6px 10px; border-bottom: 1px solid var(--line); }
  tr:hover { background: var(--card); }
  .num { text-align: right; font-variant-numeric: tabular-nums; }
  .pos { color: var(--green); }
  .neg { color: var(--red); }
  .bar { display: inline-block; width: 80px; height: 8px; background: var(--line); border-radius: 4px; overflow: hidden; vertical-align: middle; }
  .bar > span { display: block; height: 100%; background: var(--accent); }
  .arch-coastal-gateway { color: var(--cyan); }
  .arch-sun-belt-growth { color: var(--green); }
  .arch-cashflow-heartland { color: var(--yellow); }
  .arch-boom-bust-beta { color: var(--magenta); }
  .arch-resource---niche { color: var(--blue); }
  .arch-mixed { color: var(--muted); }
  .calculator { background: var(--card); border: 1px solid var(--line); border-radius: 8px; padding: 20px; }
  .calculator label { display: block; margin: 8px 0; font-size: 12px; color: var(--muted); }
  .calculator input { width: 100%; padding: 6px 8px; background: var(--bg); border: 1px solid var(--line); border-radius: 4px; color: var(--text); font: 13px ui-monospace, monospace; }
  .results { margin-top: 16px; padding: 12px; background: var(--bg); border-radius: 4px; }
  .results .row { display: flex; justify-content: space-between; padding: 4px 0; border-bottom: 1px dotted var(--line); }
  .results .row:last-child { border-bottom: none; }
  .results .label { color: var(--muted); }
  .verdict { padding: 10px; margin: 10px 0; border-radius: 4px; font-weight: bold; }
  .verdict.good { background: rgba(63,185,80,0.15); color: var(--green); border: 1px solid var(--green); }
  .verdict.bad { background: rgba(248,81,73,0.15); color: var(--red); border: 1px solid var(--red); }
  .filters { margin: 12px 0; }
  .filters select, .filters input { background: var(--bg); border: 1px solid var(--line); color: var(--text); padding: 4px 8px; border-radius: 4px; font: 13px ui-monospace, monospace; }
  details > summary { cursor: pointer; padding: 8px 0; }
</style>
</head>
<body>
<h1>reip — Real Estate Investment Platform</h1>
<div class="meta">Generated <span id="ts">__TIMESTAMP__</span> · __NMSA__ MSAs scored · framework: <em>Allocating Capital Across U.S. Real Estate</em>, May 2026</div>

<h2>MSA rankings</h2>
<div class="filters">
  Sort by:
  <select id="sortby" onchange="renderTable()">
    <option value="total_return_score">Total Return</option>
    <option value="appreciation_score">Appreciation</option>
    <option value="cashflow_score">Cashflow</option>
  </select>
  Archetype:
  <select id="archetype" onchange="renderTable()">
    <option value="">All</option>
    <option>Coastal Gateway</option>
    <option>Sun Belt Growth</option>
    <option>Cashflow Heartland</option>
    <option>Boom-Bust Beta</option>
    <option>Resource & Niche</option>
    <option>Mixed</option>
  </select>
  Min population:
  <input id="minpop" type="number" value="250000" style="width: 100px;" oninput="renderTable()">
  Top N:
  <input id="topn" type="number" value="50" style="width: 60px;" oninput="renderTable()">
</div>
<div id="msa-table"></div>

<h2>Underwriting calculator</h2>
<div class="grid">
  <div class="calculator">
    <label>Purchase price <input id="price" type="number" value="200000"></label>
    <label>Rehab cost <input id="rehab" type="number" value="20000"></label>
    <label>ARV (after-repair value) <input id="arv" type="number" value="280000"></label>
    <label>Monthly rent <input id="rent" type="number" value="2200"></label>
    <label>Mortgage rate <input id="rate" type="number" step="0.001" value="0.07"></label>
    <label>LTV <input id="ltv" type="number" step="0.01" value="0.75"></label>
    <label>Vacancy rate <input id="vacancy" type="number" step="0.01" value="0.05"></label>
    <label>Operating expense ratio <input id="opex" type="number" step="0.01" value="0.40"></label>
    <label>Property tax rate (% of value) <input id="proptax" type="number" step="0.001" value="0.012"></label>
    <label>Insurance (annual) <input id="insurance" type="number" value="1500"></label>
    <label>Exit cap rate <input id="exitcap" type="number" step="0.001" value="0.06"></label>
    <label>Hold years <input id="hold" type="number" value="5"></label>
  </div>
  <div class="results" id="results"></div>
</div>

<details><summary>How scores are computed (Framework Table 5)</summary>
  <p style="color: var(--muted); max-width: 700px;">
    <strong>Appreciation score</strong> weights demand (40%: 5y pop CAGR, employment CAGR, income CAGR, net migration)
    plus supply (20%: permits per 1000 households, months of inventory, Saiz elasticity).
    <strong>Cashflow score</strong> weights yield (20%: gross rent yield, price-to-income inverted, DOM trend).
    Both penalize a 20% risk component (climate/insurance/regulatory friction/effective property tax).
    Robust z-scores (median, IQR). No trailing price appreciation as a feature.
  </p>
</details>

<script>__JS__</script>
<script>
const MSAS = __DATA__;
const HISTORY = __HISTORY__;
function spark(values) {
  if (!values || values.length < 2) return '';
  const w = 60, h = 18, pad = 2;
  const lo = Math.min(...values), hi = Math.max(...values);
  const span = hi - lo || 1;
  const pts = values.map((v, i) => {
    const x = pad + (i / (values.length - 1)) * (w - 2 * pad);
    const y = pad + (1 - (v - lo) / span) * (h - 2 * pad);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');
  const last = values[values.length - 1], first = values[0];
  const color = last >= first ? 'var(--green)' : 'var(--red)';
  return `<svg width="${w}" height="${h}" style="vertical-align:middle"><polyline points="${pts}" fill="none" stroke="${color}" stroke-width="1.5"/></svg>`;
}
function classToken(s) { return 'arch-' + (s||'').toLowerCase().replace(/[^a-z0-9]+/g, '-'); }
function fmtPct(x, d=2) { return (x===null||x===undefined||isNaN(x)) ? '—' : (x*100).toFixed(d) + '%'; }
function fmtNum(x, d=0) { return (x===null||x===undefined||isNaN(x)) ? '—' : x.toLocaleString(undefined, {maximumFractionDigits: d}); }
function score(x) {
  if (x===null||x===undefined||isNaN(x)) return '—';
  const cls = x>0.05 ? 'pos' : x<-0.05 ? 'neg' : '';
  return `<span class="${cls}">${(x>=0?'+':'')+x.toFixed(3)}</span>`;
}
function renderTable() {
  const sortby = document.getElementById('sortby').value;
  const arch = document.getElementById('archetype').value;
  const minpop = +document.getElementById('minpop').value;
  const n = +document.getElementById('topn').value;
  let rows = MSAS.filter(r => (arch==='' || r.archetype===arch) && r.pop >= minpop);
  rows.sort((a, b) => (b[sortby]||-99) - (a[sortby]||-99));
  rows = rows.slice(0, n);
  let html = '<table><thead><tr><th>CBSA</th><th>MSA</th><th>Archetype</th><th class="num">Pop</th><th class="num">PopΔ5y</th><th class="num">Mig%</th><th class="num">Yield</th><th class="num">Permits/1k</th><th>ZHVI 12m</th><th class="num">Appr</th><th class="num">Cash</th><th class="num">Total</th><th>Cmp</th></tr></thead><tbody>';
  for (const r of rows) {
    html += `<tr>
      <td>${r.cbsa_code}</td>
      <td>${r.cbsa_name||''}</td>
      <td class="${classToken(r.archetype)}">${r.archetype||''}</td>
      <td class="num">${fmtNum(r.pop)}</td>
      <td class="num">${fmtPct(r.pop_cagr_5yr)}</td>
      <td class="num">${fmtPct(r.net_migration_pct_pop)}</td>
      <td class="num">${fmtPct(r.gross_yield)}</td>
      <td class="num">${fmtNum(r.permits_per_1000_hh, 1)}</td>
      <td>${spark(HISTORY[r.cbsa_code])}</td>
      <td class="num">${score(r.appreciation_score)}</td>
      <td class="num">${score(r.cashflow_score)}</td>
      <td class="num">${score(r.total_return_score)}</td>
      <td><span class="bar"><span style="width:${(r.completeness||0)*100}%"></span></span></td>
    </tr>`;
  }
  html += '</tbody></table>';
  document.getElementById('msa-table').innerHTML = html;
}
function recalc() {
  const a = {
    purchasePrice: +document.getElementById('price').value,
    rehabCost: +document.getElementById('rehab').value,
    arv: +document.getElementById('arv').value || null,
    monthlyRent: +document.getElementById('rent').value,
    mortgageRate: +document.getElementById('rate').value,
    ltv: +document.getElementById('ltv').value,
    termYears: 30,
    closingCostPct: 0.03,
    refiLtv: 0.75,
    vacancy: +document.getElementById('vacancy').value,
    opexRatio: +document.getElementById('opex').value,
    propTax: +document.getElementById('proptax').value,
    insurance: +document.getElementById('insurance').value,
    hoaMonthly: 0,
    rentGrowth: 0.03,
    expenseGrowth: 0.03,
    exitCap: +document.getElementById('exitcap').value,
    sellingCostPct: 0.07,
    holdYears: +document.getElementById('hold').value,
  };
  const r = underwrite(a);
  const dscrOK = r.dscr >= 1.20;
  const irrOK = r.irr >= 0.10;
  const verdict = (dscrOK && irrOK) ? `<div class="verdict good">DEAL — DSCR ${r.dscr.toFixed(2)} · IRR ${(r.irr*100).toFixed(1)}%</div>`
                                    : `<div class="verdict bad">PASS — DSCR ${r.dscr.toFixed(2)} · IRR ${(r.irr*100).toFixed(1)}%</div>`;
  let html = verdict + `<div class="row"><span class="label">Year-1 NOI</span><span>$${fmtNum(r.noi, 0)}</span></div>`;
  html += `<div class="row"><span class="label">Cap rate</span><span>${fmtPct(r.capRate)}</span></div>`;
  html += `<div class="row"><span class="label">DSCR</span><span class="${dscrOK?'pos':'neg'}">${r.dscr.toFixed(2)}</span></div>`;
  html += `<div class="row"><span class="label">Year-1 cash flow</span><span class="${r.cashFlow>0?'pos':'neg'}">$${fmtNum(r.cashFlow, 0)}</span></div>`;
  html += `<div class="row"><span class="label">Cash-on-cash</span><span class="${r.cocr>0?'pos':'neg'}">${fmtPct(r.cocr)}</span></div>`;
  html += `<div class="row"><span class="label">Equity invested</span><span>$${fmtNum(r.equity, 0)}</span></div>`;
  html += `<div class="row"><span class="label">5-yr IRR</span><span class="${irrOK?'pos':'neg'}">${fmtPct(r.irr, 1)}</span></div>`;
  html += `<div class="row"><span class="label">Equity multiple</span><span>${r.equityMultiple.toFixed(2)}×</span></div>`;
  if (r.brrrr) {
    html += `<div class="row" style="margin-top:8px;"><span class="label"><strong>BRRRR refi</strong></span><span></span></div>`;
    html += `<div class="row"><span class="label">New loan</span><span>$${fmtNum(r.brrrr.newLoan, 0)}</span></div>`;
    html += `<div class="row"><span class="label">Cash out</span><span class="${r.brrrr.cashOut>0?'pos':'neg'}">$${fmtNum(r.brrrr.cashOut, 0)}</span></div>`;
    html += `<div class="row"><span class="label">Equity left in</span><span>$${fmtNum(r.brrrr.equityLeft, 0)}</span></div>`;
    if (r.brrrr.infiniteReturn) html += `<div class="verdict good">∞ return — equity fully recovered at refi</div>`;
  }
  document.getElementById('results').innerHTML = html;
}
document.querySelectorAll('.calculator input').forEach(el => el.addEventListener('input', recalc));
renderTable();
recalc();
</script>
</body>
</html>"""


ZHVI_HISTORY_SQL = """
WITH zip_to_cbsa AS (
    SELECT z.zip, c.cbsa_code
    FROM zip_county_xwalk z JOIN county_cbsa_xwalk c USING (fips_county)
),
zhvi_recent AS (
    SELECT zip, period, value
    FROM zillow_zhvi
    WHERE period >= (CURRENT_DATE - INTERVAL '13 months')
)
SELECT zc.cbsa_code, zr.period, MEDIAN(zr.value) AS zhvi_med
FROM zhvi_recent zr JOIN zip_to_cbsa zc USING (zip)
GROUP BY zc.cbsa_code, zr.period
ORDER BY zc.cbsa_code, zr.period
"""


def _zhvi_history(con) -> dict[str, list[float]]:
    if con is None:
        return {}
    rows = con.execute(ZHVI_HISTORY_SQL).fetchall()
    history: dict[str, list[float]] = {}
    for cbsa_code, _period, val in rows:
        history.setdefault(cbsa_code, []).append(float(val) if val is not None else None)
    # Keep only the trailing 12 points so the JSON payload stays small
    return {k: v[-12:] for k, v in history.items() if len(v) >= 6}


def build(scored: pd.DataFrame, out_path: Path | str = "data/reip-report.html",
          con=None) -> Path:
    """Generate a single-file HTML report."""
    cols = [
        "cbsa_code", "cbsa_name", "archetype", "pop", "pop_cagr_5yr",
        "net_migration_pct_pop", "gross_yield", "permits_per_1000_hh",
        "appreciation_score", "cashflow_score", "total_return_score",
        "completeness",
    ]
    have = scored[[c for c in cols if c in scored.columns]].copy()
    history = _zhvi_history(con)
    # JSON-friendly: replace NaN with None
    data = json.loads(have.to_json(orient="records"))
    page = (
        PAGE.replace("__DATA__", json.dumps(data))
            .replace("__HISTORY__", json.dumps(history))
            .replace("__JS__", UNDERWRITING_JS)
            .replace("__NMSA__", str(len(have)))
            .replace("__TIMESTAMP__", html.escape(pd.Timestamp.now().strftime("%Y-%m-%d %H:%M")))
    )
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(page)
    return p

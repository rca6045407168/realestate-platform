// reip SPA — vanilla JS, no build step.
//
// Four screens: MSA dashboard, MSA detail, underwriting workspace, AVM,
// remarks parser. UX discipline matches the spec:
//   - single Green/Yellow/Red verdict front-and-center
//   - 3-4 reasons in plain English
//   - one primary action
//   - 'show me the math' panel hidden behind a toggle

// ---- helpers --------------------------------------------------------------

const API = '/api';
const $ = (id) => document.getElementById(id);
const fmtPct = (v, d=2) => v == null || isNaN(v) ? '—' : (v*100).toFixed(d) + '%';
const fmtNum = (v, d=0) => v == null || isNaN(v) ? '—' : Number(v).toLocaleString(undefined, { maximumFractionDigits: d });
const fmtMoney = (v) => v == null || isNaN(v) ? '—' : '$' + fmtNum(v, 0);
const archCls = (a) => 'arch-' + (a || '').replace(/[^A-Za-z0-9]+/g, '-');
const scoreClass = (v) => v == null ? 'text-muted' : v > 0.05 ? 'text-green' : v < -0.05 ? 'text-red' : 'text-muted';
const scoreFmt = (v) => v == null ? '—' : (v > 0 ? '+' : '') + Number(v).toFixed(3);

async function api(path, opts={}) {
  const r = await fetch(API + path, { headers: { 'Content-Type': 'application/json' }, ...opts });
  if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
  return r.json();
}

// ---- routing --------------------------------------------------------------

function go(name) {
  document.querySelectorAll('.screen').forEach(s => s.classList.add('hidden'));
  $('screen-' + name).classList.remove('hidden');
  document.querySelectorAll('.navlink').forEach(b => b.classList.remove('border-accent'));
  // (don't bother with active style — hover is enough)
  if (name === 'dashboard') loadDashboard();
  if (name === 'avm') loadAvm();
}
window.go = go;

// ---- DASHBOARD -----------------------------------------------------------

let MSAS = [];
async function loadDashboard() {
  const sortBy = $('sortBy').value;
  const archetype = $('archetypeFilter').value;
  const minPop = +$('minPop').value || 0;
  const limit = +$('limit').value || 50;
  const qs = new URLSearchParams({ sort_by: sortBy, min_pop: minPop, limit });
  if (archetype) qs.set('archetype', archetype);
  $('msaTableHost').innerHTML = '<div class="p-6 text-muted text-sm">Loading…</div>';
  try {
    MSAS = await api('/msas?' + qs);
  } catch (e) {
    $('msaTableHost').innerHTML = `<div class="p-6 text-red">Error: ${e.message}</div>`;
    return;
  }
  if (!MSAS.length) {
    $('msaTableHost').innerHTML = '<div class="p-6 text-muted">No MSAs scored. Run <code>reip ingest</code> first.</div>';
    return;
  }
  let html = '<table class="tight w-full text-sm"><thead><tr>'
    + '<th>CBSA</th><th>MSA</th><th>Archetype</th>'
    + '<th class="text-right">Pop</th>'
    + '<th class="text-right">PopΔ5y</th>'
    + '<th class="text-right">Mig%</th>'
    + '<th class="text-right">Yield</th>'
    + '<th class="text-right">Permits/1k</th>'
    + '<th class="text-right">Appr</th>'
    + '<th class="text-right">Cash</th>'
    + '<th class="text-right">Total</th>'
    + '<th>Cmp</th>'
    + '</tr></thead><tbody>';
  for (const m of MSAS) {
    html += `<tr onclick="openMsa('${m.cbsa_code}')">
      <td class="text-muted">${m.cbsa_code}</td>
      <td>${m.cbsa_name || ''}</td>
      <td class="${archCls(m.archetype)}">${m.archetype || ''}</td>
      <td class="text-right num">${fmtNum(m.pop)}</td>
      <td class="text-right num">${fmtPct(m.pop_cagr_5yr)}</td>
      <td class="text-right num">${fmtPct(m.net_migration_pct_pop)}</td>
      <td class="text-right num">${fmtPct(m.gross_yield)}</td>
      <td class="text-right num">${fmtNum(m.permits_per_1000_hh, 1)}</td>
      <td class="text-right num ${scoreClass(m.appreciation_score)}">${scoreFmt(m.appreciation_score)}</td>
      <td class="text-right num ${scoreClass(m.cashflow_score)}">${scoreFmt(m.cashflow_score)}</td>
      <td class="text-right num ${scoreClass(m.total_return_score)}">${scoreFmt(m.total_return_score)}</td>
      <td><div class="w-20 h-2 bg-line rounded overflow-hidden"><div class="h-full bg-accent" style="width:${(m.completeness||0)*100}%"></div></div></td>
    </tr>`;
  }
  html += '</tbody></table>';
  $('msaTableHost').innerHTML = html;
}

// Wire dashboard filter changes
['sortBy', 'archetypeFilter', 'minPop', 'limit'].forEach(id => {
  document.addEventListener('DOMContentLoaded', () => $(id).addEventListener('input', loadDashboard));
});

// ---- MSA DETAIL ----------------------------------------------------------

async function openMsa(cbsa_code) {
  go('msa');
  $('msaDetailHost').innerHTML = '<div class="p-6 text-muted">Loading…</div>';
  let m;
  try { m = await api('/msas/' + cbsa_code); }
  catch (e) { $('msaDetailHost').innerHTML = `<div class="text-red">${e.message}</div>`; return; }
  const pct = (v) => v == null ? '—' : (v*100).toFixed(0) + '%';
  $('msaDetailHost').innerHTML = `
    <div class="flex items-baseline gap-3 mb-2">
      <h2 class="text-3xl font-semibold">${m.cbsa_name}</h2>
      <div class="text-muted">CBSA ${m.cbsa_code}</div>
      <div class="${archCls(m.archetype)} font-medium">${m.archetype}</div>
    </div>
    <div class="grid md:grid-cols-3 gap-4 mb-6">
      ${[['Population', fmtNum(m.pop)],
         ['5y pop CAGR', fmtPct(m.pop_cagr_5yr)],
         ['5y emp CAGR', fmtPct(m.emp_cagr_5yr)],
         ['5y income CAGR', fmtPct(m.income_cagr_5yr)],
         ['Net migration % pop', fmtPct(m.net_migration_pct_pop)],
         ['Permits / 1k HH', fmtNum(m.permits_per_1000_hh, 1)],
         ['Gross rent yield', fmtPct(m.gross_yield)],
         ['Saiz elasticity', m.elasticity?.toFixed?.(2) || '—'],
         ['Wharton WRLURI', m.wrluri?.toFixed?.(2) || '—'],
      ].map(([k, v]) => `<div class="bg-card rounded border border-line p-3">
        <div class="text-xs text-muted">${k}</div><div class="num text-lg">${v}</div>
      </div>`).join('')}
    </div>
    <div class="grid md:grid-cols-3 gap-4 mb-6">
      <div class="bg-card rounded border border-line p-4">
        <div class="text-xs uppercase text-muted mb-1">Appreciation</div>
        <div class="text-2xl num ${scoreClass(m.appreciation_score)}">${scoreFmt(m.appreciation_score)}</div>
        <div class="text-xs text-muted mt-1">${pct(m.appreciation_pct)} percentile</div>
      </div>
      <div class="bg-card rounded border border-line p-4">
        <div class="text-xs uppercase text-muted mb-1">Cashflow</div>
        <div class="text-2xl num ${scoreClass(m.cashflow_score)}">${scoreFmt(m.cashflow_score)}</div>
        <div class="text-xs text-muted mt-1">${pct(m.cashflow_pct)} percentile</div>
      </div>
      <div class="bg-card rounded border border-line p-4">
        <div class="text-xs uppercase text-muted mb-1">Total return (blend)</div>
        <div class="text-2xl num ${scoreClass(m.total_return_score)}">${scoreFmt(m.total_return_score)}</div>
        <div class="text-xs text-muted mt-1">${pct(m.total_return_pct)} percentile</div>
      </div>
    </div>
    <button onclick="prefillUnderwriteFor('${m.cbsa_name?.replace(/'/g, "")}')" class="px-4 py-2 rounded bg-accent text-bg font-medium">
      Underwrite a property in ${m.cbsa_name?.split(',')[0]}
    </button>
  `;
}
window.openMsa = openMsa;

// ---- UNDERWRITE ----------------------------------------------------------

const UW_FIELDS = [
  ['purchase_price', 'Purchase price', 200000, '$'],
  ['rehab_cost',     'Rehab cost',      20000, '$'],
  ['arv',            'ARV (after-repair)', 280000, '$'],
  ['monthly_rent',   'Monthly rent',     2200, '$'],
  ['mortgage_rate',  'Mortgage rate',    0.07, ''],
  ['ltv',            'LTV',              0.75, ''],
  ['vacancy',        'Vacancy',          0.05, ''],
  ['opex_ratio',     'Op-ex ratio',      0.40, ''],
  ['property_tax_rate', 'Property tax %',0.012, ''],
  ['insurance_annual','Insurance annual', 1500, '$'],
  ['exit_cap',       'Exit cap rate',    0.06, ''],
  ['hold_years',     'Hold years',       5,    ''],
  // Rec-gate inputs
  ['climate_pct',    'Climate risk pct (0–1)', 0.30, '*'],
  ['insurance_trend_pct', 'Insurance trend % (3y)', 0.10, '*'],
  ['alpha_stack_count', 'Alpha-stack flag count', 2, '*'],
  ['msa_blended_percentile', 'MSA blended pct (0–1)', 0.50, '*'],
];

function renderUwForm() {
  $('uwForm').innerHTML = UW_FIELDS.map(([id, label, def, unit]) => `
    <label class="block">
      <span class="text-xs text-muted">${label}${unit==='*' ? ' <span class="text-yellow">(rec gate)</span>' : ''}</span>
      <input id="uw_${id}" type="number" step="0.001" value="${def}" class="mt-0.5 w-full bg-bg border border-line rounded px-2 py-1.5 num" />
    </label>
  `).join('');
  $('uwForm').innerHTML += `
    <div class="pt-2 grid grid-cols-3 gap-2 text-xs">
      <label class="flex items-center gap-2"><input id="uw_rehab_overrun_risk" type="checkbox"> rehab overrun risk</label>
      <label class="flex items-center gap-2"><input id="uw_financing_concentration_risk" type="checkbox" checked> financing risk</label>
      <label class="flex items-center gap-2"><input id="uw_exit_risk_no_ltr_fallback" type="checkbox"> exit risk</label>
    </div>
  `;
}

function prefillUnderwriteFor(name) {
  go('underwrite');
  // Could prefill defaults by archetype later
}
window.prefillUnderwriteFor = prefillUnderwriteFor;

let LAST_DEAL = null;

async function runUnderwrite() {
  const body = {};
  for (const [id] of UW_FIELDS) body[id] = +$('uw_' + id).value;
  body.rehab_overrun_risk = $('uw_rehab_overrun_risk').checked;
  body.financing_concentration_risk = $('uw_financing_concentration_risk').checked;
  body.exit_risk_no_ltr_fallback = $('uw_exit_risk_no_ltr_fallback').checked;
  $('uwResultHost').innerHTML = '<div class="text-muted">Underwriting…</div>';
  let r;
  try { r = await api('/underwritings', { method: 'POST', body: JSON.stringify(body) }); }
  catch (e) { $('uwResultHost').innerHTML = `<div class="text-red">${e.message}</div>`; return; }
  LAST_DEAL = r.deal_inputs;
  renderUwResult(r);
}

function renderVerdict(rec) {
  return `
    <div class="verdict-${rec.verdict} rounded-lg p-4">
      <div class="flex items-center justify-between">
        <div>
          <div class="text-xs uppercase tracking-wide opacity-80">Recommendation</div>
          <div class="text-3xl font-bold">${rec.verdict}</div>
        </div>
        <div class="text-right text-xs opacity-80">
          ${rec.failures.length ? `${rec.failures.length} failure(s)` : 'all thresholds clear'}<br>
          ${rec.required_mitigations.length} mitigation(s) needed
        </div>
      </div>
      ${rec.primary_action ? `<div class="mt-3 text-sm font-medium">→ ${rec.primary_action}</div>` : ''}
    </div>
  `;
}

function renderReasons(reasons) {
  if (!reasons || !reasons.length) return '';
  return `<div class="bg-card rounded border border-line p-4">
    <div class="text-xs uppercase tracking-wide text-muted mb-2">Why</div>
    <ul class="text-sm space-y-1.5 list-disc list-inside">${reasons.map(r => `<li>${r}</li>`).join('')}</ul>
  </div>`;
}

const MITIGATION_LABELS = {
  verified_70pct_ltv_term_sheet: 'Verified 70% LTV DSCR-loan term sheet',
  documented_capital_reserve_min_25k: 'Documented ≥$25K capital reserve',
  signed_contractor_bid: 'Signed contractor bid (vs. heuristic estimate)',
  committed_hard_money_primary: 'Committed hard-money primary line',
  committed_hard_money_backup: 'Committed hard-money backup line',
  ltr_fallback_pm_identified: 'LTR-fallback property manager identified',
};

function renderMitigationsPanel(rec) {
  const all = Object.keys(MITIGATION_LABELS);
  return `<div class="bg-card rounded border border-line p-4">
    <div class="text-xs uppercase tracking-wide text-muted mb-2">Mitigations — toggle to upgrade verdict</div>
    <div class="space-y-1.5">
      ${all.map(name => {
        const verified = rec.verified_mitigations.includes(name);
        const required = rec.required_mitigations.includes(name);
        return `<label class="flex items-center gap-2 text-sm">
          <input type="checkbox" data-mit="${name}" ${verified ? 'checked' : ''}
            onchange="reapplyMitigations()">
          <span class="${required ? 'text-yellow' : verified ? 'text-green' : 'text-muted'}">
            ${MITIGATION_LABELS[name]}${required ? ' — needed' : verified ? ' — verified' : ''}
          </span>
        </label>`;
      }).join('')}
    </div>
  </div>`;
}

function renderProforma(pf, brrrr, irr) {
  const row = (k, v) => `<div class="flex justify-between border-b border-line border-dotted py-1"><span class="text-muted">${k}</span><span class="num">${v}</span></div>`;
  let html = `<div class="bg-card rounded border border-line p-4">
    <div class="text-xs uppercase tracking-wide text-muted mb-2">Pro forma + IRR</div>
    ${row('Year-1 NOI', fmtMoney(pf.noi))}
    ${row('Cap rate', fmtPct(pf.cap_rate, 2))}
    ${row('DSCR', pf.dscr.toFixed(2) + '×')}
    ${row('Year-1 cash flow', fmtMoney(pf.cash_flow_y1))}
    ${row('Cash-on-cash', fmtPct(pf.cash_on_cash, 2))}
    ${row('Equity invested', fmtMoney(pf.equity_invested))}
    ${row('5-yr IRR', fmtPct(irr.irr, 1))}
    ${row('Equity multiple', irr.equity_multiple.toFixed(2) + '×')}`;
  if (brrrr.applicable) {
    html += `<div class="mt-2 pt-2 border-t border-line text-xs uppercase tracking-wide text-muted">BRRRR refi</div>
      ${row('Cash out at refi', fmtMoney(brrrr.cash_out_at_refi))}
      ${row('Equity left in', fmtMoney(brrrr.equity_left_in_after_refi))}`;
    if (brrrr.infinite_return) html += `<div class="mt-2 verdict-GREEN rounded p-2 text-sm">∞ return — equity fully recovered at refi</div>`;
  }
  html += `</div>`;
  return html;
}

function renderUwResult(r) {
  $('uwResultHost').innerHTML = renderVerdict(r.recommendation)
    + renderReasons(r.recommendation.reasons)
    + renderMitigationsPanel(r.recommendation)
    + renderProforma(r.proforma, r.brrrr_refi, r.irr)
    + `<details class="bg-card rounded border border-line">
        <summary class="px-4 py-3 cursor-pointer text-sm text-muted">Show me the math — sensitivity grid (rent×vacancy×exit cap)</summary>
        <div class="px-4 pb-4 max-h-72 overflow-auto">
          <table class="tight w-full text-xs"><thead><tr><th>rent</th><th>vac</th><th>exit cap</th><th class="text-right">IRR</th><th class="text-right">eq mult</th></tr></thead><tbody>
            ${r.sensitivity.map(s => `<tr><td class="num">${fmtPct(s.rent_change)}</td><td class="num">${fmtPct(s.vacancy)}</td><td class="num">${fmtPct(s.exit_cap)}</td><td class="text-right num ${s.irr<0?'text-red':'text-green'}">${fmtPct(s.irr,1)}</td><td class="text-right num">${s.eq_mult.toFixed(2)}×</td></tr>`).join('')}
          </tbody></table>
        </div>
      </details>`;
}

async function reapplyMitigations() {
  if (!LAST_DEAL) return;
  const m = {};
  document.querySelectorAll('input[data-mit]').forEach(el => m[el.dataset.mit] = el.checked);
  // Apply mitigations on the original (un-mitigated) deal so toggling off restores RED.
  const out = await api('/underwritings/mitigations', {
    method: 'POST',
    body: JSON.stringify({ deal: LAST_DEAL, mitigations: m }),
  });
  // Update only the verdict + reasons + mitigation panel (don't refetch full underwriting)
  const host = $('uwResultHost');
  const sections = host.children;
  sections[0].outerHTML = renderVerdict(out);
  sections[1].outerHTML = renderReasons(out.reasons);
  sections[2].outerHTML = renderMitigationsPanel(out);
}
window.reapplyMitigations = reapplyMitigations;

// ---- AVM screen ----------------------------------------------------------

async function loadAvm() {
  const dir = $('avmDir').value;
  const min = +$('avmMin').value, max = +$('avmMax').value;
  $('avmTableHost').innerHTML = '<div class="p-6 text-muted">Loading…</div>';
  let rows;
  try { rows = await api(`/avm?direction=${dir}&min_price=${min}&max_price=${max}&limit=50`); }
  catch (e) { $('avmTableHost').innerHTML = `<div class="p-6 text-red">${e.message}</div>`; return; }
  if (!rows.length) { $('avmTableHost').innerHTML = '<div class="p-6 text-muted">No zips match.</div>'; return; }
  $('avmTableHost').innerHTML = `<table class="tight w-full text-sm"><thead><tr>
    <th>ZIP</th><th class="text-right">ZHVI</th><th class="text-right">Redfin sale (90d)</th>
    <th class="text-right">Divergence</th><th class="text-right">z-score</th><th>Direction</th>
    </tr></thead><tbody>${rows.map(r => `
      <tr>
        <td class="num">${r.zip}</td>
        <td class="text-right num">${fmtMoney(r.zhvi)}</td>
        <td class="text-right num">${fmtMoney(r.redfin_sale_90d)}</td>
        <td class="text-right num ${r.divergence_pct < 0 ? 'text-red' : 'text-green'}">${fmtPct(r.divergence_pct, 1)}</td>
        <td class="text-right num">${r.divergence_z?.toFixed(2)}</td>
        <td class="${r.direction==='hot'?'text-green':r.direction==='cold'?'text-red':'text-muted'}">${r.direction}</td>
      </tr>`).join('')}</tbody></table>`;
}
['avmDir', 'avmMin', 'avmMax'].forEach(id =>
  document.addEventListener('DOMContentLoaded', () => $(id).addEventListener('input', loadAvm))
);

// ---- Remarks parser ------------------------------------------------------

document.addEventListener('DOMContentLoaded', () => {
  $('remarksRun').addEventListener('click', async () => {
    const text = $('remarksText').value.trim();
    if (!text) return;
    let r;
    try { r = await api('/remarks', { method: 'POST', body: JSON.stringify({ text }) }); }
    catch (e) { $('remarksOut').innerHTML = `<div class="text-red">${e.message}</div>`; return; }
    const flags = ['motivated', 'distressed', 'use_change', 'assumable', 'price_cut', 'short_sale', 'probate'];
    $('remarksOut').innerHTML = `
      <div class="bg-card rounded border border-line p-4">
        <div class="text-xs uppercase tracking-wide text-muted mb-2">Alpha stack: ${flags.filter(f => r[f]).length}/7 flags</div>
        <div class="flex flex-wrap gap-2 mb-3">
          ${flags.map(f => `<span class="px-2 py-1 rounded text-xs ${r[f] ? 'verdict-GREEN' : 'bg-bg border border-line text-muted'}">${f}</span>`).join('')}
        </div>
        ${r.matched_terms.length ? `<div class="text-xs text-muted">matched: ${r.matched_terms.map(t => `<code class="px-1 bg-bg rounded">${t}</code>`).join(' · ')}</div>` : ''}
      </div>
    `;
  });
});

// ---- Listing ingestion -------------------------------------------------

async function ingestLink() {
  const url = $('linkInput').value.trim();
  if (!url) return;
  $('linkOut').innerHTML = '<span class="text-muted">Fetching…</span>';
  let p;
  try {
    p = await api('/properties/ingest', { method: 'POST', body: JSON.stringify({ url }) });
  } catch (e) {
    $('linkOut').innerHTML = `<span class="text-red">${e.message}</span>`;
    return;
  }
  // Prefill known fields
  const prefilled = [];
  function set(id, val) {
    if (val == null || val === '' || (typeof val === 'number' && isNaN(val))) return;
    const el = $('uw_' + id);
    if (!el) return;
    el.value = val;
    prefilled.push(id);
  }
  set('purchase_price', p.listed_price);
  set('arv',            p.listed_price);  // user can adjust upward
  set('monthly_rent',   p.rent_estimate);

  // Pretty status line
  const addr = [p.address, p.city, p.state, p.zip].filter(Boolean).join(', ');
  const fields = [];
  if (p.listed_price)  fields.push(`<b>$${fmtNum(p.listed_price)}</b>`);
  if (p.beds)          fields.push(`${p.beds} bd`);
  if (p.baths)         fields.push(`${p.baths} ba`);
  if (p.sqft)          fields.push(`${fmtNum(p.sqft)} sqft`);
  if (p.year_built)    fields.push(`built ${p.year_built}`);
  const rent = p.rent_estimate
    ? `· rent <b>$${fmtNum(p.rent_estimate)}</b>/mo (${p.rent_source || 'listing'})`
    : '· <span class="text-yellow">no rent estimate — enter manually</span>';
  const via = p.extracted_via && p.extracted_via.length ? ` · via ${p.extracted_via.join(' + ')}` : '';
  const warn = p.warnings && p.warnings.length
    ? `<div class="text-red mt-1">⚠ ${p.warnings.join(' · ')}</div>` : '';
  $('linkOut').innerHTML = `
    <div><span class="text-fg">${addr || '(address not extracted)'}</span></div>
    <div>${fields.join(' · ')} ${rent}${via}</div>
    <div class="text-green mt-1">Prefilled ${prefilled.length} field(s): ${prefilled.join(', ') || '(none)'} — review and run.</div>
    ${warn}
  `;
}

// Boot
document.addEventListener('DOMContentLoaded', () => {
  renderUwForm();
  $('uwSubmit').addEventListener('click', runUnderwrite);
  $('linkSubmit').addEventListener('click', ingestLink);
  $('linkInput').addEventListener('keydown', (e) => { if (e.key === 'Enter') ingestLink(); });
  go('dashboard');
});

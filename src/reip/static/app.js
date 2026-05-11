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

// Reusable spinner. `text` shows next to the spinner; `colSpan` is for table-cell embeds.
function spinnerHTML(text, opts = {}) {
  const cls = opts.colSpan ? `col-span-${opts.colSpan}` : '';
  return `<div class="spinner-row ${cls}"><div class="spinner"></div><div>${text || 'Loading…'}</div></div>`;
}

// ---- routing --------------------------------------------------------------

function go(name) {
  document.querySelectorAll('.screen').forEach(s => s.classList.add('hidden'));
  $('screen-' + name).classList.remove('hidden');
  document.querySelectorAll('.navlink').forEach(b => b.classList.remove('border-accent'));
  if (name === 'dashboard') loadDashboard();
  if (name === 'avm')       loadAvm();
  if (name === 'buy')       loadBuy();
  if (name === 'topzips')   loadTopZips();
  if (name === 'stress')    initStress();
  if (name === 'pipeline')  loadPipeline();
  if (name === 'portfolio') loadPortfolio();
  if (name === 'ask')       focusAskInput();
}
window.go = go;

// ---- Ask reip (chat) ----------------------------------------------------
//
// Conversations persist in localStorage as a list of {id, title, created_at,
// updated_at, messages: [{role, content, tool_calls?}]}. Single-user
// platform, no auth — localStorage is private to this browser.

const ASK_STORE_KEY = 'reip_ask_conversations_v1';
const ASK_CURRENT_KEY = 'reip_ask_current_id_v1';
const ASK_MAX_CONVS = 50;

let CONVERSATIONS = [];   // array of conversation objects
let CURRENT_ID = null;     // id of the open conversation

function loadConversations() {
  try {
    CONVERSATIONS = JSON.parse(localStorage.getItem(ASK_STORE_KEY) || '[]');
    if (!Array.isArray(CONVERSATIONS)) CONVERSATIONS = [];
  } catch (e) { CONVERSATIONS = []; }
  CURRENT_ID = localStorage.getItem(ASK_CURRENT_KEY) || null;
}

function persistConversations() {
  // LRU cap
  CONVERSATIONS.sort((a, b) => (b.updated_at || 0) - (a.updated_at || 0));
  if (CONVERSATIONS.length > ASK_MAX_CONVS) CONVERSATIONS.length = ASK_MAX_CONVS;
  localStorage.setItem(ASK_STORE_KEY, JSON.stringify(CONVERSATIONS));
  if (CURRENT_ID) localStorage.setItem(ASK_CURRENT_KEY, CURRENT_ID);
}

function currentConversation() {
  return CONVERSATIONS.find(c => c.id === CURRENT_ID) || null;
}

function newConversation() {
  const c = {
    id: 'c_' + Date.now().toString(36) + '_' + Math.random().toString(36).slice(2, 6),
    title: 'New chat',
    created_at: Date.now(),
    updated_at: Date.now(),
    messages: [],
  };
  CONVERSATIONS.unshift(c);
  CURRENT_ID = c.id;
  persistConversations();
  renderConversationList();
  renderConversationThread();
  focusAskInput();
}

function selectConversation(id) {
  CURRENT_ID = id;
  localStorage.setItem(ASK_CURRENT_KEY, id);
  renderConversationList();
  renderConversationThread();
}

function deleteConversation(id) {
  CONVERSATIONS = CONVERSATIONS.filter(c => c.id !== id);
  if (CURRENT_ID === id) {
    CURRENT_ID = CONVERSATIONS[0]?.id || null;
  }
  persistConversations();
  renderConversationList();
  renderConversationThread();
}

function clearAllConversations() {
  if (!confirm(`Delete all ${CONVERSATIONS.length} conversations? This can't be undone.`)) return;
  CONVERSATIONS = [];
  CURRENT_ID = null;
  localStorage.removeItem(ASK_STORE_KEY);
  localStorage.removeItem(ASK_CURRENT_KEY);
  renderConversationList();
  renderConversationThread();
}

function formatRelative(ts) {
  if (!ts) return '';
  const dt = Date.now() - ts;
  const m = Math.floor(dt / 60000);
  if (m < 1)   return 'just now';
  if (m < 60)  return m + 'm ago';
  const h = Math.floor(m / 60);
  if (h < 24)  return h + 'h ago';
  const d = Math.floor(h / 24);
  if (d < 7)   return d + 'd ago';
  return new Date(ts).toLocaleDateString();
}

function renderConversationList() {
  const host = document.getElementById('askConversations');
  if (!host) return;
  if (!CONVERSATIONS.length) {
    host.innerHTML = '<div class="text-xs text-muted px-2 py-3">No history yet. Start a chat below.</div>';
    return;
  }
  host.innerHTML = CONVERSATIONS.map(c => `
    <div class="conv-row ${c.id === CURRENT_ID ? 'active' : ''}" onclick="selectConversation('${c.id}')">
      <div class="conv-title" title="${escapeHtml(c.title)}">${escapeHtml(c.title)}</div>
      <div class="conv-date">${formatRelative(c.updated_at)}</div>
      <span class="conv-del" onclick="event.stopPropagation(); deleteConversation('${c.id}')" title="Delete">×</span>
    </div>
  `).join('');
}

function renderConversationThread() {
  const thread = document.getElementById('askThread');
  if (!thread) return;
  thread.innerHTML = '';
  const conv = currentConversation();
  if (!conv || !conv.messages.length) {
    thread.innerHTML = `<div class="text-xs text-muted text-center pt-12">
      Conversational research over the full REIP dataset. The system prompt is pre-loaded with current
      top markets and the 11 verified live-listing metros so most questions answer in one Claude call
      with zero tool use; specific lookups (one zip, one underwriting) trigger a tool call.<br><br>
      Start with one of the example prompts under the input, or ask anything.
    </div>`;
    return;
  }
  for (const m of conv.messages) {
    renderAskMessage(m.role, m.content, m.tool_calls || []);
  }
}

function focusAskInput() {
  const el = document.getElementById('askInput');
  if (el) setTimeout(() => el.focus(), 50);
}

// Expose for onclick handlers
window.selectConversation = selectConversation;
window.deleteConversation = deleteConversation;

function escapeHtml(s) {
  if (s == null) return '';
  return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function renderMarkdown(md) {
  // marked + DOMPurify load via CDN; if either is missing, fall back to
  // escaped plain text so the message still displays.
  if (typeof marked === 'undefined' || typeof DOMPurify === 'undefined') {
    return `<div class="whitespace-pre-wrap">${escapeHtml(md)}</div>`;
  }
  marked.setOptions({ breaks: true, gfm: true });
  return DOMPurify.sanitize(marked.parse(md));
}

function renderAskMessage(role, text, toolCalls) {
  const thread = document.getElementById('askThread');
  if (!thread) return;
  const div = document.createElement('div');
  if (role === 'user') {
    div.className = 'flex justify-end';
    div.innerHTML = `<div class="chat-bubble-user whitespace-pre-wrap">${escapeHtml(text)}</div>`;
  } else if (role === 'assistant') {
    div.className = 'flex flex-col gap-1';
    let toolsLine = '';
    if (toolCalls && toolCalls.length) {
      const pills = toolCalls.map(t => `<span class="tool-pill">${escapeHtml(t.name)}</span>`).join('');
      toolsLine = `<div class="chat-tools-line">used ${toolCalls.length} tool${toolCalls.length>1?'s':''}: ${pills}</div>`;
    }
    div.innerHTML = `${toolsLine}<div class="chat-bubble-assistant"><div class="markdown-body">${renderMarkdown(text)}</div></div>`;
  } else {
    div.className = 'flex justify-center';
    div.innerHTML = `<div class="text-xs text-yellow">${escapeHtml(text)}</div>`;
  }
  thread.appendChild(div);
  thread.scrollTop = thread.scrollHeight;
}

async function sendAsk() {
  const input = document.getElementById('askInput');
  const msg = input.value.trim();
  if (!msg) return;
  input.value = '';

  // Ensure a current conversation exists. If this is the first message,
  // also wipe the "start a chat" placeholder.
  let conv = currentConversation();
  if (!conv) {
    newConversation();
    conv = currentConversation();
  } else if (conv.messages.length === 0) {
    // Wipe the placeholder DOM before the first message renders
    document.getElementById('askThread').innerHTML = '';
  }

  // Auto-title from the first message (truncate at 60 chars on a word boundary)
  if (!conv.messages.length) {
    const t = msg.length > 60 ? msg.slice(0, 60).replace(/\s+\S*$/, '') + '…' : msg;
    conv.title = t;
  }

  conv.messages.push({ role: 'user', content: msg });
  conv.updated_at = Date.now();
  persistConversations();
  renderConversationList();
  renderAskMessage('user', msg);

  const thread = document.getElementById('askThread');
  const pending = document.createElement('div');
  pending.className = 'flex items-center gap-2 text-muted text-xs';
  pending.innerHTML = `<div class="spinner" style="width:14px;height:14px;border-width:2px;"></div>thinking…`;
  thread.appendChild(pending);
  thread.scrollTop = thread.scrollHeight;

  try {
    // Send all prior messages as history (not the one we just appended)
    const history = conv.messages.slice(0, -1).map(m => ({ role: m.role, content: m.content }));
    // Build a compact pipeline summary so chat is deal-aware. Keep the
    // payload small — just enough for the model to reference saved deals.
    const pipeline_summary = buildPipelineSummary();
    const r = await api('/chat', {
      method: 'POST',
      body: JSON.stringify({ message: msg, history, pipeline_summary }),
    });
    pending.remove();
    if (r.error) {
      renderAskMessage('system', r.error);
      return;
    }
    const reply = r.reply || '(empty reply)';
    const toolCalls = r.tool_calls || [];
    renderAskMessage('assistant', reply, toolCalls);
    conv.messages.push({ role: 'assistant', content: reply, tool_calls: toolCalls });
    conv.updated_at = Date.now();
    persistConversations();
    renderConversationList();
  } catch (e) {
    pending.remove();
    renderAskMessage('system', `Error: ${e.message}`);
  }
}

document.addEventListener('DOMContentLoaded', () => {
  loadConversations();
  renderConversationList();
  renderConversationThread();

  const send = document.getElementById('askSend');
  const inp  = document.getElementById('askInput');
  const newB = document.getElementById('askNewChat');
  const clrB = document.getElementById('askClearAll');

  if (send) send.addEventListener('click', sendAsk);
  if (inp)  inp.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendAsk(); }
  });
  if (newB) newB.addEventListener('click', newConversation);
  if (clrB) clrB.addEventListener('click', clearAllConversations);
});

// ---- DASHBOARD -----------------------------------------------------------

let MSAS = [];
async function loadDashboard() {
  const sortBy = $('sortBy').value;
  const archetype = $('archetypeFilter').value;
  const minPop = +$('minPop').value || 0;
  const limit = +$('limit').value || 50;
  const qs = new URLSearchParams({ sort_by: sortBy, min_pop: minPop, limit });
  if (archetype) qs.set('archetype', archetype);
  $('msaTableHost').innerHTML = spinnerHTML('Scoring MSAs…');
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
    html += `<tr onclick="openMsaListings('${m.cbsa_code}', '${(m.cbsa_name||'').replace(/'/g, "\\'")}')" title="Click to see live listings in this metro">
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

// ---- MSA → LIVE LISTINGS jump ------------------------------------------

async function openMsaListings(cbsa_code, cbsa_name) {
  // Smart routing: if the CBSA has a verified Redfin region_id, jump to
  // Buy ideas (live property cards). Otherwise, jump to Top zips (US)
  // pre-filtered to this CBSA — the user sees ranked zips in this metro
  // with deep-links to Redfin/Zillow for browsing actual listings.
  await loadMarkets();
  const sel = $('buyCbsa');
  const opt = Array.from(sel.options).find(o => o.value === cbsa_code);
  const isMapped = opt && opt.dataset.unmapped !== '1';

  if (isMapped) {
    document.querySelectorAll('.screen').forEach(s => s.classList.add('hidden'));
    $('screen-buy').classList.remove('hidden');
    $('buyHost').innerHTML = spinnerHTML(`Loading ${cbsa_name}…`, { colSpan: 3 });
    $('buyMeta').textContent = '';
    sel.value = cbsa_code;
    loadBuy();
    return;
  }

  // Unmapped: jump to Top zips with this CBSA filter
  document.querySelectorAll('.screen').forEach(s => s.classList.add('hidden'));
  $('screen-topzips').classList.remove('hidden');
  $('tzCbsa').value = cbsa_code;
  $('tzState').value = '';
  $('tzHost').innerHTML = spinnerHTML(`Loading top zips in ${cbsa_name}…`);
  $('tzMeta').textContent = `→ ${cbsa_name} — ranking every ZIP in this metro by 5y IRR`;
  loadTopZips();
}
window.openMsaListings = openMsaListings;

// ---- MSA DETAIL (legacy, kept for direct link access) -------------------

async function openMsa(cbsa_code) {
  go('msa');
  $('msaDetailHost').innerHTML = spinnerHTML('Loading MSA breakdown…');
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
  $('uwResultHost').innerHTML = spinnerHTML('Running pro forma + recommendation gate…');
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
  $('avmTableHost').innerHTML = spinnerHTML(`Computing AVM divergence (${dir} zips)…`);
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

// ---- Buy ideas screen ----------------------------------------------------

let MARKETS_LOADED = false;
async function loadMarkets() {
  if (MARKETS_LOADED) return;
  let markets;
  try {
    markets = await api('/listings/markets');
  } catch (e) {
    return;
  }
  // Group by archetype hint, alphabetize within group, render
  const groups = {};
  for (const m of markets) {
    const k = m.archetype_hint || 'Other';
    (groups[k] = groups[k] || []).push(m);
  }
  const order = ['Coastal Gateway', 'Sun Belt Growth', 'Cashflow Heartland',
                 'Boom-Bust Beta', 'Resource & Niche', 'Mixed', 'Other'];
  const sel = $('buyCbsa');
  sel.innerHTML = '';
  // Always-on: cross-market top picks
  const topOpt = document.createElement('option');
  topOpt.value = 'all';
  topOpt.textContent = '★ Best across all markets';
  sel.appendChild(topOpt);
  for (const archetype of order) {
    const group = groups[archetype];
    if (!group) continue;
    group.sort((a, b) => a.name.localeCompare(b.name));
    const og = document.createElement('optgroup');
    og.label = archetype;
    for (const m of group) {
      const o = document.createElement('option');
      o.value = m.cbsa_code;
      o.textContent = m.name;
      og.appendChild(o);
    }
    sel.appendChild(og);
  }
  // Default to Memphis (a working market with rich data) over the
  // 'all' option to avoid 30-60s blocking call on first paint.
  sel.value = '32820';
  if (sel.value !== '32820') sel.selectedIndex = 0;
  MARKETS_LOADED = true;
}

async function loadBuy() {
  await loadMarkets();
  const params = new URLSearchParams({
    cbsa: $('buyCbsa').value,
    sort: $('buySort').value,
    min_price: $('buyMin').value,
    max_price: $('buyMax').value,
    mortgage_rate: $('buyRate').value,
    limit: $('buyLimit').value || '12',
  });
  const zip = $('buyZip').value.trim();
  if (zip) params.set('zip', zip);
  for (const [id, key] of [
    ['buyMinIrr',     'min_irr'],
    ['buyMinDscr',    'min_dscr'],
    ['buyMinCap',     'min_cap'],
    ['buyMinSchools', 'min_school_count'],
  ]) {
    const v = $(id).value.trim();
    if (v !== '') params.set(key, v);
  }
  const isAll = $('buyCbsa').value === 'all';
  const msg = isAll
    ? 'Fanning out to all markets in parallel — first call takes ~30s, cached after.'
    : 'Pulling live listings + projecting 5y returns…';
  $('buyHost').innerHTML = spinnerHTML(msg, { colSpan: 3 });
  $('buyMeta').textContent = '';
  let r;
  try {
    r = await api('/listings/buy?' + params.toString());
  } catch (e) {
    $('buyHost').innerHTML = `<div class="col-span-3 p-6 text-red">${e.message}</div>`;
    return;
  }
  $('buyMeta').textContent = `${r.market || ''} · archetype ${r.archetype || '—'} · ${r.results.length} properties scored`;
  if (!r.results.length) {
    const sel = $('buyCbsa');
    const cur = sel.options[sel.selectedIndex];
    const isUnmapped = cur && cur.dataset.unmapped === '1';
    const reason = isUnmapped
      ? `<div class="text-yellow text-sm">This metro doesn't yet have a verified Redfin region_id, so the live-listings search returned nothing.</div>
         <div class="text-muted text-sm mt-2">Try <button onclick="document.getElementById('buyCbsa').value='all'; loadBuy();" class="text-accent underline">★ Best across all markets</button>, or paste a specific Redfin URL into the <button onclick="go('underwrite')" class="text-accent underline">Underwrite</button> tab.</div>`
      : `<div class="text-yellow text-sm">No listings matched your filters.</div>
         <div class="text-muted text-sm mt-2">Try widening price band or relaxing the IRR / DSCR thresholds.</div>`;
    $('buyHost').innerHTML = `<div class="col-span-3 p-6">${reason}${(r.warnings || []).length ? '<div class="text-xs text-muted mt-3">'+r.warnings.join('<br>')+'</div>' : ''}</div>`;
    return;
  }
  $('buyHost').innerHTML = r.results.map(renderBuyCard).join('');
}

function renderBuyCard(r) {
  const L = r.listing;
  const p = r.projection;
  const d = r.decision;
  const v = d.verdict;
  const verdictColor = v === 'GREEN' ? 'verdict-GREEN' : v === 'YELLOW' ? 'verdict-YELLOW' : 'verdict-RED';
  const avmTag = r.avm.direction === 'cold'
    ? `<span class="text-red text-xs">AVM cold ${r.avm.z?.toFixed(1)}σ</span>`
    : r.avm.direction === 'hot'
    ? `<span class="text-green text-xs">AVM hot +${r.avm.z?.toFixed(1)}σ</span>`
    : r.avm.direction === 'aligned'
    ? `<span class="text-muted text-xs">AVM aligned</span>`
    : '';

  return `<div class="bg-card rounded border border-line overflow-hidden flex flex-col">
    <div class="px-4 pt-4 flex items-start justify-between gap-2">
      <div class="min-w-0">
        <div class="text-base font-semibold truncate">${L.address || '—'}</div>
        <div class="text-xs text-muted truncate">${L.city || ''}, ${L.state || ''} ${L.zip || ''}</div>
        <div class="text-xs text-accent truncate">${L.cbsa_name}</div>
      </div>
      <div class="text-xs px-2 py-0.5 rounded ${verdictColor} whitespace-nowrap">${v}</div>
    </div>

    <div class="px-4 pt-3 grid grid-cols-2 gap-2 text-sm">
      <div><div class="text-xs text-muted">List price</div><div class="num text-lg">${fmtMoney(L.listed_price)}</div></div>
      <div><div class="text-xs text-muted">Bed/Bath/Sqft</div>
        <div class="num">${L.beds || '—'}/${L.baths || '—'}/${fmtNum(L.sqft, 0)}</div></div>
      <div><div class="text-xs text-muted">DOM</div><div class="num">${L.days_on_market ?? '—'}</div></div>
      <div><div class="text-xs text-muted">Built</div><div class="num">${L.year_built ?? '—'}</div></div>
    </div>

    <div class="px-4 mt-3 pt-3 border-t border-line grid grid-cols-3 gap-2 text-xs">
      <div>
        <div class="text-muted">5y rental profit</div>
        <div class="num text-base ${p.rental_profit_5y > 0 ? 'text-green' : 'text-red'}">${fmtMoney(p.rental_profit_5y)}</div>
      </div>
      <div>
        <div class="text-muted">5y appreciation</div>
        <div class="num text-base ${p.appreciation_5y_dollars > 0 ? 'text-green' : 'text-red'}">${fmtMoney(p.appreciation_5y_dollars)}</div>
        <div class="text-muted">${fmtPct(p.appreciation_cagr, 1)}/yr</div>
      </div>
      <div>
        <div class="text-muted">5y total / IRR</div>
        <div class="num text-base ${p.total_return_5y_dollars > 0 ? 'text-green' : 'text-red'}">${fmtMoney(p.total_return_5y_dollars)}</div>
        <div class="text-muted">${fmtPct(p.irr_5y, 1)} IRR</div>
      </div>
    </div>

    <div class="px-4 mt-2 text-xs flex items-center gap-2 text-muted flex-wrap">
      <span>cap ${fmtPct(p.cap_rate_y1, 1)}</span>·
      <span>DSCR ${p.dscr_y1.toFixed(2)}×</span>·
      <span>CoC ${fmtPct(p.cash_on_cash_y1, 1)}</span>·
      <span title="Vacancy source: ${p.vacancy_source || 'unknown'}">vac ${fmtPct(p.vacancy_used, 1)}${p.vacancy_source && p.vacancy_source.startsWith('acs') ? ' · ACS' : p.vacancy_source === 'default-5pct' ? ' · default' : ''}</span>
      <span class="ml-auto">${avmTag}</span>
    </div>

    <div class="px-4 mt-3 pt-3 border-t border-line text-xs flex flex-wrap gap-x-3 gap-y-1">
      ${r.schools && r.schools.school_count ? `
        <span class="text-muted">Schools:</span>
        <span><b>${r.schools.school_count}</b> public</span>
        <span>· ${r.schools.elementary_count}E · ${r.schools.middle_count}M · ${r.schools.high_count}H</span>
        ${r.schools.charter_count ? `<span>· ${r.schools.charter_count} charter</span>` : ''}
        ${r.schools.avg_st_ratio ? `<span>· ${r.schools.avg_st_ratio}:1 st/teach</span>` : ''}
      ` : '<span class="text-muted">Schools: —</span>'}
      ${r.county_median_income ? `<span class="ml-auto text-muted">County median income <b>${fmtMoney(r.county_median_income)}</b></span>` : ''}
    </div>

    <div class="px-4 mt-3 pt-3 border-t border-line">
      <div class="text-xs uppercase tracking-wide text-muted mb-1">Decision · ${d.thesis_tag}</div>
      <ul class="text-xs space-y-1.5 list-disc list-inside">${d.reasons.map(r => `<li>${r}</li>`).join('')}</ul>
      <div class="mt-2 text-xs ${v === 'GREEN' ? 'text-green' : v === 'YELLOW' ? 'text-yellow' : 'text-red'}">→ ${d.primary_action}</div>
    </div>

    <div class="px-4 py-3 mt-auto bg-bg border-t border-line flex items-center gap-3 text-xs">
      <a href="${L.url}" target="_blank" rel="noreferrer" class="text-accent hover:underline">View on Redfin ↗</a>
      <button class="text-muted hover:text-accent" onclick="prefillFromBuy('${encodeURIComponent(JSON.stringify({price:L.listed_price, sqft:L.sqft, year:L.year_built, rent:Math.round((p.cap_rate_y1*L.listed_price + (L.listed_price*0.012 + 1500))/12 + (p.dscr_y1>0 ? (p.cap_rate_y1*L.listed_price)/12 * 1.2 : 0))}))}')">Underwrite →</button>
    </div>
  </div>`;
}

function prefillFromBuy(payload) {
  const data = JSON.parse(decodeURIComponent(payload));
  go('underwrite');
  if (data.price) { $('uw_purchase_price').value = data.price; $('uw_arv').value = data.price; }
  if (data.rent && data.rent > 100) $('uw_monthly_rent').value = data.rent;
}
window.prefillFromBuy = prefillFromBuy;

['buyCbsa','buySort','buyMin','buyMax','buyRate','buyLimit',
 'buyZip','buyMinIrr','buyMinDscr','buyMinCap','buyMinSchools'].forEach(id =>
  document.addEventListener('DOMContentLoaded', () => $(id).addEventListener('change', loadBuy))
);
document.addEventListener('DOMContentLoaded', () => $('buyRefresh').addEventListener('click', loadBuy));

// ---- Top Zips (nationwide) ---------------------------------------------

async function loadTopZips() {
  const params = new URLSearchParams({
    sort: $('tzSort').value,
    limit: $('tzLimit').value || '100',
    min_price: $('tzMin').value,
    max_price: $('tzMax').value,
    mortgage_rate: $('tzRate').value,
  });
  const st = $('tzState').value.trim().toUpperCase();
  const cb = $('tzCbsa').value.trim();
  if (st) params.set('state', st);
  if (cb) params.set('cbsa', cb);
  $('tzHost').innerHTML = spinnerHTML('Scoring every US zip with ZHVI+ZORI coverage…');
  $('tzMeta').textContent = '';
  let r;
  try {
    r = await api('/zips/top?' + params.toString());
  } catch (e) {
    $('tzHost').innerHTML = `<div class="p-6 text-red">${e.message}</div>`;
    return;
  }
  $('tzMeta').textContent = `${r.count} zips ranked`;
  if (!r.results.length) {
    $('tzHost').innerHTML = '<div class="p-6 text-muted">No zips matched.</div>';
    return;
  }
  let html = '<table class="tight w-full text-sm"><thead><tr>'
    + '<th>Rank</th><th>ZIP</th><th>State</th><th>Metro</th>'
    + '<th class="text-right">ZHVI</th><th class="text-right">ZORI</th>'
    + '<th class="text-right" title="Last 12 months ZHVI change — price momentum">price 12mo</th>'
    + '<th class="text-right" title="Last 12 months ZORI change — rent momentum">rent 12mo</th>'
    + '<th title="Composite regime: avg(price+rent 12mo) clipped to ±15%">Regime</th>'
    + '<th class="text-right" title="Regime-adjusted: IRR × (1 + regime_score)">Adj IRR</th>'
    + '<th class="text-right">5y IRR</th><th class="text-right">5y total ($)</th>'
    + '<th class="text-right">5y rental ($)</th><th class="text-right">5y appr ($)</th>'
    + '<th class="text-right">cap</th><th class="text-right">DSCR</th><th class="text-right">vac</th>'
    + '<th>Browse</th>'
    + '</tr></thead><tbody>';
  r.results.forEach((z, i) => {
    // Whole row opens Redfin search for that ZIP in a new tab. Browse
    // column links retained for explicit Zillow choice; the inner <a>s
    // get a stopPropagation handler so they don't double-open.
    html += `<tr onclick="openZipBuyBox('${z.zip}', '${(z.redfin_search_url||'').replace(/'/g, '%27')}', '${(z.zillow_search_url||'').replace(/'/g, '%27')}')" style="cursor: pointer" title="Click for buy box + one-click stress test on ZIP ${z.zip}">
      <td class="text-muted">${i+1}</td>
      <td class="num text-accent font-semibold">${z.zip}</td>
      <td>${z.state || '—'}</td>
      <td class="text-xs">${z.cbsa_name || '—'}</td>
      <td class="text-right num">${fmtMoney(z.typical_price)}</td>
      <td class="text-right num">${fmtMoney(z.typical_rent)}/mo</td>
      <td class="text-right num ${z.chg_12mo < -0.05 ? 'text-red' : z.chg_12mo > 0.05 ? 'text-green' : 'text-muted'}" title="Trailing 5y CAGR ${fmtPct(z.appreciation_cagr_5y_trail, 1)} · Trailing 2y CAGR ${fmtPct(z.appreciation_cagr_2y_trail, 1)}">${fmtPct(z.chg_12mo, 1)}</td>
      <td class="text-right num ${z.rent_chg_12mo < -0.05 ? 'text-red' : z.rent_chg_12mo > 0.05 ? 'text-green' : 'text-muted'}">${fmtPct(z.rent_chg_12mo, 1)}</td>
      <td class="text-xs ${z.regime_label === 'expanding' ? 'text-green' : z.regime_label === 'crash' ? 'text-red font-semibold' : z.regime_label === 'contracting' ? 'text-yellow' : 'text-muted'}">${z.regime_label}</td>
      <td class="text-right num ${z.regime_adjusted_irr > 0.10 ? 'text-green' : z.regime_adjusted_irr < 0 ? 'text-red' : ''}">${fmtPct(z.regime_adjusted_irr, 1)}</td>
      <td class="text-right num ${z.irr_5y > 0.10 ? 'text-green' : z.irr_5y < 0 ? 'text-red' : 'text-muted'}">${fmtPct(z.irr_5y, 1)}</td>
      <td class="text-right num ${z.total_return_5y_dollars > 0 ? 'text-green' : 'text-red'}">${fmtMoney(z.total_return_5y_dollars)}</td>
      <td class="text-right num ${z.rental_profit_5y > 0 ? 'text-green' : 'text-red'}">${fmtMoney(z.rental_profit_5y)}</td>
      <td class="text-right num ${z.appreciation_5y_dollars > 0 ? 'text-green' : 'text-red'}">${fmtMoney(z.appreciation_5y_dollars)}</td>
      <td class="text-right num">${fmtPct(z.cap_rate_y1, 1)}</td>
      <td class="text-right num">${z.dscr_y1.toFixed(2)}×</td>
      <td class="text-right num">${fmtPct(z.vacancy_used, 1)}</td>
      <td class="text-xs whitespace-nowrap" onclick="event.stopPropagation()">
        <a href="${z.redfin_search_url}" target="_blank" rel="noreferrer" class="text-accent hover:underline">Redfin↗</a> ·
        <a href="${z.zillow_search_url}" target="_blank" rel="noreferrer" class="text-accent hover:underline">Zillow↗</a>
      </td>
    </tr>`;
  });
  html += '</tbody></table>';
  $('tzHost').innerHTML = html;
}

['tzState','tzCbsa','tzSort','tzMin','tzMax','tzRate','tzLimit'].forEach(id =>
  document.addEventListener('DOMContentLoaded', () => {
    const el = $(id); if (el) el.addEventListener('change', loadTopZips);
  })
);
document.addEventListener('DOMContentLoaded', () => {
  const btn = $('tzRefresh'); if (btn) btn.addEventListener('click', loadTopZips);
});

// ---- Listing ingestion -------------------------------------------------

async function ingestLink() {
  const url = $('linkInput').value.trim();
  if (!url) return;
  $('linkOut').innerHTML = `<div class="flex items-center gap-2 text-muted text-xs"><div class="spinner" style="width:14px;height:14px;border-width:2px;"></div>Fetching listing details…</div>`;
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

// ---- Portfolio view ----------------------------------------------------

let PORTFOLIO_BOUND = false;
async function loadPortfolio() {
  if (!PORTFOLIO_BOUND) {
    PORTFOLIO_BOUND = true;
    $('pfRefresh').addEventListener('click', loadPortfolio);
    ['pfBracket', 'pfLandAlloc', 'pfActive'].forEach(id => {
      $(id)?.addEventListener('change', loadPortfolio);
    });
  }
  const host = $('portfolioHost');
  if (!DEALS.length) {
    host.innerHTML = `<div class="bg-card border border-line rounded p-8 text-center text-muted text-sm">
      No saved deals yet. Run a stress test and save it — portfolio metrics aggregate across whatever's in your pipeline.
    </div>`;
    return;
  }
  host.innerHTML = spinnerHTML('Aggregating portfolio + tax math…');
  const body = {
    deals: DEALS,
    tax_bracket:                +$('pfBracket').value,
    land_allocation:            +$('pfLandAlloc').value,
    useful_life_years:          27.5,
    deduction_against_ordinary: $('pfActive').value === 'true',
  };
  try {
    const r = await fetch('/api/portfolio/aggregate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();
    host.innerHTML = renderPortfolio(data);
  } catch (e) {
    host.innerHTML = `<div class="bg-card border border-red rounded p-4 text-red text-sm">Error: ${escapeHtml(e.message)}</div>`;
  }
}

function renderPortfolio(p) {
  const t = p.totals;
  const fmtMoney = (x) => '$' + Math.round(x || 0).toLocaleString();
  const fmtMoneySigned = (x) => (x >= 0 ? '+' : '') + fmtMoney(x);
  // Hero stats grid
  const cf_pre = t.annual_cf_pretax, cf_post = t.annual_cf_posttax;
  const cf_post_color = cf_post >= 0 ? 'text-green' : 'text-red';
  const cf_pre_color  = cf_pre >= 0 ? 'text-green' : 'text-red';
  const irr_pre  = t.weighted_irr_pretax;
  const irr_post = t.weighted_irr_posttax;
  const hero = `
    <div class="grid md:grid-cols-2 lg:grid-cols-4 gap-3">
      <div class="bg-card border border-line rounded p-4">
        <div class="text-xs uppercase tracking-wide text-muted">Equity deployed</div>
        <div class="text-2xl font-bold text-fg mt-1">${fmtMoney(t.equity_deployed)}</div>
        <div class="text-xs text-muted mt-1">${p.count} deal${p.count===1?'':'s'}</div>
      </div>
      <div class="bg-card border border-line rounded p-4">
        <div class="text-xs uppercase tracking-wide text-muted">Monthly CF (post-tax)</div>
        <div class="text-2xl font-bold ${cf_post_color} mt-1">${fmtMoneySigned(t.monthly_cf_posttax)}</div>
        <div class="text-xs text-muted mt-1">pre-tax: <span class="${cf_pre_color}">${fmtMoneySigned(t.monthly_cf_pretax)}</span></div>
      </div>
      <div class="bg-card border border-line rounded p-4">
        <div class="text-xs uppercase tracking-wide text-muted">Weighted IRR</div>
        <div class="text-2xl font-bold ${irrColor(irr_post)} mt-1">${fmtPct(irr_post)} <span class="text-xs text-muted font-normal">post-tax</span></div>
        <div class="text-xs text-muted mt-1">pre-tax: <span class="${irrColor(irr_pre)}">${fmtPct(irr_pre)}</span></div>
      </div>
      <div class="bg-card border border-line rounded p-4">
        <div class="text-xs uppercase tracking-wide text-muted">Annual tax savings</div>
        <div class="text-2xl font-bold text-green mt-1">${fmtMoney(t.annual_tax_savings)}</div>
        <div class="text-xs text-muted mt-1">depreciation: ${fmtMoney(t.annual_depreciation)}/yr</div>
      </div>
    </div>`;

  // Concentration warnings
  const warnings = p.concentration_warnings.length ? `
    <div class="bg-card border border-yellow rounded p-4">
      <div class="text-xs uppercase tracking-wide text-yellow mb-2">⚠ Concentration warnings</div>
      <ul class="list-disc pl-5 space-y-1 text-sm">
        ${p.concentration_warnings.map(w => `<li>${escapeHtml(w)}</li>`).join('')}
      </ul>
    </div>` : '';

  // Concentration bars (state / verdict / status)
  const bars = (rows, title, colorOf) => {
    if (!rows.length) return '';
    return `
      <div class="bg-card border border-line rounded p-4">
        <div class="text-xs uppercase tracking-wide text-muted mb-3">${title}</div>
        <div class="space-y-2">
          ${rows.map(r => `
            <div class="text-sm">
              <div class="flex justify-between mb-1">
                <span>${escapeHtml(r.label)} <span class="text-xs text-muted">(${r.deals} deal${r.deals===1?'':'s'})</span></span>
                <span class="tabular-nums">${fmtMoney(r.equity)} · ${(r.pct*100).toFixed(0)}%</span>
              </div>
              <div class="h-2 bg-bg rounded overflow-hidden">
                <div class="h-full ${colorOf(r)}" style="width: ${r.pct*100}%"></div>
              </div>
            </div>`).join('')}
        </div>
      </div>`;
  };
  const stateColor = (r) => {
    const climate = ['FL','TX','CA','AZ','NV','CO','LA','MS','AL'];
    return climate.includes(r.key) ? 'bg-yellow' : 'bg-accent';
  };
  const verdictColor = (r) => ({GREEN:'bg-green',YELLOW:'bg-yellow',RED:'bg-red'})[r.key] || 'bg-fg';
  const statusColor = (r) => 'bg-accent';

  const concentrationGrid = `
    <div class="grid md:grid-cols-3 gap-3">
      ${bars(p.by_state,   'Equity by state',   stateColor)}
      ${bars(p.by_verdict, 'Equity by verdict', verdictColor)}
      ${bars(p.by_status,  'Equity by status',  statusColor)}
    </div>`;

  // Per-deal table
  const rows = p.deals_with_tax.map(d => {
    const v = d.verdict || '—';
    const vCol = {GREEN:'text-green',YELLOW:'text-yellow',RED:'text-red'}[v] || '';
    return `
      <tr class="border-t border-line hover:bg-bg">
        <td class="py-2 pr-3 font-medium">${escapeHtml(d.label || '—')}</td>
        <td class="py-2 pr-3 ${vCol} font-semibold">${v}</td>
        <td class="py-2 pr-3 text-xs text-muted">${d.state || '—'}</td>
        <td class="py-2 pr-3 text-right tabular-nums">${fmtMoney(d.equity)}</td>
        <td class="py-2 pr-3 text-right tabular-nums ${irrColor(d.irr_pretax)}">${fmtPct(d.irr_pretax)}</td>
        <td class="py-2 pr-3 text-right tabular-nums ${irrColor(d.irr_posttax)}">${fmtPct(d.irr_posttax)}</td>
        <td class="py-2 pr-3 text-right tabular-nums ${d.annual_cf_pretax >= 0 ? 'text-green' : 'text-red'}">${fmtMoneySigned(d.annual_cf_pretax)}</td>
        <td class="py-2 pr-3 text-right tabular-nums ${d.annual_cf_posttax >= 0 ? 'text-green' : 'text-red'}">${fmtMoneySigned(d.annual_cf_posttax)}</td>
        <td class="py-2 pr-3 text-right tabular-nums text-green">${fmtMoney(d.annual_tax_savings)}</td>
      </tr>`;
  }).join('');
  const dealTable = `
    <div class="bg-card border border-line rounded overflow-hidden">
      <div class="px-4 py-3 text-xs uppercase tracking-wide text-muted border-b border-line">Per-deal breakdown</div>
      <div class="overflow-x-auto">
        <table class="w-full text-sm">
          <thead class="text-xs uppercase tracking-wide text-muted bg-bg">
            <tr>
              <th class="text-left py-2 pl-4 pr-3">Deal</th>
              <th class="text-left py-2 pr-3">Verdict</th>
              <th class="text-left py-2 pr-3">State</th>
              <th class="text-right py-2 pr-3">Equity</th>
              <th class="text-right py-2 pr-3">IRR pre</th>
              <th class="text-right py-2 pr-3">IRR post</th>
              <th class="text-right py-2 pr-3">CF/yr pre</th>
              <th class="text-right py-2 pr-3">CF/yr post</th>
              <th class="text-right py-2 pr-3">Tax saved</th>
            </tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    </div>`;

  // Tax assumptions caveat
  const tA = p.tax_assumptions;
  const caveat = `
    <div class="text-xs text-muted bg-bg border border-line rounded p-3">
      <span class="uppercase tracking-wide">Tax model:</span>
      ${(tA.tax_bracket*100).toFixed(0)}% bracket · ${(tA.land_allocation*100).toFixed(0)}% land · ${tA.useful_life_years}yr useful life ·
      ${tA.deduction_against_ordinary ? '<span class="text-green">active deduction</span> (REP/STR/sub-$100K)' : '<span class="text-yellow">passive — losses suspended</span>'}.
      Model excludes: state tax, 1031 exchanges, opportunity zones, bonus depreciation, cost-seg, AMT, exit recapture drag.
      Treat post-tax IRR as the optimistic ceiling.
    </div>`;

  return [hero, warnings, concentrationGrid, dealTable, caveat].filter(Boolean).join('\n');
}

// ---- Buy box (per-zip target bands + one-click stress) ------------------

let LAST_BUYBOX = null;

async function openZipBuyBox(zip, redfinUrl, zillowUrl) {
  document.getElementById('buyboxModal').classList.remove('hidden');
  document.getElementById('buyboxTitle').textContent = `ZIP ${zip}`;
  document.getElementById('buyboxSubtitle').textContent = '';
  document.getElementById('buyboxBody').innerHTML = spinnerHTML('Deriving buy box…');
  document.getElementById('buyboxRedfin').href = redfinUrl || `https://www.redfin.com/zipcode/${zip}/filter/property-type=house`;
  document.getElementById('buyboxZillow').href = zillowUrl || `https://www.zillow.com/homes/${zip}_rb/`;
  try {
    const r = await fetch(`/api/zips/${encodeURIComponent(zip)}/buybox`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const b = await r.json();
    LAST_BUYBOX = b;
    document.getElementById('buyboxTitle').textContent = `ZIP ${b.zip} — ${b.cbsa_name || 'Unmapped'}`;
    document.getElementById('buyboxSubtitle').textContent =
      `${b.state || '—'}  ·  Regime: ${b.regime_label} (${(b.regime_score*100).toFixed(1)}%)  ·  ${b.archetype_hint || 'Mixed'}`;
    document.getElementById('buyboxBody').innerHTML = renderBuyBox(b);
  } catch (e) {
    document.getElementById('buyboxBody').innerHTML = `<div class="text-red">Failed: ${escapeHtml(e.message)}</div>`;
  }
}

function renderBuyBox(b) {
  const fmtMoney = (x) => '$' + (x || 0).toLocaleString();
  const regimeBadge = ({
    expanding: 'bg-green/10 text-green border-green',
    mixed:     'bg-bg text-muted border-line',
    contracting: 'bg-yellow/10 text-yellow border-yellow',
    crash:     'bg-red/10 text-red border-red',
  })[b.regime_label] || 'border-line text-muted';
  const climateBlock = b.climate ? renderClimateBlock(b.climate) : '';
  return climateBlock + `
    <div class="grid grid-cols-3 gap-3">
      <div class="bg-bg rounded border border-line p-3">
        <div class="text-xs uppercase tracking-wide text-muted">Target price</div>
        <div class="text-fg font-medium text-base mt-1">${fmtMoney(b.target_price_low)} – ${fmtMoney(b.target_price_high)}</div>
        <div class="text-xs text-muted mt-1">mid: ${fmtMoney(b.target_price_mid)} (ZHVI)</div>
      </div>
      <div class="bg-bg rounded border border-line p-3">
        <div class="text-xs uppercase tracking-wide text-muted">Target rent</div>
        <div class="text-fg font-medium text-base mt-1">${fmtMoney(b.target_rent_low)} – ${fmtMoney(b.target_rent_high)}/mo</div>
        <div class="text-xs text-muted mt-1">mid: ${fmtMoney(b.target_rent_mid)} (ZORI)</div>
      </div>
      <div class="bg-bg rounded border border-line p-3">
        <div class="text-xs uppercase tracking-wide text-muted">Rehab band</div>
        <div class="text-fg font-medium text-base mt-1">${fmtMoney(b.target_rehab_light)} – ${fmtMoney(b.target_rehab_heavy)}</div>
        <div class="text-xs text-muted mt-1">cosmetic → value-add BRRRR</div>
      </div>
      <div class="bg-bg rounded border border-line p-3">
        <div class="text-xs uppercase tracking-wide text-muted">ARV (12mo)</div>
        <div class="text-fg font-medium text-base mt-1">${fmtMoney(b.arv_trend_12mo)}</div>
        <div class="text-xs text-muted mt-1">today: ${fmtMoney(b.arv_now)}</div>
      </div>
      <div class="bg-bg rounded border border-line p-3">
        <div class="text-xs uppercase tracking-wide text-muted">Target cap rate</div>
        <div class="text-fg font-medium text-base mt-1">≥ ${(b.target_cap_rate*100).toFixed(1)}%</div>
        <div class="text-xs text-muted mt-1">floor: ${(b.floor_cap_rate*100).toFixed(1)}%</div>
      </div>
      <div class="bg-bg rounded border border-line p-3">
        <div class="text-xs uppercase tracking-wide text-muted">County vacancy</div>
        <div class="text-fg font-medium text-base mt-1">${(b.vacancy_used*100).toFixed(1)}%</div>
        <div class="text-xs text-muted mt-1">stress goes higher</div>
      </div>
    </div>
    <div class="bg-bg rounded border border-line p-3 text-xs text-muted">
      <span class="uppercase tracking-wide">ARV method:</span> ${escapeHtml(b.arv_method)}
    </div>
    ${b.notes && b.notes.length ? `
    <div class="bg-bg rounded border border-line p-3">
      <div class="text-xs uppercase tracking-wide text-muted mb-2">Notes</div>
      <ul class="list-disc pl-5 space-y-1 text-sm">
        ${b.notes.map(n => `<li>${escapeHtml(n)}</li>`).join('')}
      </ul>
    </div>` : ''}
  `;
}

function renderClimateBlock(c) {
  const catColor = {
    minimal:  'border-green text-green',
    moderate: 'border-fg text-fg',
    elevated: 'border-yellow text-yellow',
    severe:   'border-red text-red',
  }[c.category] || 'border-line text-muted';
  const score = c.overall_score;
  // Visual score bar
  const barColor = score >= 75 ? 'bg-red' : score >= 50 ? 'bg-yellow' : score >= 20 ? 'bg-fg' : 'bg-green';
  return `
    <div class="bg-bg rounded border ${catColor.split(' ')[0]} p-3 mb-3">
      <div class="flex items-baseline justify-between mb-2">
        <div>
          <span class="text-xs uppercase tracking-wide text-muted">Climate risk</span>
          <span class="ml-2 px-2 py-0.5 rounded border ${catColor} text-xs font-semibold uppercase">${c.category}</span>
        </div>
        <div class="text-2xl font-bold ${catColor.split(' ')[1]}">${score}<span class="text-xs text-muted font-normal">/100</span></div>
      </div>
      <div class="h-2 bg-card rounded overflow-hidden">
        <div class="h-full ${barColor}" style="width: ${score}%"></div>
      </div>
      <div class="grid grid-cols-3 gap-2 mt-2 text-xs text-muted">
        <div>Primary risk: <span class="text-fg uppercase">${c.primary_risk}</span></div>
        <div>5y NFIP claims: <span class="text-fg">${Math.round(c.flood_claims_5y).toLocaleString()}</span></div>
        <div>5y NFIP paid: <span class="text-fg">$${Math.round(c.flood_paid_5y).toLocaleString()}</span></div>
      </div>
      ${c.notes && c.notes.length ? `<div class="text-xs text-muted mt-2 space-y-1">${c.notes.map(n => `<div>• ${escapeHtml(n)}</div>`).join('')}</div>` : ''}
    </div>
  `;
}

function closeBuyBox() {
  document.getElementById('buyboxModal').classList.add('hidden');
  LAST_BUYBOX = null;
}

function stressFromBuyBox() {
  if (!LAST_BUYBOX) return;
  const d = LAST_BUYBOX.typical_deal;
  const zip = LAST_BUYBOX.zip;
  // Switch to Stress tab and fill inputs
  go('stress');
  closeBuyBox();
  // Small defer so the screen is visible before we set values
  setTimeout(() => {
    $('stPrice').value = d.purchase_price;
    $('stRent').value  = d.monthly_rent;
    $('stRehab').value = d.rehab_cost;
    $('stRate').value  = d.mortgage_rate;
    $('stLtv').value   = d.ltv;
    $('stVac').value   = d.vacancy;
    $('stIns').value   = d.insurance_annual;
    $('stTax').value   = d.property_tax_rate;
    $('stHoa').value   = 0;
    $('stARV').value   = LAST_BUYBOX.arv_trend_12mo || '';
    if ($('stZip')) $('stZip').value = zip || '';
    if (d.state) {
      const sel = $('stState');
      if ([...sel.options].some(o => o.value === d.state)) sel.value = d.state;
    }
    runStress();   // auto-fire (with climate overlay since zip is set)
  }, 50);
}

window.openZipBuyBox = openZipBuyBox;
window.closeBuyBox = closeBuyBox;
window.stressFromBuyBox = stressFromBuyBox;

// ---- Deal pipeline ------------------------------------------------------
//
// localStorage `reip_deals_v1`. Every deal carries inputs (so we can re-run
// stress) plus the last stress result (so the list view can color the
// verdict without re-hitting the API).

const DEALS_KEY = 'reip_deals_v1';
let DEALS = [];
let PIPE_FILTER = 'all';
let PIPE_SELECTED = new Set();    // for compare mode
let PIPE_EDITING_ID = null;
let LAST_STRESS_RESULT = null;    // capture stress test output so Save can attach

const STATUS_META = {
  researching:    { icon: '🔬', label: 'Researching',   color: 'border-fg text-fg' },
  underwritten:   { icon: '📝', label: 'Underwritten',  color: 'border-accent text-accent' },
  offer:          { icon: '📞', label: 'Offer made',    color: 'border-yellow text-yellow' },
  under_contract: { icon: '📃', label: 'Under contract', color: 'border-yellow text-yellow' },
  closed:         { icon: '✅', label: 'Closed',         color: 'border-green text-green' },
  passed:         { icon: '❌', label: 'Passed',         color: 'border-red text-red' },
};

function loadDeals() {
  try {
    DEALS = JSON.parse(localStorage.getItem(DEALS_KEY) || '[]');
    if (!Array.isArray(DEALS)) DEALS = [];
  } catch (e) { DEALS = []; }
}

function persistDeals() {
  DEALS.sort((a, b) => (b.updated_at || 0) - (a.updated_at || 0));
  localStorage.setItem(DEALS_KEY, JSON.stringify(DEALS));
  updatePipelineCount();
}

function newDealFromStress(inputs, stressResult, opts = {}) {
  const verdict = stressResult?.gate?.verdict;
  const status = opts.status || (verdict === 'GREEN' ? 'underwritten' : 'researching');
  const deal = {
    id: 'd_' + Date.now().toString(36) + '_' + Math.random().toString(36).slice(2, 6),
    label: opts.label || _defaultDealLabel(inputs),
    link: opts.link || '',
    notes: opts.notes || '',
    status,
    inputs,
    stress: stressResult,
    created_at: Date.now(),
    updated_at: Date.now(),
  };
  DEALS.unshift(deal);
  persistDeals();
  return deal;
}

function buildPipelineSummary() {
  // Map full deal objects to the compact shape chat.py reads. Limit to the
  // 12 most-recently-updated to keep the payload small.
  return DEALS.slice(0, 12).map(d => ({
    label:           d.label,
    status:          d.status,
    verdict:         d.stress?.gate?.verdict,
    purchase_price:  d.inputs?.purchase_price,
    monthly_rent:    d.inputs?.monthly_rent,
    state:           d.inputs?.state,
    base_irr:        d.stress?.scenarios?.[0]?.irr,
    worst_irr:       d.stress?.scenarios?.[2]?.irr,
    price_to_green:  d.stress?.price_to_green,
    notes:           d.notes,
  }));
}

function _defaultDealLabel(inputs) {
  const p = inputs.purchase_price ? `$${Math.round(inputs.purchase_price / 1000)}k` : '?';
  const r = inputs.monthly_rent ? `$${Math.round(inputs.monthly_rent)}/mo` : '?';
  const s = inputs.state ? ` (${inputs.state})` : '';
  return `${p} / ${r}${s}`;
}

function updateDeal(id, patch) {
  const i = DEALS.findIndex(d => d.id === id);
  if (i < 0) return;
  DEALS[i] = { ...DEALS[i], ...patch, updated_at: Date.now() };
  persistDeals();
}

function deleteDeal(id) {
  DEALS = DEALS.filter(d => d.id !== id);
  PIPE_SELECTED.delete(id);
  persistDeals();
}

function clearAllDeals() {
  if (!confirm(`Delete all ${DEALS.length} saved deals? This can't be undone.`)) return;
  DEALS = [];
  PIPE_SELECTED.clear();
  localStorage.removeItem(DEALS_KEY);
  updatePipelineCount();
}

function updatePipelineCount() {
  const el = document.getElementById('pipelineNavCount');
  if (el) el.textContent = DEALS.length ? `(${DEALS.length})` : '';
}

// ---- Pipeline render ---------------------------------------------------

function loadPipeline() {
  renderPipeline();
}

function renderPipeline() {
  const host = document.getElementById('pipelineHost');
  if (!host) return;
  const filtered = PIPE_FILTER === 'all'
    ? DEALS
    : DEALS.filter(d => d.status === PIPE_FILTER);
  if (!filtered.length) {
    host.innerHTML = `<div class="p-8 text-center text-muted text-sm">
      ${DEALS.length ? `No deals with status "${PIPE_FILTER}".` : 'No saved deals yet. Run a stress test and click <b class="text-accent">Save to pipeline</b>.'}
    </div>`;
    return;
  }
  const rows = filtered.map(d => {
    const v = d.stress?.gate?.verdict;
    const vColor = { GREEN: 'text-green', YELLOW: 'text-yellow', RED: 'text-red' }[v] || 'text-muted';
    const base = d.stress?.scenarios?.[0];
    const worst = d.stress?.scenarios?.[2];
    const s = STATUS_META[d.status] || { icon: '?', label: d.status, color: '' };
    const selected = PIPE_SELECTED.has(d.id);
    // Climate badge: comes from d.stress.climate if the deal was stress-tested with a zip
    const c = d.stress?.climate;
    const cBadge = c ? (() => {
      const cls = {minimal:'text-green',moderate:'text-fg',elevated:'text-yellow',severe:'text-red'}[c.category] || 'text-muted';
      return `<span class="${cls}" title="${escapeHtml((c.notes||[]).join(' · '))}">${c.overall_score}</span>`;
    })() : '<span class="text-muted">—</span>';
    return `
      <tr class="border-t border-line hover:bg-bg cursor-pointer ${selected ? 'bg-bg' : ''}" onclick="openPipeDetail('${d.id}')">
        <td class="py-2 pl-3 pr-1" onclick="event.stopPropagation(); togglePipeSelect('${d.id}')">
          <input type="checkbox" class="cursor-pointer" ${selected ? 'checked' : ''}>
        </td>
        <td class="py-2 pr-3 font-medium">${escapeHtml(d.label)}</td>
        <td class="py-2 pr-3 text-xs"><span class="inline-block px-1.5 py-0.5 rounded border ${s.color}">${s.icon} ${s.label}</span></td>
        <td class="py-2 pr-3 font-semibold ${vColor}">${v || '—'}</td>
        <td class="py-2 pr-3 text-right tabular-nums">${base ? fmtPct(base.cash_on_cash) : '—'}</td>
        <td class="py-2 pr-3 text-right tabular-nums">${worst ? fmtPct(worst.irr) : '—'}</td>
        <td class="py-2 pr-3 text-right tabular-nums">${d.stress?.price_to_green ? '$' + d.stress.price_to_green.toLocaleString() : '—'}</td>
        <td class="py-2 pr-3 text-right tabular-nums font-semibold">${cBadge}</td>
        <td class="py-2 pr-3 text-xs text-muted">${formatRelative(d.updated_at)}</td>
      </tr>`;
  }).join('');
  host.innerHTML = `
    <table class="w-full text-sm">
      <thead class="text-xs uppercase tracking-wide text-muted bg-bg">
        <tr>
          <th class="py-2 pl-3 pr-1"></th>
          <th class="text-left py-2 pr-3">Deal</th>
          <th class="text-left py-2 pr-3">Status</th>
          <th class="text-left py-2 pr-3">Verdict</th>
          <th class="text-right py-2 pr-3">Base CoC</th>
          <th class="text-right py-2 pr-3">Worst IRR</th>
          <th class="text-right py-2 pr-3">Walk-away $</th>
          <th class="text-right py-2 pr-3" title="Climate risk score 0-100 (from FEMA NFIP)">Climate</th>
          <th class="text-right py-2 pr-3">Updated</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>`;
  updateCompareButton();
}

function togglePipeSelect(id) {
  if (PIPE_SELECTED.has(id)) PIPE_SELECTED.delete(id);
  else if (PIPE_SELECTED.size < 3) PIPE_SELECTED.add(id);
  else { alert('Compare up to 3 deals at a time.'); return; }
  renderPipeline();
}

function updateCompareButton() {
  const btn = document.getElementById('pipeCompareBtn');
  const countEl = document.getElementById('pipeCompareCount');
  if (!btn || !countEl) return;
  countEl.textContent = PIPE_SELECTED.size;
  btn.disabled = PIPE_SELECTED.size < 2;
}

// ---- Pipeline detail modal ---------------------------------------------

function openPipeDetail(id) {
  const d = DEALS.find(x => x.id === id);
  if (!d) return;
  PIPE_EDITING_ID = id;
  document.getElementById('pipeDetailLabel').value = d.label || '';
  document.getElementById('pipeDetailStatus').value = d.status || 'researching';
  document.getElementById('pipeDetailLink').value = d.link || '';
  document.getElementById('pipeDetailNotes').value = d.notes || '';
  document.getElementById('pipeDetailStress').innerHTML = d.stress
    ? renderStressResult(d.stress)
    : '<div class="text-muted text-sm">No stress result attached.</div>';
  document.getElementById('pipeDetailModal').classList.remove('hidden');
}

function closePipeDetail() {
  // Persist edits
  if (PIPE_EDITING_ID) {
    updateDeal(PIPE_EDITING_ID, {
      label: document.getElementById('pipeDetailLabel').value.trim() || 'Untitled deal',
      status: document.getElementById('pipeDetailStatus').value,
      link: document.getElementById('pipeDetailLink').value.trim(),
      notes: document.getElementById('pipeDetailNotes').value.trim(),
    });
  }
  PIPE_EDITING_ID = null;
  document.getElementById('pipeDetailModal').classList.add('hidden');
  renderPipeline();
}

async function rerunCurrentDeal() {
  if (!PIPE_EDITING_ID) return;
  const d = DEALS.find(x => x.id === PIPE_EDITING_ID);
  if (!d) return;
  const host = document.getElementById('pipeDetailStress');
  host.innerHTML = spinnerHTML('Re-running stress test…');
  try {
    const r = await fetch('/api/stress', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(d.inputs),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const fresh = await r.json();
    updateDeal(PIPE_EDITING_ID, { stress: fresh });
    host.innerHTML = renderStressResult(fresh);
  } catch (e) {
    host.innerHTML = `<div class="text-red text-sm">Re-run failed: ${escapeHtml(e.message)}</div>`;
  }
}

function deleteCurrentDeal() {
  if (!PIPE_EDITING_ID) return;
  if (!confirm('Delete this deal?')) return;
  deleteDeal(PIPE_EDITING_ID);
  PIPE_EDITING_ID = null;
  document.getElementById('pipeDetailModal').classList.add('hidden');
  renderPipeline();
}

// ---- Compare drawer -----------------------------------------------------

function openPipeCompare() {
  const picked = [...PIPE_SELECTED].map(id => DEALS.find(d => d.id === id)).filter(Boolean);
  if (picked.length < 2) return;
  const host = document.getElementById('pipeCompareHost');
  host.innerHTML = renderCompare(picked);
  document.getElementById('pipeCompareModal').classList.remove('hidden');
}

function closePipeCompare() {
  document.getElementById('pipeCompareModal').classList.add('hidden');
}

function renderCompare(deals) {
  const metrics = [
    { label: 'Verdict',       get: d => d.stress?.gate?.verdict || '—',
      cls: d => ({ GREEN: 'text-green', YELLOW: 'text-yellow', RED: 'text-red' }[d.stress?.gate?.verdict] || '') },
    { label: 'Purchase price', get: d => '$' + (d.inputs.purchase_price || 0).toLocaleString() },
    { label: 'Monthly rent',   get: d => '$' + (d.inputs.monthly_rent || 0).toLocaleString() },
    { label: 'State',          get: d => d.inputs.state || '—' },
    { label: 'Base IRR',       get: d => fmtPct(d.stress?.scenarios?.[0]?.irr), cls: d => irrColor(d.stress?.scenarios?.[0]?.irr) },
    { label: 'Base CoC',       get: d => fmtPct(d.stress?.scenarios?.[0]?.cash_on_cash), cls: d => cocColor(d.stress?.scenarios?.[0]?.cash_on_cash) },
    { label: 'Base DSCR',      get: d => d.stress?.scenarios?.[0]?.dscr?.toFixed(2) ?? '—', cls: d => dscrColor(d.stress?.scenarios?.[0]?.dscr) },
    { label: 'Stress IRR',     get: d => fmtPct(d.stress?.scenarios?.[1]?.irr), cls: d => irrColor(d.stress?.scenarios?.[1]?.irr) },
    { label: 'Stress DSCR',    get: d => d.stress?.scenarios?.[1]?.dscr?.toFixed(2) ?? '—', cls: d => dscrColor(d.stress?.scenarios?.[1]?.dscr) },
    { label: 'Worst IRR',      get: d => fmtPct(d.stress?.scenarios?.[2]?.irr), cls: d => irrColor(d.stress?.scenarios?.[2]?.irr) },
    { label: 'Worst DSCR',     get: d => d.stress?.scenarios?.[2]?.dscr?.toFixed(2) ?? '—', cls: d => dscrColor(d.stress?.scenarios?.[2]?.dscr) },
    { label: 'Walk-away $',    get: d => d.stress?.price_to_green ? '$' + d.stress.price_to_green.toLocaleString() : '—' },
    { label: 'State overlay',  get: d => d.stress?.state_overlay_summary || '—' },
    { label: 'Status',         get: d => (STATUS_META[d.status]?.label || d.status) },
  ];
  const head = deals.map(d => `<th class="py-2 px-3 text-left text-fg font-medium border-b border-line">${escapeHtml(d.label)}</th>`).join('');
  const rows = metrics.map(m => {
    const cells = deals.map(d => `<td class="py-2 px-3 ${m.cls ? m.cls(d) : ''}">${m.get(d)}</td>`).join('');
    return `<tr class="border-b border-line"><td class="py-2 px-3 text-xs uppercase tracking-wide text-muted">${m.label}</td>${cells}</tr>`;
  }).join('');
  return `
    <table class="w-full text-sm">
      <thead>
        <tr>
          <th class="py-2 px-3 text-left text-xs uppercase tracking-wide text-muted border-b border-line">Metric</th>
          ${head}
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>`;
}

window.openPipeDetail = openPipeDetail;
window.closePipeDetail = closePipeDetail;
window.closePipeCompare = closePipeCompare;
window.togglePipeSelect = togglePipeSelect;

// ---- Stress test --------------------------------------------------------

let STRESS_BOUND = false;
function initStress() {
  if (STRESS_BOUND) return;
  STRESS_BOUND = true;
  $('stRun').addEventListener('click', runStress);
}

async function runStress() {
  const zip = ($('stZip')?.value || '').trim() || null;
  const body = {
    purchase_price: +$('stPrice').value,
    monthly_rent:   +$('stRent').value,
    rehab_cost:     +$('stRehab').value || 0,
    arv:            $('stARV').value ? +$('stARV').value : null,
    mortgage_rate:  +$('stRate').value,
    ltv:            +$('stLtv').value,
    vacancy:        +$('stVac').value,
    insurance_annual: +$('stIns').value,
    property_tax_rate: +$('stTax').value,
    hoa_monthly:    +$('stHoa').value || 0,
    state:          $('stState').value || null,
    zip:            zip,
  };
  const host = $('stResultHost');
  host.innerHTML = spinnerHTML('Running base / stress / worst scenarios + price-to-green search…');
  try {
    const r = await fetch('/api/stress', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();
    LAST_STRESS_RESULT = { inputs: body, result: data };
    host.innerHTML = renderStressResult(data)
      + `<div class="bg-card border border-line rounded p-4 flex items-center gap-3">
           <div class="text-sm flex-1">
             <div class="text-fg font-medium">Save this deal to your pipeline?</div>
             <div class="text-xs text-muted">Auto-titled <span class="text-fg">${escapeHtml(_defaultDealLabel(body))}</span> — you can rename, set status, and add notes from the Pipeline tab.</div>
           </div>
           <button id="stressSaveBtn" class="px-4 py-2 rounded bg-accent text-bg font-medium hover:opacity-90">+ Save to pipeline</button>
         </div>`;
    document.getElementById('stressSaveBtn')?.addEventListener('click', saveStressToPipeline);
  } catch (e) {
    host.innerHTML = `<div class="bg-card border border-red rounded p-4 text-red text-sm">Error: ${e.message}</div>`;
  }
}

function saveStressToPipeline() {
  if (!LAST_STRESS_RESULT) return;
  const d = newDealFromStress(LAST_STRESS_RESULT.inputs, LAST_STRESS_RESULT.result);
  const btn = document.getElementById('stressSaveBtn');
  if (btn) {
    btn.textContent = '✓ Saved — open Pipeline';
    btn.classList.remove('bg-accent', 'text-bg');
    btn.classList.add('border', 'border-green', 'text-green');
    btn.onclick = () => go('pipeline');
  }
}

function renderStressResult(d) {
  const verdict = d.gate.verdict;
  const verdictColor = { GREEN: 'text-green border-green', YELLOW: 'text-yellow border-yellow', RED: 'text-red border-red' }[verdict] || 'text-fg border-line';
  const overlayLine = d.state_overlay_summary
    ? `<div class="text-xs text-muted mt-1">State overlay: <span class="text-fg">${d.state_overlay_summary}</span></div>`
    : (d.state ? `<div class="text-xs text-muted mt-1">No state overlay for ${d.state}.</div>` : '');
  const climateLine = d.climate_overlay_summary
    ? `<div class="text-xs text-muted mt-1">Climate overlay: <span class="text-fg">${escapeHtml(d.climate_overlay_summary)}</span></div>`
    : '';
  const climateBlock = d.climate ? renderClimateBlock(d.climate) : '';
  // Post-tax IRR per scenario, when a tax bracket is selected
  const taxBracket = $('stTaxBracket') ? +$('stTaxBracket').value : 0;
  const postTaxBlock = taxBracket > 0 ? renderPostTaxIRR(d, taxBracket) : '';

  const scenRows = d.scenarios.map(s => {
    const deltaChips = Object.entries(s.deltas || {}).map(([k, v]) => {
      const label = ({
        rent_pct: `rent ${(v * 100).toFixed(0)}%`,
        vacancy_pp: `vac ${v >= 0 ? '+' : ''}${(v * 100).toFixed(0)}pp`,
        rate_bps: `rate ${v >= 0 ? '+' : ''}${v}bps`,
        insurance_pct: `ins ${v >= 0 ? '+' : ''}${(v * 100).toFixed(0)}%`,
        rehab_pct: `rehab ${v >= 0 ? '+' : ''}${(v * 100).toFixed(0)}%`,
        exit_cap_bps: `exit-cap ${v >= 0 ? '+' : ''}${v}bps`,
        tax_rate_pp: `tax ${v >= 0 ? '+' : ''}${v.toFixed(2)}pp`,
      })[k] || `${k}=${v}`;
      return `<span class="text-xs bg-bg border border-line rounded px-1.5 py-0.5 mr-1">${label}</span>`;
    }).join('');
    const irrCell = fmtPct(s.irr);
    const cocCell = fmtPct(s.cash_on_cash);
    const dscrCell = s.dscr != null ? s.dscr.toFixed(2) : '—';
    const bep = s.break_even_occupancy;
    const bepCell = bep == null || Number.isNaN(bep) ? '—'
      : bep > 1 ? `<span class="text-red">>100% (cannot CF)</span>`
      : `${(bep * 100).toFixed(0)}%`;
    return `
      <tr class="border-b border-line">
        <td class="py-2 pr-3 font-medium">${s.label}</td>
        <td class="py-2 pr-3 ${irrColor(s.irr)} text-right tabular-nums">${irrCell}</td>
        <td class="py-2 pr-3 ${cocColor(s.cash_on_cash)} text-right tabular-nums">${cocCell}</td>
        <td class="py-2 pr-3 ${dscrColor(s.dscr)} text-right tabular-nums">${dscrCell}</td>
        <td class="py-2 pr-3 text-right tabular-nums">${bepCell}</td>
        <td class="py-2 text-xs text-muted">${deltaChips || '—'}</td>
      </tr>`;
  }).join('');

  const reasonsList = d.gate.reasons.length
    ? `<ul class="text-sm list-disc pl-5 space-y-1">${d.gate.reasons.map(r => `<li>${r}</li>`).join('')}</ul>`
    : `<div class="text-sm text-muted">No threshold violations.</div>`;
  const mitigationsList = d.gate.mitigations.length
    ? `<ul class="text-sm list-disc pl-5 space-y-1">${d.gate.mitigations.map(m => `<li>${m}</li>`).join('')}</ul>`
    : '';

  const priceLine = d.price_to_green != null
    ? `<div class="bg-card border border-accent rounded p-4">
         <div class="text-xs uppercase tracking-wide text-muted mb-1">Walk-away price</div>
         <div class="text-2xl font-bold text-accent">$${d.price_to_green.toLocaleString()}</div>
         <div class="text-xs text-muted mt-1">Asking price that flips this deal to GREEN at current rent + rate.</div>
       </div>`
    : (verdict !== 'GREEN'
       ? `<div class="bg-card border border-red rounded p-4 text-sm text-red">
            No price between 30% and 100% of ask makes this GREEN. Rent / cost structure is broken; walk.
          </div>`
       : '');

  return `
    <div class="bg-card rounded border ${verdictColor.split(' ')[1]} p-5">
      <div class="flex items-baseline justify-between">
        <div>
          <div class="text-xs uppercase tracking-wide text-muted">Verdict</div>
          <div class="text-3xl font-bold ${verdictColor.split(' ')[0]}">${verdict}</div>
        </div>
        <div class="text-xs text-muted text-right">
          State: <span class="text-fg">${d.state || '—'}</span>
        </div>
      </div>
      ${overlayLine}
      ${climateLine}
    </div>
    ${climateBlock}
    ${postTaxBlock}

    <div class="bg-card rounded border border-line p-4 overflow-x-auto">
      <table class="w-full text-sm">
        <thead class="text-xs uppercase tracking-wide text-muted">
          <tr class="border-b border-line">
            <th class="text-left py-2 pr-3">Scenario</th>
            <th class="text-right py-2 pr-3">5y IRR</th>
            <th class="text-right py-2 pr-3">Y1 CoC</th>
            <th class="text-right py-2 pr-3">DSCR</th>
            <th class="text-right py-2 pr-3">Break-even occ.</th>
            <th class="text-left py-2">Deltas vs base</th>
          </tr>
        </thead>
        <tbody>${scenRows}</tbody>
      </table>
    </div>

    ${d.gate.reasons.length ? `
    <div class="bg-card rounded border border-line p-4">
      <div class="text-xs uppercase tracking-wide text-muted mb-2">Why it didn't clear</div>
      ${reasonsList}
    </div>` : ''}

    ${mitigationsList ? `
    <div class="bg-card rounded border border-line p-4">
      <div class="text-xs uppercase tracking-wide text-muted mb-2">Mitigations</div>
      ${mitigationsList}
    </div>` : ''}

    ${priceLine}
  `;
}

function renderPostTaxIRR(d, taxBracket) {
  // Approximate per-scenario post-tax IRR using the same shield formula
  // as portfolio.aggregate. Server doesn't ship this on /api/stress yet
  // because the stress endpoint doesn't take tax knobs — we compute it
  // client-side from inputs we already have.
  const a = d.assumptions || {};
  const price = a.purchase_price || 0;
  const rehab = a.rehab_cost || 0;
  const landAlloc = 0.20;
  const useful   = 27.5;
  const depr = ((price + rehab) * (1 - landAlloc)) / useful;
  const equity = price * (1 - (a.ltv || 0.75)) + price * 0.03 + rehab;
  const rows = d.scenarios.map(s => {
    const cf = s.cash_flow_y1 || 0;
    // Active deduction: tax_owed = (cf - depr) × bracket (can be negative)
    const taxOwed = (cf - depr) * taxBracket;
    const postCF = cf - taxOwed;
    const deltaCF = postCF - cf;
    const irrPre = s.irr;
    const irrPost = irrPre !== null && equity > 0 ? irrPre + deltaCF / equity : null;
    return `
      <tr class="border-b border-line">
        <td class="py-2 pr-3 font-medium">${s.label}</td>
        <td class="py-2 pr-3 text-right tabular-nums ${irrColor(irrPre)}">${fmtPct(irrPre)}</td>
        <td class="py-2 pr-3 text-right tabular-nums ${irrColor(irrPost)}">${fmtPct(irrPost)}</td>
        <td class="py-2 pr-3 text-right tabular-nums ${cf >= 0 ? 'text-green' : 'text-red'}">${(cf >= 0 ? '+' : '') + Math.round(cf).toLocaleString()}</td>
        <td class="py-2 pr-3 text-right tabular-nums ${postCF >= 0 ? 'text-green' : 'text-red'}">${(postCF >= 0 ? '+' : '') + Math.round(postCF).toLocaleString()}</td>
      </tr>`;
  }).join('');
  return `
    <div class="bg-card rounded border border-line p-4">
      <div class="text-xs uppercase tracking-wide text-muted mb-2">Post-tax view (${(taxBracket*100).toFixed(0)}% bracket, 20% land alloc, active deduction)</div>
      <div class="text-xs text-muted mb-2">Annual depreciation: $${Math.round(depr).toLocaleString()} · Equity: $${Math.round(equity).toLocaleString()}. <span class="text-muted">For passive-loss limited investors, post-tax IRR is closer to pre-tax (loss suspends).</span></div>
      <table class="w-full text-sm">
        <thead class="text-xs uppercase tracking-wide text-muted">
          <tr class="border-b border-line">
            <th class="text-left py-2 pr-3">Scenario</th>
            <th class="text-right py-2 pr-3">IRR pre-tax</th>
            <th class="text-right py-2 pr-3">IRR post-tax</th>
            <th class="text-right py-2 pr-3">CF/yr pre</th>
            <th class="text-right py-2 pr-3">CF/yr post</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}

function fmtPct(x) {
  if (x == null || Number.isNaN(x)) return '—';
  if (x <= -0.99) return '< -99%';
  return (x * 100).toFixed(1) + '%';
}
function irrColor(x) {
  if (x == null) return '';
  if (x >= 0.10) return 'text-green';
  if (x >= 0) return 'text-fg';
  if (x >= -0.05) return 'text-yellow';
  return 'text-red';
}
function cocColor(x) {
  if (x == null) return '';
  if (x >= 0.06) return 'text-green';
  if (x >= 0.03) return 'text-yellow';
  return 'text-red';
}
function dscrColor(x) {
  if (x == null) return '';
  if (x >= 1.25) return 'text-green';
  if (x >= 1.10) return 'text-yellow';
  return 'text-red';
}

function initPipelineHandlers() {
  // Filter pills
  document.querySelectorAll('.pipe-filter').forEach(btn => {
    btn.addEventListener('click', () => {
      PIPE_FILTER = btn.dataset.status;
      document.querySelectorAll('.pipe-filter').forEach(b => {
        b.classList.remove('border-accent', 'text-accent');
        b.classList.add('border-line', 'text-muted');
      });
      btn.classList.add('border-accent', 'text-accent');
      btn.classList.remove('border-line', 'text-muted');
      renderPipeline();
    });
  });
  // Compare / Clear
  document.getElementById('pipeCompareBtn')?.addEventListener('click', openPipeCompare);
  document.getElementById('pipeClearAll')?.addEventListener('click', () => {
    clearAllDeals();
    renderPipeline();
  });
  // Detail-modal buttons
  document.getElementById('pipeDetailRerun')?.addEventListener('click', rerunCurrentDeal);
  document.getElementById('pipeDetailDelete')?.addEventListener('click', deleteCurrentDeal);
  // Close detail by clicking the backdrop (but not the inner card)
  document.getElementById('pipeDetailModal')?.addEventListener('click', (e) => {
    if (e.target.id === 'pipeDetailModal') closePipeDetail();
  });
  document.getElementById('pipeCompareModal')?.addEventListener('click', (e) => {
    if (e.target.id === 'pipeCompareModal') closePipeCompare();
  });
  // Esc closes whatever modal is open
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      if (!document.getElementById('pipeDetailModal').classList.contains('hidden')) closePipeDetail();
      else if (!document.getElementById('pipeCompareModal').classList.contains('hidden')) closePipeCompare();
      else if (!document.getElementById('buyboxModal').classList.contains('hidden')) closeBuyBox();
    }
  });
  // Buy-box modal: backdrop-click to close + "Stress-test typical deal"
  document.getElementById('buyboxModal')?.addEventListener('click', (e) => {
    if (e.target.id === 'buyboxModal') closeBuyBox();
  });
  document.getElementById('buyboxRunStress')?.addEventListener('click', stressFromBuyBox);
}

// Boot
document.addEventListener('DOMContentLoaded', () => {
  renderUwForm();
  $('uwSubmit').addEventListener('click', runUnderwrite);
  $('linkSubmit').addEventListener('click', ingestLink);
  $('linkInput').addEventListener('keydown', (e) => { if (e.key === 'Enter') ingestLink(); });
  loadDeals();
  updatePipelineCount();
  initPipelineHandlers();
  go('dashboard');
});

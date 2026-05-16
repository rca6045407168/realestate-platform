"""Conversational deal-research surface ("Ask reip").

Architecture: single-call agentic Q&A. The user asks anything ("which
Memphis zips are still expanding?", "underwrite a $250k Cleveland duplex
at 7% / 75% LTV", "show me cold-AVM zips in the Midwest"). The agent
decomposes the question, calls one or more REIP tools, and writes the
answer.

Latent-RAG spirit, not letter: we don't train models, so we can't do the
paper's joint encoder/retriever alignment. What we CAN do is minimize
agentic loops by pre-loading the most retrieval-likely context into the
system prompt — current top-10 MSAs, current top-10 zips, framework
summary, recommendation-gate thresholds, the 11 verified live-listing
markets. Most questions can then be answered in one Claude call with
zero tool use; only specific lookups (a particular zip, a per-property
underwriting) need a tool invocation.

Tools mirror existing /api/* endpoints but call internal modules
directly (no HTTP round-trip).

Requires ANTHROPIC_API_KEY in the environment.
"""
from __future__ import annotations
import json
import os
from typing import Any, Optional
from .store import connect
from . import (
    msa_score, zip_returns, listings_search, underwriting,
    remarks as remarks_mod, avm as avm_mod, recommendation as rec_mod,
    projection as proj_mod, decision as decision_mod,
)

try:
    import anthropic
except ImportError:
    anthropic = None


MODEL = "claude-sonnet-4-5"
# Fallback when the primary model 429s (Claude Code Max shares quota with
# the user's active Claude Code session). Haiku has its own bucket on the
# Max plan and is plenty for tool-use orchestration.
FALLBACK_MODEL = "claude-haiku-4-5"


def _get_claude_code_oauth_token() -> str | None:
    """Read the Claude Code OAuth access token from macOS keychain.

    Claude Code stores credentials as a JSON blob under the generic-password
    entry 'Claude Code-credentials'. The shape is:
      {"claudeAiOauth": {"accessToken": "sk-ant-o…", "refreshToken": ..., ...}}

    Returns the access token string, or None if unavailable / non-macOS.
    Note: tokens expire; refresh is not implemented here — if Claude Code
    has run recently the token will be fresh.
    """
    import subprocess, json, sys
    if sys.platform != "darwin":
        return None
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            return None
        blob = json.loads((r.stdout or "").strip())
        token = ((blob.get("claudeAiOauth") or {}).get("accessToken") or "").strip()
        return token or None
    except Exception:
        return None

# Tool schemas — JSON Schema per Anthropic's tool-use spec.
TOOLS = [
    {
        "name": "top_zips",
        "description": (
            "Return top US ZIPs ranked by regime-adjusted 5y IRR (default) or another sort key. "
            "Use this for 'best zips' / 'top markets at the zip level' / 'where should I buy' questions. "
            "Returns a JSON list with zip, state, cbsa_name, regime_label, regime_adjusted_irr, irr_5y, "
            "typical_price, typical_rent, chg_12mo, rent_chg_12mo, cap_rate_y1, dscr_y1."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "state":     {"type": "string", "description": "2-letter state code (e.g. 'CA')"},
                "cbsa":      {"type": "string", "description": "CBSA code (e.g. '32820' for Memphis)"},
                "sort":      {"type": "string", "enum": ["regime", "irr", "total_return", "cashflow", "appreciation", "yield"], "default": "regime"},
                "limit":     {"type": "integer", "default": 10},
                "min_price": {"type": "integer", "default": 50000},
                "max_price": {"type": "integer", "default": 800000},
            },
        },
    },
    {
        "name": "top_msas",
        "description": (
            "Return top US MSAs ranked by the framework's blended Appreciation × Cashflow × Risk score. "
            "Use for 'which metros' / 'best markets' / archetype questions. "
            "Returns cbsa_code, cbsa_name, archetype, pop, pop_cagr_5yr, emp_cagr_5yr, gross_yield, "
            "appreciation_score, cashflow_score, total_return_score."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "archetype": {"type": "string", "enum": ["Coastal Gateway", "Sun Belt Growth", "Cashflow Heartland", "Boom-Bust Beta", "Resource & Niche", "Mixed"]},
                "sort_by":   {"type": "string", "enum": ["total", "appreciation", "cashflow"], "default": "total"},
                "min_pop":   {"type": "integer", "default": 250000},
                "limit":     {"type": "integer", "default": 10},
            },
        },
    },
    {
        "name": "msa_detail",
        "description": "Full breakdown for one MSA: every factor, percentile ranks, archetype, region info.",
        "input_schema": {
            "type": "object",
            "properties": {"cbsa_code": {"type": "string"}},
            "required": ["cbsa_code"],
        },
    },
    {
        "name": "live_listings",
        "description": (
            "Live Redfin listings for a verified market (one of 11 wired metros). Returns active for-sale "
            "properties with verdict (GREEN/YELLOW/RED), 5y projections, rec-gate reasons, decision "
            "rationale. Use for 'show me actual properties to buy in <verified metro>'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cbsa":  {"type": "string", "description": "CBSA code or 'all' for cross-market top picks"},
                "limit": {"type": "integer", "default": 5},
                "min_price": {"type": "integer", "default": 50000},
                "max_price": {"type": "integer", "default": 500000},
                "sort":  {"type": "string", "enum": ["irr", "total_return", "cashflow", "appreciation"], "default": "irr"},
            },
            "required": ["cbsa"],
        },
    },
    {
        "name": "underwrite",
        "description": (
            "Underwrite a single hypothetical or actual deal. Returns year-1 pro forma (NOI, cap rate, "
            "DSCR, cash-on-cash), BRRRR refi math if ARV given, 5y IRR + equity multiple, and the "
            "recommendation-gate verdict with reasons + required mitigations."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "purchase_price":   {"type": "number"},
                "monthly_rent":     {"type": "number"},
                "rehab_cost":       {"type": "number", "default": 0},
                "arv":              {"type": "number"},
                "mortgage_rate":    {"type": "number", "default": 0.07},
                "ltv":              {"type": "number", "default": 0.75},
                "vacancy":          {"type": "number", "default": 0.05},
                "hold_years":       {"type": "integer", "default": 5},
            },
            "required": ["purchase_price", "monthly_rent"],
        },
    },
    {
        "name": "avm_zips",
        "description": "Zips where Redfin sales are clearing meaningfully above (hot) or below (cold) Zillow's smoothed ZHVI — Information-alpha signal per the framework's §5.7.",
        "input_schema": {
            "type": "object",
            "properties": {
                "direction": {"type": "string", "enum": ["cold", "hot", "all"], "default": "cold"},
                "limit":     {"type": "integer", "default": 10},
            },
        },
    },
    {
        "name": "parse_remarks",
        "description": "Parse MLS public-remarks text and return the 8 alpha flags (motivated, distressed, use_change, assumable, price_cut, short_sale, probate, auction) with matched terms. The `auction` flag catches REO / bank-owned / foreclosure / trustee sale / sheriff sale / HUD-owned / online-auction language — surface this when the user is hunting for distressed/auction deals.",
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "current_rates",
        "description": (
            "Get TODAY's macro rates: 30-year fixed mortgage rate (Freddie Mac PMMS), "
            "10-year Treasury yield, effective fed funds rate. Use when the user asks "
            "'what are rates today', 'where are mortgage rates', or any question where "
            "current rates ground the discussion (e.g. 'is 7% conservative or optimistic?')."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "portfolio_resilience",
        "description": (
            "Compute the historical resilience score (0-100) of the user's CURRENT pipeline, "
            "based on FHFA HPI 1985-now. Returns equity-weighted historical max drawdown, "
            "recovery time, tier distribution (Boring/Standard/Volatile/Boom-Bust), and a "
            "comparison vs the All-Weather benchmark. Use when the user asks 'how resilient "
            "is my portfolio', 'what would my portfolio have done in 2008', or 'should I "
            "diversify away from <state>'. Reads the pipeline_summary already in the system prompt — "
            "you don't need to pass deals."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "strategy_backtest",
        "description": (
            "Run the 50-year empirical strategy analyses live against current FHFA HPI + "
            "Zillow ZORI data. Returns one of: 'regimes' (8 housing cycles 1985-2024 with "
            "best/worst metro per regime), 'drawdowns' (worst 15 + shallowest 15 metros), "
            "'momentum' (3y→3y quartile transition matrix, 15K transition test), "
            "'strategies' (34-year backtest of 4 archetype portfolios), or 'rent_yield' "
            "(top metros by total return 2015-2024). Use when the user asks 'what's worked "
            "historically', 'show me the worst drawdowns', 'is momentum real', 'how have "
            "the archetypes performed', or any question that needs empirical evidence from "
            "the long historical record. The full report is also documented in docs/STRATEGY.md."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "section": {
                    "type": "string",
                    "enum": ["regimes", "drawdowns", "momentum", "strategies", "rent_yield"],
                    "description": "Which analysis to fetch. Pick the most relevant one to the user's question.",
                },
            },
            "required": ["section"],
        },
    },
    {
        "name": "buy_box",
        "description": (
            "Derive a buy-box for a specific US zip code: target price band (80%–110% "
            "of ZHVI), target rent band (90%–110% of ZORI), light/heavy rehab bands, "
            "trend-projected ARV, target cap rate, regime context, and a 'typical_deal' "
            "object you can immediately stress-test. Use when the user asks 'what should "
            "I be paying in <zip>', 'what's my buy box for <zip>', 'what's a typical deal "
            "in <zip>', or before recommending live listings in a zip."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "zip": {"type": "string", "description": "5-digit US zip code"},
            },
            "required": ["zip"],
        },
    },
    {
        "name": "record_decision",
        "description": (
            "Record Richard's verdict on a specific zip / deal so future chat sessions see it "
            "as fast-weight context. Use when Richard says 'I'd buy this', 'I'd pass on this', "
            "'this looks interesting, watching', or otherwise reveals a preference about a zip "
            "or listing. `verdict` must be BUY / PASS / WATCH. `reason` is 1-2 sentences in "
            "Richard's voice — the rationale that future sessions will read. Optional `action`, "
            "`price`, `state`, `msa`, `verdict_gate` (GREEN/YELLOW/RED from stress_test) for "
            "context. Do NOT invent decisions — only call this when the user has actually "
            "expressed a verdict."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "zip":          {"type": "string", "description": "5-digit US zip code"},
                "verdict":      {"type": "string", "enum": ["BUY", "PASS", "WATCH"]},
                "reason":       {"type": "string", "description": "1-2 sentences explaining the call"},
                "action":       {"type": "string", "description": "Optional concrete next step (e.g. 'pull comps', 'offer at $80k')"},
                "price":        {"type": "number", "description": "Optional purchase price the verdict was on"},
                "state":        {"type": "string", "description": "Optional 2-letter state code"},
                "msa":          {"type": "string", "description": "Optional CBSA code or name"},
                "verdict_gate": {"type": "string", "enum": ["GREEN", "YELLOW", "RED"], "description": "Optional gate verdict from stress_test"},
            },
            "required": ["zip", "verdict", "reason"],
        },
    },
    {
        "name": "recent_decisions",
        "description": (
            "Read back Richard's most recent recorded verdicts (BUY/PASS/WATCH). Use when he "
            "asks 'what have I been looking at', 'what zips am I tracking', 'what did I pass on', "
            "or before recommending something to avoid re-pitching a deal he already passed on. "
            "Returns at most `limit` records, newest first. The same data is also auto-loaded "
            "into the system prompt at chat init, so you usually don't need to call this — "
            "only call it explicitly when the user asks about decision history."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 10, "description": "Max records to return (1-50)"},
            },
        },
    },
    {
        "name": "vault_search",
        "description": (
            "Search Richard's Obsidian vault for markdown notes matching a query. "
            "Use this when he asks 'what did I write about X', 'remind me about <topic>', "
            "or any question that wants content from his personal knowledge base rather "
            "than the live data tools. The Knowledge/ folder is already auto-loaded into "
            "the system prompt — only call vault_search when the question targets vault "
            "content OUTSIDE that folder (sessions, daily notes, other project briefs) or "
            "when you need a specific quote/excerpt. Returns up to `limit` hits as "
            "{path, line, excerpt}. Slim by design — never dump full notes back to the user."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Case-insensitive substring to find"},
                "limit": {"type": "integer", "default": 5, "description": "Max hits (1-20)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "stress_test",
        "description": (
            "Multi-scenario underwriter. Runs base / stress / worst-case on a deal "
            "with state-aware overlays (FL hurricane-insurance, TX high property tax, "
            "CA rent-cap drag, rust-belt rehab overrun). Returns each scenario's IRR, "
            "CoC, DSCR, break-even occupancy + a GREEN/YELLOW/RED gate with concrete "
            "mitigations + `price_to_green` (the price ceiling that lifts the deal to "
            "GREEN). Use whenever the user asks 'is this deal good', 'underwrite this', "
            "'what could go wrong', or pastes a listing with numbers."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "purchase_price":     {"type": "number"},
                "monthly_rent":       {"type": "number"},
                "rehab_cost":         {"type": "number", "default": 0},
                "arv":                {"type": "number"},
                "mortgage_rate":      {"type": "number", "default": 0.07},
                "ltv":                {"type": "number", "default": 0.75},
                "vacancy":            {"type": "number", "default": 0.05},
                "property_tax_rate":  {"type": "number", "default": 0.012},
                "insurance_annual":   {"type": "number", "default": 1500},
                "hoa_monthly":        {"type": "number", "default": 0},
                "state":              {"type": "string", "description": "2-letter state code — drives state overlay (FL/TX/CA/OH/MI/...)"},
                "zip":                {"type": "string", "description": "5-digit ZIP — enables climate amplification (FEMA NFIP-driven)"},
            },
            "required": ["purchase_price", "monthly_rent"],
        },
    },
]


# ---------------------------------------------------------------------------
# Tool execution — direct calls into existing modules.
# ---------------------------------------------------------------------------

def _execute(name: str, args: dict) -> Any:
    con = connect()

    if name == "top_zips":
        # Pull archetypes for the overlay
        archetypes = {}
        raw = msa_score.features(con)
        if not raw.empty:
            scored = msa_score.with_archetype(msa_score.score(raw))
            archetypes = dict(zip(scored["cbsa_code"].astype(str), scored["archetype"]))
        rows = zip_returns.rank_us(
            con,
            min_price=args.get("min_price", 50_000),
            max_price=args.get("max_price", 800_000),
            sort=args.get("sort", "regime"),
            state=args.get("state"),
            cbsa_code=args.get("cbsa"),
            limit=args.get("limit", 10),
            archetypes_by_cbsa=archetypes,
        )
        # Token-cost trim: ZipReturn has ~25 fields; the model only needs
        # the essential 8 to answer "best zips" / "where to buy" questions.
        # Saves ~70% of returned JSON, ~$0.005/tool-call on multi-iter loops.
        ESSENTIAL = ("zip", "state", "cbsa_name", "regime_label",
                      "regime_adjusted_irr", "irr_5y",
                      "typical_price", "typical_rent",
                      "cap_rate_y1", "dscr_y1")
        return [{k: getattr(z, k, None) for k in ESSENTIAL if getattr(z, k, None) is not None}
                 for z in rows]

    if name == "top_msas":
        raw = msa_score.features(con)
        if raw.empty:
            return []
        scored = msa_score.with_archetype(msa_score.score(raw))
        if args.get("archetype"):
            scored = scored[scored["archetype"] == args["archetype"]]
        sort_col = {"total": "total_return_score", "appreciation": "appreciation_score",
                    "cashflow": "cashflow_score"}[args.get("sort_by", "total")]
        scored = scored[scored["pop"] >= args.get("min_pop", 250_000)]
        scored = scored.sort_values(sort_col, ascending=False).head(args.get("limit", 10))
        # Trim from 12 to 7 essentials. The model rarely cites the 5 dropped
        # fields (emp_cagr_5yr, income_cagr_5yr, net_migration_pct_pop,
        # appreciation_score, cashflow_score) in chat answers.
        keep = ["cbsa_code", "cbsa_name", "archetype", "pop",
                "pop_cagr_5yr", "gross_yield", "total_return_score"]
        cols = [c for c in keep if c in scored.columns]
        return scored[cols].to_dict("records")

    if name == "msa_detail":
        raw = msa_score.features(con)
        if raw.empty:
            return {"error": "No MSA features. Run ingest first."}
        scored = msa_score.with_archetype(msa_score.score(raw))
        m = scored[scored["cbsa_code"].astype(str) == str(args["cbsa_code"])]
        if m.empty:
            return {"error": f"No MSA {args['cbsa_code']}"}
        # msa_detail has ~40 fields; trim to the meaningful 12 for chat.
        row = m.iloc[0].to_dict()
        keep = ("cbsa_code", "cbsa_name", "archetype", "pop", "pop_cagr_5yr",
                "emp_cagr_5yr", "income_cagr_5yr", "net_migration_pct_pop",
                "gross_yield", "permits_per_1000_hh",
                "appreciation_score", "cashflow_score", "total_return_score")
        return {k: row.get(k) for k in keep if row.get(k) is not None}

    if name == "live_listings":
        # Reuse the same path the API uses
        from .api import _score_one_market, _all_markets
        cbsa = args["cbsa"]
        if str(cbsa).lower() == "all":
            return _all_markets(
                None, args.get("limit", 5),
                args.get("min_price", 50_000), args.get("max_price", 500_000),
                None, None, None, None,
                0.07, 0.75, args.get("sort", "irr"),
            )
        bundle = _score_one_market(cbsa, args.get("min_price", 50_000),
                                    args.get("max_price", 500_000), 0.07, 0.75)
        return {"market": listings_search.MARKETS.get(cbsa, {}).get("name", cbsa),
                "results": [e["output"] for e in (bundle.get("listings") or [])][:args.get("limit", 5)]}

    if name == "underwrite":
        a = underwriting.Assumptions(
            purchase_price=args["purchase_price"],
            rehab_cost=args.get("rehab_cost", 0),
            arv=args.get("arv"),
            monthly_rent=args["monthly_rent"],
            mortgage_rate=args.get("mortgage_rate", 0.07),
            ltv=args.get("ltv", 0.75),
            vacancy=args.get("vacancy", 0.05),
            hold_years=args.get("hold_years", 5),
        )
        out = underwriting.underwrite(a)
        # Add rec gate
        deal = rec_mod.DealUnderwriting(
            stabilized_dscr=out["proforma_y1"]["dscr"],
            stress_coc_on_residual=out["proforma_y1"]["cash_on_cash"] * 0.7,
            sensitivity_negative_cashflow=out["proforma_y1"]["dscr"] < 1.0,
        )
        out["recommendation"] = rec_mod.classify(deal).to_dict()
        return out

    if name == "avm_zips":
        avm_mod.persist(con)
        rows = con.execute(
            "SELECT * FROM zip_avm_signal WHERE direction = ? ORDER BY divergence_z "
            + ("ASC" if args.get("direction", "cold") == "cold" else "DESC")
            + " LIMIT ?",
            [args.get("direction", "cold"), args.get("limit", 10)],
        ).df()
        return rows.to_dict("records")

    if name == "current_rates":
        out = {}
        for sid, label in [("MORTGAGE30US", "mortgage_30y"),
                            ("DGS10", "treasury_10y"),
                            ("FEDFUNDS", "fed_funds")]:
            r = con.execute(
                "SELECT period, value FROM fred_macro WHERE series_id = ? "
                "ORDER BY period DESC LIMIT 1", [sid]
            ).fetchone()
            if r:
                out[label] = {"value": float(r[1]), "as_of": str(r[0])}
        return out

    if name == "portfolio_resilience":
        # The chat orchestrator passes pipeline_summary as a parameter via
        # closure. We pull it from the executor's pipeline list captured at
        # call-time. Since `_execute` doesn't take a pipeline arg, we use
        # a module-level placeholder that chat() sets before invoking tools.
        global _CURRENT_PIPELINE
        deals = _CURRENT_PIPELINE or []
        if not deals:
            return {"error": "No saved deals in pipeline; nothing to score."}
        # The compact summary the client sends doesn't have the full inputs
        # structure that portfolio_resilience() expects. Reshape it.
        reshaped = [
            {
                "label": d.get("label"),
                "inputs": {
                    "purchase_price": d.get("purchase_price"),
                    "monthly_rent": d.get("monthly_rent"),
                    "ltv": 0.75,
                    "rehab_cost": 0,
                    "state": d.get("state"),
                    "zip": d.get("zip"),
                },
            }
            for d in deals
        ]
        from . import strategy as strat
        return strat.portfolio_resilience(con, reshaped)

    if name == "strategy_backtest":
        from . import strategy as strat
        section = args["section"]
        fn = {
            "regimes":    strat.regime_decomposition,
            "drawdowns":  strat.drawdown_panel,
            "momentum":   strat.momentum_persistence,
            "strategies": strat.strategy_backtest,
            "rent_yield": strat.rent_yield_panel,
        }[section]
        return {section: fn(con)}

    if name == "buy_box":
        from . import buybox as buybox_mod
        b = buybox_mod.derive(con, args["zip"])
        if not b:
            return {"error": f"No ZHVI/ZORI data for zip {args['zip']}"}
        return buybox_mod.to_dict(b)

    if name == "stress_test":
        from . import stress as stress_mod
        from . import climate as climate_mod
        a = underwriting.Assumptions(
            purchase_price=args["purchase_price"],
            rehab_cost=args.get("rehab_cost", 0),
            arv=args.get("arv"),
            monthly_rent=args["monthly_rent"],
            mortgage_rate=args.get("mortgage_rate", 0.07),
            ltv=args.get("ltv", 0.75),
            vacancy=args.get("vacancy", 0.05),
            property_tax_rate=args.get("property_tax_rate", 0.012),
            insurance_annual=args.get("insurance_annual", 1500.0),
            hoa_monthly=args.get("hoa_monthly", 0.0),
        )
        # Auto-score climate if a zip was provided
        climate_dict = None
        if args.get("zip"):
            cs = climate_mod.score_zip(con, args["zip"], args.get("state"))
            if cs:
                climate_dict = climate_mod.to_dict(cs)
        out = stress_mod.stress_test(a, state=args.get("state"),
                                       climate_score=climate_dict)
        if climate_dict:
            out["climate"] = climate_dict
        return out

    if name == "record_decision":
        from . import decision_ledger
        return decision_ledger.append(
            zip_code=args.get("zip", ""),
            verdict=args.get("verdict", ""),
            reason=args.get("reason", ""),
            action=args.get("action"),
            extra={
                "price": args.get("price"),
                "state": args.get("state"),
                "msa": args.get("msa"),
                "verdict_gate": args.get("verdict_gate"),
            },
        )

    if name == "recent_decisions":
        from . import decision_ledger
        n = args.get("limit", 10)
        try:
            n = max(1, min(int(n), 50))
        except (TypeError, ValueError):
            n = 10
        return decision_ledger.recent(n)

    if name == "vault_search":
        from . import vault_knowledge
        return vault_knowledge.search(args.get("query", ""), args.get("limit", 5))

    if name == "parse_remarks":
        s = remarks_mod.parse(args["text"])
        return {
            "motivated": s.motivated, "distressed": s.distressed,
            "use_change": s.use_change, "assumable": s.assumable,
            "price_cut": s.price_cut, "short_sale": s.short_sale, "probate": s.probate,
            "auction":   s.auction,
            "score": round(s.score, 3),
            "matched_terms": list(s.matched_terms),
        }

    return {"error": f"unknown tool: {name}"}


# ---------------------------------------------------------------------------
# Context preload — LatentRAG spirit. Front-load the dataset summary into
# the system prompt so the model answers many questions in one shot.
# ---------------------------------------------------------------------------

def _build_context() -> str:
    """Return a compact summary of REIP state that fits in ~2k tokens.
    Refreshed at chat-init time so the model sees current rankings."""
    parts = ["Current REIP state (refreshed each chat)."]
    try:
        con = connect()
        # Top 10 MSAs by total return
        raw = msa_score.features(con)
        if not raw.empty:
            scored = msa_score.with_archetype(msa_score.score(raw))
            top_msas = scored.sort_values("total_return_score", ascending=False).head(10)
            parts.append("\n## Top 10 MSAs by blended total return score:")
            for _, r in top_msas.iterrows():
                parts.append(
                    f"  - {r['cbsa_code']} {r['cbsa_name']} ({r['archetype']}) "
                    f"pop={r.get('pop', 0):,.0f} popCAGR={(r.get('pop_cagr_5yr') or 0)*100:+.1f}% "
                    f"yield={(r.get('gross_yield') or 0)*100:.1f}% total={r.get('total_return_score', 0):+.3f}"
                )

        # Top 10 zips by regime-adjusted IRR
        archetypes = (dict(zip(scored["cbsa_code"].astype(str), scored["archetype"]))
                      if not raw.empty else {})
        top_zips = zip_returns.rank_us(con, sort="regime", limit=10,
                                       min_price=80_000, max_price=400_000,
                                       archetypes_by_cbsa=archetypes)
        if top_zips:
            parts.append("\n## Top 10 zips by regime-adjusted 5y IRR:")
            for z in top_zips:
                parts.append(
                    f"  - {z.zip} ({z.state}, {z.cbsa_name}): regime={z.regime_label} "
                    f"adjIRR={z.regime_adjusted_irr*100:+.1f}% rawIRR={z.irr_5y*100:+.1f}% "
                    f"price=${z.typical_price:,.0f} rent=${z.typical_rent:,.0f} "
                    f"price12mo={z.chg_12mo*100:+.1f}% rent12mo={z.rent_chg_12mo*100:+.1f}%"
                )

        # Verified live-listing markets
        parts.append("\n## 11 markets wired for live Redfin listings (CBSA code → name):")
        for cbsa, m in listings_search.MARKETS.items():
            parts.append(f"  - {cbsa}: {m['name']} (archetype={m.get('archetype_hint', 'Mixed')})")

    except Exception as e:
        parts.append(f"\n## Context preload partial — {type(e).__name__}: {e}")

    # Fast-weight context: Richard's recent BUY/PASS/WATCH verdicts.
    # Read at chat init so it sits inside the cached system prefix —
    # cheap on cache reads, refreshes session-to-session. Empty for new
    # users keeps the prompt identical (stable cache key).
    try:
        from . import decision_ledger
        decisions_block = decision_ledger.render_context_block(limit=10)
        if decisions_block:
            parts.append(decisions_block)
    except Exception as e:
        parts.append(f"\n## Decision ledger unavailable — {type(e).__name__}: {e}")

    # Obsidian knowledge — any markdown Richard drops into
    # ~/Documents/Obsidian Vault/Real Estate Platform/Knowledge/*.md gets
    # auto-loaded into the cached system prefix. Authoring lives in the
    # vault; the platform reads from disk.
    try:
        from . import vault_knowledge
        knowledge_block = vault_knowledge.load_knowledge_block()
        if knowledge_block:
            parts.append(knowledge_block)
    except Exception as e:
        parts.append(f"\n## Vault knowledge unavailable — {type(e).__name__}: {e}")

    return "\n".join(parts)


SYSTEM_PROMPT = """You are reip's investment-research assistant. You help an active real-estate investor decide where and what to buy.

## Framework you reason with

Total return = current yield (cap rate × LTV math) + appreciation (zip ZHVI projection) + equity paydown + tax shield. Every market is a blend of yield/growth/risk.

Five archetypes (named in the partner memo):
- Coastal Gateway (SF, NY, LA, Boston, DC, SJ): yield 3-5%, appreciation-led, negative carry, regulatory risk
- Sun Belt Growth (Austin, Dallas, Houston, Miami, Phoenix, Raleigh, Charlotte): yield 5-7%, hybrid, best risk-adjusted total return
- Cashflow Heartland (Memphis, Indianapolis, Cleveland, Pittsburgh, Detroit, KC): yield 7-10%, current-yield thesis, capex risk, low growth
- Boom-Bust Beta (Las Vegas, Phoenix late-cycle, Riverside, Cape Coral, Reno): high cycle amplitude
- Resource & Niche (Boise, Bozeman, Bend, Midland, college towns, STR markets): idiosyncratic

Recommendation gate is the moral center. GREEN requires DSCR ≥ 1.30×, refi appraisal stress passes ≥70% LTV, insurance trend ≤+20%, climate <75th percentile, alpha-stack ≥2 flags, stress CoC ≥8%. RED on hard failures (DSCR <1.10×, top-decile climate, sensitivity → negative CF). YELLOW = RED-failures-with-verified-mitigations.

## Honesty rules
- Don't invent numbers. Use tools to look up current data.
- Florida and AZ/NV are weakening 2024-2026; the regime-adjusted ranking accounts for this. Mention it when relevant.
- Live property listings are gated to the 11 wired metros. For any other metro, use top_zips and direct the user to the Redfin/Zillow zip URL.
- If a deal screens RED, say RED, even if it's close. The gate doesn't soften.

## Response style
- Plain English, investor-grade. No marketing copy.
- Numbers come with units and provenance.
- When you pull data, summarize what it means; don't dump JSON.
- Short paragraphs and tight bullets. No long preambles.

## Response length — calibrate to the question
- Direct factual question ("what's today's rate", "what's my pipeline score") → 1-3 sentences. No headers, no padding.
- Single-market analysis ("is Memphis good", "stress this deal") → ≤200 words, one table if needed, action line.
- Comparative / strategic ("compare these 3 metros", "what's a good strategy") → full markdown, ≤400 words.
- Never explain methodology unless asked. The investor knows what an IRR is.

## Tools

You have tools for: top_zips, top_msas, msa_detail, live_listings, underwrite, avm_zips, parse_remarks, buy_box, stress_test, strategy_backtest, record_decision, recent_decisions, vault_search. Use them when the user asks for specific data; if you can answer from the pre-loaded context below, do that and skip tool use.

When Richard expresses a verdict on a zip or deal — "I'd buy this", "pass", "watching this one" — call `record_decision` with the zip, verdict (BUY/PASS/WATCH), and his rationale in 1-2 sentences. These verdicts become fast-weight context next session. Don't fabricate verdicts; only record when he's actually expressed one.

His Obsidian Knowledge/ folder is already auto-loaded below. Only call `vault_search` for content OUTSIDE that folder (other project briefs, session notes, daily notes) or when you need a specific quoted excerpt from a known note.

Use `strategy_backtest` when the user asks for empirical historical evidence — e.g. "what's the worst drawdown ever in X market", "show me the regime decomposition", "is momentum real in real estate", "how did Sun Belt compare to CA Coastal over 30 years". Pick the right section.

## Empirical strategy defaults (from 50-year FHFA HPI + ZORI backtests)

These are derived from real data on 410 metros 1975-2024 (see docs/STRATEGY.md):

- **Geography matters more than timing.** Within any single regime the spread between best/worst metro is 8-12 percentage points per year. Long-run national CAGRs are 3-5%; regime spreads are 10×+ that.
- **Median worst-drawdown across metros is -16.4%, median time-to-recover is 9.8 years.** Worst-decile metros (Merced, Vegas, Modesto, Stockton, Cape Coral, Reno) saw -50% to -65% drawdowns. Best-decile (Pittsburgh, Buffalo, Rochester NY, Iowa metros) saw -2% to -4%.
- **3-year momentum is real:** top-quartile-past-3y stays top-half 64% of the time; forward 3y mean return is +18% for Q1 vs +10.6% for Q4 (7.4pp spread). Do not contrarian-trade real estate.
- **34-year backtest CAGRs** (buy 1990, hold 2024): Sun Belt Growth +5.09% / DD -26%, All-Weather Lifestyle +4.80% / DD -18%, CA Coastal +4.66% / DD -36% (worse risk-adjusted than Sun Belt), Heartland Yield +3.75% / DD -10%.
- **Yield ≠ no-growth:** correlation of 2024 yield vs 9y appreciation is only -0.26. Rockford IL, Youngstown OH, Flint MI, Toledo OH, Fort Wayne IN delivered both 8-9% yields AND 100%+ appreciation 2015-2024.
- **Recommended allocation**: 40% Sun Belt Growth (momentum-screened, drop top decile), 30% All-Weather lifestyle, 20% Cashflow Heartland, 10% speculative. Climate-severe (NFIP ≥75) capped at 10%. Hold ≥10 years.

When the user asks "what's a good real estate strategy" or "where should I buy", reference these empirical findings rather than generic advice. Cite the doc when helpful.
"""


# ---------------------------------------------------------------------------
# Chat orchestrator
# ---------------------------------------------------------------------------

# Set by chat() before tool execution. Read by the portfolio_resilience
# tool. Module-level handoff because the tool input_schema is intentionally
# empty (the LLM shouldn't have to re-pass the pipeline that's already in
# the system prompt).
_CURRENT_PIPELINE: Optional[list[dict]] = None


def _format_pipeline_block(deals: list[dict]) -> str:
    """Render a client-supplied pipeline list into a system-prompt block.

    Inputs each look like:
      {label, status, verdict, purchase_price, monthly_rent, state,
       price_to_green, base_irr, worst_irr, notes}
    """
    if not deals:
        return ""
    lines = ["\n## Your saved deals (from the user's pipeline — they may ask follow-ups about any):"]
    for d in deals[:12]:                 # cap to keep token usage bounded
        bits = [f"  - **{d.get('label','?')}** [{d.get('status','?')}] verdict={d.get('verdict','?')}"]
        if d.get("purchase_price"):
            bits.append(f"price=${int(d['purchase_price']):,}")
        if d.get("monthly_rent"):
            bits.append(f"rent=${int(d['monthly_rent']):,}/mo")
        if d.get("state"):
            bits.append(f"state={d['state']}")
        if d.get("base_irr") is not None:
            bits.append(f"baseIRR={d['base_irr']*100:+.1f}%")
        if d.get("worst_irr") is not None:
            bits.append(f"worstIRR={d['worst_irr']*100:+.1f}%")
        if d.get("price_to_green"):
            bits.append(f"walk-away=${int(d['price_to_green']):,}")
        lines.append(" ".join(bits))
        if d.get("notes"):
            lines.append(f"      notes: {d['notes'][:200]}")
    return "\n".join(lines)


def _today_spend_usd() -> float:
    """Sum est_cost_usd from chat_usage.jsonl for today (UTC)."""
    from pathlib import Path
    import datetime
    log = Path.home() / ".reip" / "chat_usage.jsonl"
    if not log.exists():
        return 0.0
    today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    total = 0.0
    try:
        for line in log.read_text().splitlines():
            if today in line:
                try:
                    r = json.loads(line)
                    total += float(r.get("est_cost_usd", 0) or 0)
                except Exception:
                    continue
    except Exception:
        return 0.0
    return total


def _daily_budget_usd() -> float:
    """Read CHAT_DAILY_BUDGET_USD env var. Default $2/day — a hard cap that
    blocks runaway. Override to 0 to disable the cap entirely."""
    try:
        return float(os.getenv("CHAT_DAILY_BUDGET_USD", "2.00"))
    except (TypeError, ValueError):
        return 2.00


def _log_chat_usage(usage: dict, model: str, n_tool_calls: int) -> None:
    """Append one line per chat turn to ~/.reip/chat_usage.jsonl so we never
    get surprised by a bill again. Local-only; rolls forever (small).

    Skips when total token usage is zero — that's a test stub or a 401
    that returned before billing happened, no signal there."""
    if sum(usage.get(k, 0) or 0 for k in
            ("input_tokens", "output_tokens",
             "cache_creation_input_tokens", "cache_read_input_tokens")) == 0:
        return
    try:
        from pathlib import Path
        import datetime
        # Rough cost estimate (Sonnet 4.5 list pricing: $3/M in, $15/M out,
        # cache-write $3.75/M, cache-read $0.30/M). Adjust if the model
        # ever changes.
        cost = (
            usage.get("input_tokens", 0)              * 3.00 / 1_000_000 +
            usage.get("output_tokens", 0)             * 15.00 / 1_000_000 +
            usage.get("cache_creation_input_tokens", 0) * 3.75 / 1_000_000 +
            usage.get("cache_read_input_tokens", 0)   * 0.30 / 1_000_000
        )
        log_dir = Path.home() / ".reip"
        log_dir.mkdir(exist_ok=True)
        line = {
            "ts": datetime.datetime.utcnow().isoformat() + "Z",
            "model": model,
            "tool_calls": n_tool_calls,
            **usage,
            "est_cost_usd": round(cost, 6),
        }
        with open(log_dir / "chat_usage.jsonl", "a") as f:
            f.write(json.dumps(line) + "\n")
    except Exception:
        pass


def chat(user_message: str, history: list[dict] | None = None,
         max_tool_iters: int = 3,
         pipeline_summary: list[dict] | None = None) -> dict:
    """Run one chat turn. Returns {reply, tool_calls, error}.

    `history` is a list of {role: 'user'|'assistant', content: str}.
    """
    if anthropic is None:
        return {"error": "anthropic SDK not installed. `uv pip install anthropic`."}

    # Daily budget cap — blocks runaway spend before any API call goes out.
    # User can configure with CHAT_DAILY_BUDGET_USD env (0 disables).
    budget = _daily_budget_usd()
    if budget > 0:
        spent = _today_spend_usd()
        if spent >= budget:
            return {"error": (
                f"Daily chat budget hit: ${spent:.4f} spent today, cap ${budget:.2f}. "
                f"Resets at UTC midnight. Override with CHAT_DAILY_BUDGET_USD env var "
                f"(set to 0 to disable). Check /api/chat/budget for live status."
            )}
    # Resolve credentials. Order: (1) ANTHROPIC_API_KEY env / .env,
    # (2) Claude Code OAuth bearer token stored in macOS keychain
    # (so a Claude Max subscriber doesn't need a separate API key).
    try:
        from dotenv import load_dotenv
        from pathlib import Path
        env_path = Path(__file__).resolve().parents[2] / ".env"
        if env_path.exists():
            load_dotenv(env_path, override=True)
        else:
            load_dotenv(override=True)
    except ImportError:
        pass
    api_key = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
    oauth_token = _get_claude_code_oauth_token()

    if api_key and len(api_key) >= 50:
        client = anthropic.Anthropic(api_key=api_key)
        auth_mode = "api_key"
    elif oauth_token:
        # OAuth flow: don't pass api_key at all — the SDK uses x-api-key
        # if api_key is truthy and Authorization: Bearer otherwise. Empty
        # string disables x-api-key. Also strip the env var so the SDK
        # doesn't auto-populate api_key from ANTHROPIC_API_KEY at init.
        os.environ.pop("ANTHROPIC_API_KEY", None)
        client = anthropic.Anthropic(
            api_key="",
            auth_token=oauth_token,
            default_headers={
                "anthropic-beta": "oauth-2025-04-20",
                "User-Agent": "claude-cli/1.0.0 (external, cli)",
            },
        )
        auth_mode = "claude_code_oauth"
    else:
        return {"error": (
            "No Anthropic credentials found. Either:\n"
            "  (a) Add ANTHROPIC_API_KEY=sk-ant-api03-… to "
            "/Users/richardchen/realestate-platform/.env, or\n"
            "  (b) Log in to Claude Code (which stores an OAuth token in "
            "macOS keychain under 'Claude Code-credentials')."
        )}
    context = _build_context()
    # Cache-stability: only inject pipeline_block when it's non-empty.
    # An empty append used to add nothing visible but changed the system
    # string's identity, potentially fragmenting the cache key. Now the
    # system prompt is identical across all empty-pipeline sessions.
    pipeline_block = _format_pipeline_block(pipeline_summary or [])
    system = SYSTEM_PROMPT + "\n\n" + context
    if pipeline_block:
        system = system + "\n" + pipeline_block

    # Make the pipeline accessible to tool executors (specifically
    # portfolio_resilience, which needs the deal list but the tool
    # input_schema can't pass arbitrary nested data without bloating
    # the prompt). Closure-style module-level handoff is fine here.
    global _CURRENT_PIPELINE
    _CURRENT_PIPELINE = pipeline_summary or []

    # Sliding window: cap conversation history at the last 20 messages
    # (~10 user/assistant turns). Long sessions otherwise re-send unbounded
    # history on every call. Beyond this, older turns are dropped — the
    # pipeline_summary block in the system prompt already carries the
    # user's saved-deal state, so context isn't lost about that.
    HISTORY_WINDOW = 20
    history = (history or [])[-HISTORY_WINDOW:]

    messages: list[dict] = []
    for h in history:
        # Coerce text content into the messages-API shape
        messages.append({"role": h["role"], "content": h["content"]})
    # User's NEW message. Wrap as a content block with cache_control so the
    # entire prefix (system + tools + all prior turns + this message) is
    # cached. Next turn's call hits 90%-off cache reads for everything
    # up to here. Anthropic allows up to 4 cache breakpoints per request;
    # we're using 2 (system, here) so we have headroom for tool-loop chains.
    messages.append({
        "role": "user",
        "content": [{
            "type": "text",
            "text": user_message,
            "cache_control": {"type": "ephemeral"},
        }],
    })

    # Model selection: try primary first, fall back to FALLBACK_MODEL on 429.
    # Claude Code Max plan shares its sonnet quota with the active Claude
    # Code session, so when both are active sonnet 429s; haiku has its own
    # bucket and almost always works.
    model = MODEL
    tool_calls = []
    fallback_used = False

    # Anthropic prompt caching: mark system + tools as ephemeral-cached so
    # the same ~6K tokens of static overhead bills 1× per 5-min window
    # instead of every call (and every tool-loop iteration). 90% discount
    # on cache reads. The user message + conversation history naturally
    # vary so they're not cached.
    cached_system = [{
        "type": "text",
        "text": system,
        "cache_control": {"type": "ephemeral"},
    }]
    # Mark the LAST tool with cache_control; per Anthropic's contract this
    # caches all tools (and the system+tools prefix up to that point).
    cached_tools = [dict(t) for t in TOOLS]
    if cached_tools:
        cached_tools[-1] = {**cached_tools[-1], "cache_control": {"type": "ephemeral"}}

    # Track cumulative usage across iterations for telemetry.
    usage_totals = {"input_tokens": 0, "output_tokens": 0,
                     "cache_creation_input_tokens": 0,
                     "cache_read_input_tokens": 0}

    for _ in range(max_tool_iters):
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=1500,        # Lower ceiling = ~25% less output cost
                system=cached_system,
                tools=cached_tools,
                messages=messages,
            )
        except anthropic.AuthenticationError as e:
            if auth_mode == "api_key":
                return {"error": (
                    f"Anthropic rejected the API key (401). Check .env — key length "
                    f"{len(api_key)} chars (real keys are ~100). Paste again."
                )}
            return {"error": (
                f"Anthropic rejected the Claude Code OAuth token (401). "
                f"Either the token expired (re-run `claude` interactively to refresh) "
                f"or this OAuth scope doesn't grant Messages API access. Detail: {e}"
            )}
        except anthropic.RateLimitError as e:
            if not fallback_used and model != FALLBACK_MODEL:
                model = FALLBACK_MODEL
                fallback_used = True
                continue
            return {"error": (
                f"Anthropic rate-limited (429) on both {MODEL} and {FALLBACK_MODEL}. "
                f"Likely Claude Code Max quota collision — wait 60s and retry, "
                f"or paste a paid ANTHROPIC_API_KEY into .env for a separate bucket."
            )}
        except anthropic.APIError as e:
            return {"error": f"Anthropic API error: {type(e).__name__}: {e}"}

        # Accumulate usage from this iteration
        if getattr(resp, "usage", None):
            u = resp.usage
            for k in usage_totals:
                usage_totals[k] += getattr(u, k, 0) or 0

        if resp.stop_reason != "tool_use":
            # Final text answer
            text_parts = [b.text for b in resp.content if b.type == "text"]
            _log_chat_usage(usage_totals, model, len(tool_calls))
            return {"reply": "".join(text_parts), "tool_calls": tool_calls,
                    "usage": usage_totals}

        # Execute tool_use blocks
        assistant_blocks = []
        tool_results = []
        for b in resp.content:
            if b.type == "text":
                assistant_blocks.append({"type": "text", "text": b.text})
            elif b.type == "tool_use":
                assistant_blocks.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
                try:
                    out = _execute(b.name, b.input or {})
                    tool_calls.append({"name": b.name, "input": b.input, "ok": True})
                except Exception as e:
                    out = {"error": f"{type(e).__name__}: {e}"}
                    tool_calls.append({"name": b.name, "input": b.input, "ok": False, "error": str(e)})
                # Truncate large outputs aggressively. Big tool results
                # (top_zips=100, strategy_backtest full report, etc.) used
                # to thread back through every tool-loop iteration and
                # multiply cost. 4KB cap = ~1K tokens, plenty for the
                # model to summarize from.
                serialized = json.dumps(out, default=str)
                if len(serialized) > 4000:
                    serialized = serialized[:4000] + "...[truncated for cost]"
                tool_results.append({"type": "tool_result", "tool_use_id": b.id, "content": serialized})

        messages.append({"role": "assistant", "content": assistant_blocks})
        messages.append({"role": "user", "content": tool_results})

    return {"reply": "(stopped after max tool iterations)", "tool_calls": tool_calls}

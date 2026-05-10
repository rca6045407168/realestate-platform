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
from typing import Any
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
        "description": "Parse MLS public-remarks text and return the 7 alpha flags (motivated, distressed, use_change, assumable, price_cut, short_sale, probate) with matched terms.",
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
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
        return [zip_returns.to_dict(z) for z in rows]

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
        keep = ["cbsa_code", "cbsa_name", "archetype", "pop", "pop_cagr_5yr", "emp_cagr_5yr",
                "income_cagr_5yr", "net_migration_pct_pop", "gross_yield",
                "appreciation_score", "cashflow_score", "total_return_score"]
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
        return m.iloc[0].to_dict()

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

    if name == "buy_box":
        from . import buybox as buybox_mod
        b = buybox_mod.derive(con, args["zip"])
        if not b:
            return {"error": f"No ZHVI/ZORI data for zip {args['zip']}"}
        return buybox_mod.to_dict(b)

    if name == "stress_test":
        from . import stress as stress_mod
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
        return stress_mod.stress_test(a, state=args.get("state"))

    if name == "parse_remarks":
        s = remarks_mod.parse(args["text"])
        return {
            "motivated": s.motivated, "distressed": s.distressed,
            "use_change": s.use_change, "assumable": s.assumable,
            "price_cut": s.price_cut, "short_sale": s.short_sale, "probate": s.probate,
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

## Tools

You have tools for: top_zips, top_msas, msa_detail, live_listings, underwrite, avm_zips, parse_remarks. Use them when the user asks for specific data; if you can answer from the pre-loaded context below, do that and skip tool use.
"""


# ---------------------------------------------------------------------------
# Chat orchestrator
# ---------------------------------------------------------------------------

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


def chat(user_message: str, history: list[dict] | None = None,
         max_tool_iters: int = 5,
         pipeline_summary: list[dict] | None = None) -> dict:
    """Run one chat turn. Returns {reply, tool_calls, error}.

    `history` is a list of {role: 'user'|'assistant', content: str}.
    """
    if anthropic is None:
        return {"error": "anthropic SDK not installed. `uv pip install anthropic`."}
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
    pipeline_block = _format_pipeline_block(pipeline_summary or [])
    system = SYSTEM_PROMPT + "\n\n" + context + ("\n" + pipeline_block if pipeline_block else "")

    messages: list[dict] = []
    for h in (history or []):
        # Coerce text content into the messages-API shape
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": user_message})

    # Model selection: try primary first, fall back to FALLBACK_MODEL on 429.
    # Claude Code Max plan shares its sonnet quota with the active Claude
    # Code session, so when both are active sonnet 429s; haiku has its own
    # bucket and almost always works.
    model = MODEL
    tool_calls = []
    fallback_used = False
    for _ in range(max_tool_iters):
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=2000,
                system=system,
                tools=TOOLS,
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
        if resp.stop_reason != "tool_use":
            # Final text answer
            text_parts = [b.text for b in resp.content if b.type == "text"]
            return {"reply": "".join(text_parts), "tool_calls": tool_calls}

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
                # Truncate large outputs to keep tokens manageable
                serialized = json.dumps(out, default=str)
                if len(serialized) > 8000:
                    serialized = serialized[:8000] + "...[truncated]"
                tool_results.append({"type": "tool_result", "tool_use_id": b.id, "content": serialized})

        messages.append({"role": "assistant", "content": assistant_blocks})
        messages.append({"role": "user", "content": tool_results})

    return {"reply": "(stopped after max tool iterations)", "tool_calls": tool_calls}

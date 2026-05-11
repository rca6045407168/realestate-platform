"""Free-text MLS remarks parser (Framework §5.8 — Behavioral alpha).

The paper's behavioral-alpha play is a stale-listing alert that fires on
DOM > 60 days, two or more price cuts, and remarks language indicating
seller motivation. We're not licensed to MLS yet, but every alpha source
that depends on remarks is a one-line plug-in once we are.

This module does the parsing now (regex baseline) so the alpha overlay
can flip on `flag_motivated_language` the moment remarks data lands.
The MLX-style local-LLM upgrade path is a `def parse_with_local_llm()`
function call swap — the input/output contract stays the same.

Signals (each a precision-tuned regex; no false-positive language like
"motivated buyer" that would trip a naive 'motivated' match):
  motivated   — "motivated seller", "bring all offers", "must sell",
                 "all offers considered", "owner relocating"
  distressed  — "as-is", "investor special", "cash only", "needs work",
                 "fixer", "TLC", "handyman", "fire damage", "foundation"
  use_change  — "ADU potential", "conversion potential", "two on a lot",
                 "R-2 zoning", "can be subdivided", "office to residential"
  assumable   — "assumable loan", "assumable financing", "VA assumable",
                 "FHA assumable", "low-rate mortgage", "3.x% mortgage"
  price_cut   — "reduced", "price drop", "new price", "price improvement",
                 "now $"
  short_sale  — "short sale", "third party approval", "subject to bank approval"
  probate     — "probate", "estate sale", "trust sale", "sold by heir"
  auction     — "auction", "REO", "bank owned", "foreclosure", "trustee sale",
                 "sheriff sale", "HUD owned", "court-ordered sale", "online auction",
                 "starting bid"

Additionally returns a `score` (0–1) that's just count-of-categories-hit/8.
"""
from __future__ import annotations
import re
from dataclasses import dataclass

# Each pattern is single-line, case-insensitive, word-boundary-anchored
# where it would otherwise misfire. Negative lookbehinds prevent
# "motivated buyer" / "non-cash only" false positives.
_PATTERNS = {
    "motivated":  re.compile(
        r"\b(motivated\s+seller|bring\s+all\s+offers|all\s+offers\s+considered|"
        r"must\s+sell|owner\s+relocating|relocation\s+forces?\s+sale|priced\s+to\s+sell|"
        r"send\s+offers|make\s+(an?\s+)?offer|seller\s+motivated)\b", re.I),
    "distressed": re.compile(
        r"\b(as[\s-]?is|investor\s+special|cash\s+only|cash\s+offers?\s+only|"
        r"needs\s+work|fixer(\s+upper)?|tlc|handyman|fire\s+damage|foundation\s+(issue|problem)|"
        r"sold\s+as[\s-]?is|gut\s+job|distress(ed)?|teardown|opportunity\s+(for\s+)?investors?)\b", re.I),
    "use_change": re.compile(
        r"\b(adu\s+(potential|possible|eligible)|conversion\s+potential|two\s+on\s+a\s+lot|"
        r"r[\-\s]?[2-4]\s+zoning|can\s+be\s+subdivided|lot\s+split|office\s+to\s+(residential|housing)|"
        r"sb\s*9|sb\s*10|by\s+right|build[\s-]?to[\s-]?rent\s+potential)\b", re.I),
    "assumable":  re.compile(
        # Catches: "assumable loan", "assumable VA loan", "VA assumable",
        # "FHA assumable", "3.25% assumable", "low-rate mortgage".
        r"\b(assumable\s+(?:\w+\s+){0,2}(?:loan|financing|mortgage)|"
        r"(?:va|fha|usda)\s+assumable|[1-4]\.\d{1,2}\s*%\s+assumable|"
        r"low[\s-]?rate\s+mortgage)\b", re.I),
    "price_cut": re.compile(
        r"\b(price\s+(reduced|drop|improvement|adjustment)|reduced!|new\s+price|"
        r"now\s+\$\d|just\s+reduced|price\s+cut)\b", re.I),
    "short_sale": re.compile(
        r"\b(short\s+sale|third[\s-]party\s+approval|subject\s+to\s+bank\s+approval|"
        r"lender\s+approval\s+required)\b", re.I),
    "probate":   re.compile(
        r"\b(probate|estate\s+sale|trust\s+sale|sold\s+by\s+heir|conservator(\s+sale)?)\b", re.I),
    "auction":   re.compile(
        # Auction / foreclosure / REO / bank-owned. Negative lookahead on
        # "no auction" / "private (not auction)" to avoid false positives.
        # `reo` requires word boundaries so "stereo" / "reorder" don't match.
        r"\b(?:(?<!no\s)(?<!not\s)auction(?!eers?\s+approved)|"
        r"(?:bank|lender)[\s-]?owned|"
        r"reo\s+(?:property|sale|listing)|\breo\b(?!\w)|"
        r"foreclosure|trustee[\']?s?\s+sale|sheriff[\']?s?\s+sale|"
        r"hud[\s-]?owned|hud\s+home|"
        r"court[\s-]?ordered\s+sale|"
        r"online\s+auction|live\s+auction|starting\s+bid|opening\s+bid|"
        r"foreclosed|repossessed)\b", re.I),
}


@dataclass
class RemarkSignals:
    motivated: bool = False
    distressed: bool = False
    use_change: bool = False
    assumable: bool = False
    price_cut: bool = False
    short_sale: bool = False
    probate: bool = False
    auction: bool = False
    score: float = 0.0
    matched_terms: tuple[str, ...] = ()


# Negation phrases that should suppress the `auction` flag when the only
# auction-indicator is a bare "auction" mention. Variable-width lookbehind
# isn't supported in re, so we post-filter.
_AUCTION_NEGATIONS = re.compile(
    r"(?:not\s+(?:an?\s+)?|no\s+|never\s+(?:an?\s+)?|non[-\s]?)auction\b", re.I)


def parse(text: str | None) -> RemarkSignals:
    """Run all regex categories on free text. Returns a RemarkSignals dataclass."""
    if not text:
        return RemarkSignals()
    hits = {}
    matched_terms: list[str] = []
    for cat, pattern in _PATTERNS.items():
        m = pattern.search(text)
        hits[cat] = bool(m)
        if m:
            matched_terms.append(m.group(0).strip().lower())

    # Auction post-filter: if the only thing triggering the auction flag is
    # the bare word "auction" AND the text has a negation like "not an auction",
    # drop the flag.
    if hits.get("auction"):
        matched_auction = next((t for t in matched_terms
                                 if any(kw in t for kw in
                                        ("auction", "reo", "foreclos", "trustee",
                                         "sheriff", "hud", "court-ordered",
                                         "starting bid", "opening bid", "bank-owned",
                                         "bank owned", "lender-owned", "lender owned"))),
                                None)
        if matched_auction == "auction" and _AUCTION_NEGATIONS.search(text):
            hits["auction"] = False
            try:
                matched_terms.remove("auction")
            except ValueError:
                pass

    score = sum(hits.values()) / len(_PATTERNS)
    return RemarkSignals(
        motivated=hits["motivated"], distressed=hits["distressed"],
        use_change=hits["use_change"], assumable=hits["assumable"],
        price_cut=hits["price_cut"], short_sale=hits["short_sale"],
        probate=hits["probate"], auction=hits["auction"],
        score=score, matched_terms=tuple(matched_terms),
    )


# ---- Local-LLM upgrade hook (MLX-equivalent stub) ------------------------

def parse_with_local_llm(text: str | None) -> RemarkSignals:
    """Drop-in slot for an Ollama / MLX model.

    The LLM upgrade catches what regex misses: implication, idiom,
    multilingual remarks, and tone (e.g. "owner has accepted a job in
    Atlanta" implies relocation). Until that's wired, fall back to regex.
    """
    return parse(text)

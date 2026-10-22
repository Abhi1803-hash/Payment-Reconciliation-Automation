"""
AI triage of unexplained bank-statement lines using Claude.

Bank narrations are short, cryptic and inconsistent ("CHGBK DR CASE#4411", "RVSL OF CR DT 120926",
"BY CLG-UNIDENTIFIED CR"). Rule-based matching breaks every time a bank changes its format, so
this module asks Claude to classify each line into a fixed set of categories and to suggest
the owning team.

Guardrails:
  - Claude only sees the narration, date and amount of lines that already failed matching
    (no customer or merchant data).
  - Output must be JSON; every label is validated against the allowed list. Anything invalid,
    missing or low-confidence falls back to the keyword rules and is marked for human review.
  - With no API key the keyword rules run alone, so the daily recon never breaks.
"""
from __future__ import annotations

import json
import os
import re

import pandas as pd

MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")

CATEGORIES = {
    "CHARGEBACK":          ("Disputes / Risk",  "Link to dispute case; recover from merchant balance"),
    "REVERSAL":            ("Settlement Ops",   "Find the original credit being reversed; re-run matching"),
    "BANK_CHARGES":        ("Finance / AP",     "Book to bank-charges GL; verify against bank tariff"),
    "PAYOUT_RETURN":       ("Payouts Ops",      "Contact merchant to update beneficiary account; re-initiate"),
    "INTEREST":            ("Treasury",         "Book to interest-income GL"),
    "REFUND_REVERSAL":     ("Refunds Ops",      "Match to failed refund; re-credit customer"),
    "UNIDENTIFIED_CREDIT": ("Settlement Ops",   "Raise query with bank for remitter details; park in suspense"),
}

KEYWORD_RULES = [
    (r"CHGBK|CHARGEBACK|DISPUTE", "CHARGEBACK"),
    (r"RFND|REFUND", "REFUND_REVERSAL"),
    (r"RVSL|REVERSAL|RECALL", "REVERSAL"),
    (r"BANK CHGS|SVC CHG|CHARGES", "BANK_CHARGES"),
    (r"RTN|RETURN|AC CLOSED", "PAYOUT_RETURN"),
    (r"\bINT\b|INTEREST", "INTEREST"),
]

SYSTEM = f"""You classify unexplained lines from an Indian payment company's nodal bank statement.
Allowed categories: {", ".join(CATEGORIES)}.
Hints: CHGBK = chargeback, RVSL = reversal, RTN = return, RFND = refund, INT = interest,
CLG = clearing. A credit that reverses a chargeback is still CHARGEBACK. If the narration gives
no reliable clue, use UNIDENTIFIED_CREDIT for credits.
Return ONLY a JSON array, one object per input line, in the same order:
[{{"txn_ref": "...", "category": "...", "confidence": 0.0-1.0, "reason": "<12 words"}}]"""


def keyword_classify(narration: str, credit: float) -> str:
    for pattern, label in KEYWORD_RULES:
        if re.search(pattern, narration.upper()):
            return label
    return "UNIDENTIFIED_CREDIT" if credit > 0 else "REVERSAL"


def _call_claude(lines: pd.DataFrame) -> list[dict] | None:
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key or lines.empty:
        return None
    try:
        import anthropic

        payload = lines[["txn_ref", "value_date", "narration", "credit", "debit"]].astype(str).to_dict("records")
        msg = anthropic.Anthropic(api_key=key).messages.create(
            model=MODEL, max_tokens=4000, system=SYSTEM,
            messages=[{"role": "user", "content": json.dumps(payload)}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        match = re.search(r"\[.*\]", text, flags=re.S)
        return json.loads(match.group(0)) if match else None
    except Exception as exc:
        print(f"[ai_triage] Claude unavailable ({exc.__class__.__name__}); using keyword rules")
        return None


def triage(lines: pd.DataFrame, min_confidence: float = 0.7) -> tuple[pd.DataFrame, str]:
    out = lines.copy()
    out["rule_category"] = [keyword_classify(n, c) for n, c in zip(out.narration, out.credit)]
    ai = _call_claude(out)
    source = f"Claude ({MODEL})" if ai else "Keyword rules (set ANTHROPIC_API_KEY to enable Claude)"

    by_ref = {str(r.get("txn_ref")): r for r in (ai or [])}
    cats, conf, reasons, review = [], [], [], []
    for row in out.itertuples():
        r = by_ref.get(row.txn_ref)
        valid = r is not None and r.get("category") in CATEGORIES
        c = float(r.get("confidence", 0)) if valid else None
        if valid and c >= min_confidence:
            cats.append(r["category"]); conf.append(c); reasons.append(r.get("reason", ""))
            review.append(r["category"] != row.rule_category)      # AI and rules disagree -> human check
        else:
            cats.append(row.rule_category); conf.append(c); reasons.append("keyword rule")
            review.append(ai is not None)                          # AI failed on this line
    out["category"], out["ai_confidence"], out["reason"], out["needs_review"] = cats, conf, reasons, review
    out["owner"] = out.category.map(lambda k: CATEGORIES[k][0])
    out["next_action"] = out.category.map(lambda k: CATEGORIES[k][1])
    out.loc[out.category.eq("UNIDENTIFIED_CREDIT"), "needs_review"] = True
    return out, source

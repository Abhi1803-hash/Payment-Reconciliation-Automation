import json
import types

import pandas as pd
import pytest

from src import ai_triage, generate_data, recon


@pytest.fixture(scope="module")
def results():
    generate_data.main(n_payments=8000, n_merchants=200)
    recon.run_matching("2026-10-15")
    return recon.collect("2026-10-15")


def test_every_injected_break_is_detected(results):
    acc = recon.score_against_ground_truth(results)
    assert (acc.recall_pct == 100).all(), acc
    assert (acc.precision_pct == 100).all(), acc


def test_clean_payments_are_not_flagged(results):
    flagged = set(results["payments"].payment_id)
    truth = set(pd.read_csv(recon.RAW / "_ground_truth_payments.csv").payment_id)
    ledger = pd.read_csv(recon.RAW / "gateway_ledger.csv")
    clean = set(ledger.payment_id) - truth
    false_positives = (flagged & clean) - set(
        results["payments"].loc[results["payments"].category == "PENDING_NOT_DUE", "payment_id"])
    assert not false_positives


def test_money_at_risk_only_on_money_breaks(results):
    p = results["payments"]
    assert (p.loc[p.category.isin(["LATE_SETTLEMENT", "ID_FORMAT_MISMATCH"]), "amount_at_risk"] == 0).all()
    assert (p.loc[p.category == "NOT_SETTLED", "amount_at_risk"] > 0).all()


# ---------------------------------------------------------------- AI triage guardrails
LINES = pd.DataFrame({
    "txn_ref": ["T1", "T2", "T3"],
    "value_date": ["2026-09-10"] * 3,
    "narration": ["CHGBK DR CASE#1 DISPUTE DEBIT", "DSPT ADJ CB-77812", "BY CLG-UNIDENTIFIED CR"],
    "credit": [0.0, 0.0, 900.0],
    "debit": [500.0, 700.0, 0.0],
})


def _fake_claude(monkeypatch, reply):
    import anthropic

    class Msgs:
        def create(self, **kw):
            return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=json.dumps(reply))])

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(anthropic, "Anthropic", lambda api_key: types.SimpleNamespace(messages=Msgs()))


def test_keyword_rules_miss_unseen_bank_formats():
    # "DSPT ADJ CB" is a chargeback in a format the rules have never seen
    assert ai_triage.keyword_classify("DSPT ADJ CB-77812", 0.0) != "CHARGEBACK"


def test_claude_fixes_unseen_format_and_flags_disagreement(monkeypatch):
    _fake_claude(monkeypatch, [
        {"txn_ref": "T1", "category": "CHARGEBACK", "confidence": 0.97, "reason": "CHGBK"},
        {"txn_ref": "T2", "category": "CHARGEBACK", "confidence": 0.86, "reason": "DSPT + CB = dispute"},
        {"txn_ref": "T3", "category": "UNIDENTIFIED_CREDIT", "confidence": 0.9, "reason": "no remitter"},
    ])
    out, source = ai_triage.triage(LINES)
    assert source.startswith("Claude")
    assert list(out.category) == ["CHARGEBACK", "CHARGEBACK", "UNIDENTIFIED_CREDIT"]
    assert list(out.needs_review) == [False, True, True]   # T2: AI overrode rules -> human check


def test_invalid_or_low_confidence_ai_output_falls_back_to_rules(monkeypatch):
    _fake_claude(monkeypatch, [
        {"txn_ref": "T1", "category": "FRAUD", "confidence": 0.99},          # not an allowed label
        {"txn_ref": "T2", "category": "CHARGEBACK", "confidence": 0.3},      # too unsure
    ])                                                                       # T3 missing entirely
    out, _ = ai_triage.triage(LINES)
    assert list(out.category) == list(out.rule_category)
    assert out.needs_review.all()

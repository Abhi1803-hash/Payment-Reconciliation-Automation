"""
Generate the three files a payments finance-ops team reconciles every day:

  data/raw/gateway_ledger.csv       Our books: every captured payment with the fee we expect
                                    to deduct and the net amount the merchant should receive.
  data/raw/settlement_report.csv    The acquiring bank's payment-level settlement report
                                    (what they say they settled, under which UTR batch).
  data/raw/bank_statement.csv       Our nodal / escrow bank statement: one credit per UTR
                                    batch, with free-text narration (messy, like real life).

Realistic breaks are injected so the reconciliation engine has something to find:
  - payment never settled                - short settlement (amount mismatch)
  - higher fee deducted than contracted  - duplicate settlement
  - settled late (beyond T+2)            - payment_id mangled in bank file (needs fuzzy match)
  - bank credit with no matching batch   - batch total != sum of its payments
  - chargeback / reversal debits with cryptic narrations
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

SEED = 7
RAW = Path(__file__).resolve().parents[1] / "data" / "raw"
METHODS = {"UPI": (0.55, 0.0), "Credit Card": (0.2, 0.019), "Debit Card": (0.12, 0.009),
           "Netbanking": (0.08, 0.019), "Wallet": (0.05, 0.019)}


def business_days_after(dates: pd.Series, n: int) -> pd.Series:
    return pd.Series(np.busday_offset(dates.values.astype("datetime64[D]"), n, roll="forward"),
                     index=dates.index)


def make_ledger(rng, month: str, n_payments: int, n_merchants: int) -> pd.DataFrame:
    start = pd.Timestamp(month + "-01")
    days = (start + pd.DateOffset(months=1) - start).days
    m = list(METHODS)
    df = pd.DataFrame({
        "payment_id": [f"pay_{i:010d}" for i in rng.choice(10**9, n_payments, replace=False)],
        "merchant_id": [f"M{100000 + i}" for i in rng.integers(0, n_merchants, n_payments)],
        "captured_at": start + pd.to_timedelta(rng.integers(0, days * 86400, n_payments), unit="s"),
        "method": rng.choice(m, n_payments, p=[METHODS[k][0] for k in m]),
        "amount": np.round(rng.lognormal(np.log(900), 0.9, n_payments), 2),
    }).sort_values("captured_at", ignore_index=True)
    rate = df.method.map({k: v[1] for k, v in METHODS.items()})
    df["fee"] = (df.amount * rate).round(2)
    df["gst"] = (df.fee * 0.18).round(2)
    df["expected_net"] = (df.amount - df.fee - df.gst).round(2)
    df["expected_settlement_date"] = business_days_after(df.captured_at.dt.normalize(), 2).dt.date
    return df


def make_settlement(rng, ledger: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    s = ledger[["payment_id", "merchant_id", "amount", "fee", "gst", "expected_net",
                "expected_settlement_date"]].copy()
    s["settled_amount"] = s.expected_net
    s["settlement_date"] = pd.to_datetime(s.expected_settlement_date)
    n = len(s)
    truth = {}

    def pick(k):
        idx = rng.choice(s.index[~s.index.isin(sum(truth.values(), []))], k, replace=False)
        return list(idx)

    truth["NOT_SETTLED"] = pick(max(20, n // 900))
    truth["SHORT_SETTLED"] = pick(max(15, n // 1500))
    short = np.minimum(rng.uniform(5, 400, len(truth["SHORT_SETTLED"])), 0.4 * s.loc[truth["SHORT_SETTLED"], "expected_net"])
    s.loc[truth["SHORT_SETTLED"], "settled_amount"] -= np.round(short, 2)
    truth["EXCESS_FEE"] = pick(max(15, n // 1500))
    extra_fee = (s.loc[truth["EXCESS_FEE"], "amount"] * 0.006).round(2) + 1
    s.loc[truth["EXCESS_FEE"], "fee"] += extra_fee
    s.loc[truth["EXCESS_FEE"], "settled_amount"] -= (extra_fee * 1.18).round(2)
    truth["LATE_SETTLEMENT"] = pick(max(25, n // 800))
    s.loc[truth["LATE_SETTLEMENT"], "settlement_date"] += pd.to_timedelta(rng.integers(3, 9, len(truth["LATE_SETTLEMENT"])), unit="D")
    truth["ID_MANGLED"] = pick(max(15, n // 1500))
    s.loc[truth["ID_MANGLED"], "payment_id"] = (s.loc[truth["ID_MANGLED"], "payment_id"]
                                                .str.upper().str.replace("PAY_", "PAY-", regex=False))
    truth["DUPLICATE_SETTLEMENT"] = pick(max(10, n // 3000))

    s = s.drop(index=truth["NOT_SETTLED"])
    s = pd.concat([s, s.loc[truth["DUPLICATE_SETTLEMENT"]]], ignore_index=False)

    # group into UTR batches: one per merchant per settlement date
    s["settlement_date"] = s.settlement_date.dt.date
    keys = s[["merchant_id", "settlement_date"]].drop_duplicates().reset_index(drop=True)
    keys["utr"] = [f"UTR{d:%y%m%d}{rng.integers(10**7, 10**8)}" for d in pd.to_datetime(keys.settlement_date)]
    s = s.merge(keys, on=["merchant_id", "settlement_date"], how="left")
    cols = ["utr", "payment_id", "merchant_id", "amount", "fee", "gst", "settled_amount", "settlement_date"]
    return s[cols].rename(columns={"amount": "gross_amount"}), truth


NARRATION_NOISE = ["NEFT CR", "RTGS CR", "IMPS CR", "NEFT-CR", "BY TRANSFER"]
UNKNOWN_NARRATIONS = [
    ("NEFT CR-HDFC0000001-CHGBK REVERSAL REF 88231", "CHARGEBACK"),
    ("RTGS CR-ICIC0000104-MISC CREDIT ADJ", "UNIDENTIFIED_CREDIT"),
    ("BY CLG-UNIDENTIFIED CR", "UNIDENTIFIED_CREDIT"),
    ("IMPS CR-RFND RVSL-MID UNKNOWN", "REFUND_REVERSAL"),
    ("NEFT CR-SBIN0001707-INT ON NODAL BAL", "INTEREST"),
]
DEBIT_NARRATIONS = [
    ("CHGBK DR CASE#{n} DISPUTE DEBIT", "CHARGEBACK"),
    ("RVSL OF CR DT {d} DUP SETTLMNT", "REVERSAL"),
    ("BANK CHGS GST ON SVC CHG", "BANK_CHARGES"),
    ("NEFT RTN-BENE AC CLOSED {n}", "PAYOUT_RETURN"),
    ("ADJ DR-SETTLEMENT RECALL REF {n}", "REVERSAL"),
]


def make_bank_statement(rng, settlement: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    batches = settlement.groupby(["utr", "merchant_id", "settlement_date"], as_index=False).settled_amount.sum()
    batches["credit"] = batches.settled_amount.round(2)
    batches["debit"] = 0.0
    batches["narration"] = [f"{rng.choice(NARRATION_NOISE)}-{u}-RZP SETTL {mid}"
                            for u, mid in zip(batches.utr, batches.merchant_id)]
    truth = {}
    # batch totals that do not tie to the payment-level file
    off = rng.choice(len(batches), 8, replace=False)
    batches.loc[off, "credit"] = (batches.loc[off, "credit"]
                                  - np.minimum(rng.uniform(50, 900, 8), 0.3 * batches.loc[off, "credit"])).round(2)
    truth["BATCH_TOTAL_MISMATCH"] = list(batches.loc[off, "utr"])
    # bank lost the UTR in the narration for a few credits
    lost = rng.choice(np.setdiff1d(np.arange(len(batches)), off), 6, replace=False)
    batches.loc[lost, "narration"] = "NEFT CR-SETTLEMENT FROM ACQUIRER"
    truth["UTR_MISSING_IN_NARRATION"] = list(batches.loc[lost, "utr"])

    stmt = batches[["settlement_date", "narration", "credit", "debit"]].rename(columns={"settlement_date": "value_date"})
    dates = pd.to_datetime(stmt.value_date)
    extra = []
    for i, (nar, label) in enumerate(UNKNOWN_NARRATIONS):
        extra.append({"value_date": dates.sample(1, random_state=i).dt.date.item(), "narration": nar,
                      "credit": round(float(rng.uniform(500, 25000)), 2), "debit": 0.0, "_label": label})
    for i in range(14):
        tmpl, label = DEBIT_NARRATIONS[i % len(DEBIT_NARRATIONS)]
        d = dates.sample(1, random_state=100 + i).dt.date.item()
        extra.append({"value_date": d, "narration": tmpl.format(n=rng.integers(10**5, 10**6), d=f"{d:%d%m%y}"),
                      "credit": 0.0, "debit": round(float(rng.uniform(300, 15000)), 2), "_label": label})
    stmt = pd.concat([stmt, pd.DataFrame(extra)], ignore_index=True).sort_values("value_date", kind="stable", ignore_index=True)
    stmt.insert(0, "txn_ref", [f"STMT{i:07d}" for i in range(len(stmt))])
    truth["_bank_labels"] = stmt.loc[stmt._label.notna(), ["txn_ref", "_label"]]
    return stmt.drop(columns="_label"), truth


def main(month="2026-09", n_payments=40000, n_merchants=600):
    rng = np.random.default_rng(SEED)
    ledger = make_ledger(rng, month, n_payments, n_merchants)
    settlement, truth_s = make_settlement(rng, ledger)
    stmt, truth_b = make_bank_statement(rng, settlement)
    RAW.mkdir(parents=True, exist_ok=True)
    ledger.to_csv(RAW / "gateway_ledger.csv", index=False)
    settlement.to_csv(RAW / "settlement_report.csv", index=False)
    stmt.to_csv(RAW / "bank_statement.csv", index=False)
    # ground truth so the engine's accuracy can be measured
    gt = [{"payment_id": ledger.loc[i, "payment_id"], "true_break": k}
          for k, idx in truth_s.items() for i in idx]
    pd.DataFrame(gt).to_csv(RAW / "_ground_truth_payments.csv", index=False)
    truth_b.pop("_bank_labels").rename(columns={"_label": "true_category"}) \
        .to_csv(RAW / "_ground_truth_bank.csv", index=False)
    pd.DataFrame([{"utr": u, "true_break": k} for k, us in truth_b.items() for u in us]) \
        .to_csv(RAW / "_ground_truth_batches.csv", index=False)
    print(f"ledger={len(ledger):,}  settlement_rows={len(settlement):,}  "
          f"utr_batches={settlement.utr.nunique():,}  bank_lines={len(stmt):,}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", default="2026-09")
    ap.add_argument("--payments", type=int, default=40000)
    a = ap.parse_args()
    main(a.month, a.payments)

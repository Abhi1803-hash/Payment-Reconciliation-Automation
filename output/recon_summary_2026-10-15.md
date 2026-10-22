# Reconciliation summary — as of 2026-10-15

- **40,000** ledger payments, **39,956** auto-matched (26 only after ID normalisation)
- **Auto-match rate: 99.54%**
- **185** open payment exceptions, **₹79,381.81** at risk
- **12,008** UTR batches checked against **12,027** bank lines; 8 total mismatches, 6 matched without UTR
- **19** unexplained bank lines triaged by: Keyword rules (set ANTHROPIC_API_KEY to enable Claude)

## Exceptions by type

| category             | owner             |   exceptions |   amount_at_risk |
|:---------------------|:------------------|-------------:|-----------------:|
| NOT_SETTLED          | Settlement Ops    |           44 |        55,255.87 |
| DUPLICATE_SETTLEMENT | Settlement Ops    |           13 |        18,874.69 |
| SHORT_SETTLED        | Settlement Ops    |           26 |         4,963.42 |
| EXCESS_FEE           | Finance / Pricing |           26 |           287.83 |
| LATE_SETTLEMENT      | Settlement Ops    |           50 |             0.00 |
| ID_FORMAT_MISMATCH   | Data Engineering  |           26 |             0.00 |

![Exceptions by type](exceptions_by_type.svg)

## Bank lines needing triage

| value_date   | narration                                    |   credit |    debit | category            | owner           | needs_review   |
|:-------------|:---------------------------------------------|---------:|---------:|:--------------------|:----------------|:---------------|
| 2026-09-04   | NEFT RTN-BENE AC CLOSED 563666               |     0    | 14168.9  | PAYOUT_RETURN       | Payouts Ops     | False          |
| 2026-09-04   | ADJ DR-SETTLEMENT RECALL REF 429838          |     0    |  4841.94 | REVERSAL            | Settlement Ops  | False          |
| 2026-09-04   | NEFT RTN-BENE AC CLOSED 729942               |     0    |  5436.59 | PAYOUT_RETURN       | Payouts Ops     | False          |
| 2026-09-07   | IMPS CR-RFND RVSL-MID UNKNOWN                |  9490.55 |     0    | REFUND_REVERSAL     | Refunds Ops     | False          |
| 2026-09-07   | BANK CHGS GST ON SVC CHG                     |     0    | 14730.5  | BANK_CHARGES        | Finance / AP    | False          |
| 2026-09-10   | NEFT CR-SBIN0001707-INT ON NODAL BAL         | 14590.6  |     0    | INTEREST            | Treasury        | False          |
| 2026-09-11   | RVSL OF CR DT 110926 DUP SETTLMNT            |     0    |  3858.18 | REVERSAL            | Settlement Ops  | False          |
| 2026-09-14   | RVSL OF CR DT 140926 DUP SETTLMNT            |     0    | 12805.9  | REVERSAL            | Settlement Ops  | False          |
| 2026-09-15   | CHGBK DR CASE#396452 DISPUTE DEBIT           |     0    | 11227.1  | CHARGEBACK          | Disputes / Risk | False          |
| 2026-09-16   | CHGBK DR CASE#336807 DISPUTE DEBIT           |     0    | 14471.1  | CHARGEBACK          | Disputes / Risk | False          |
| 2026-09-21   | RTGS CR-ICIC0000104-MISC CREDIT ADJ          | 10299.9  |     0    | UNIDENTIFIED_CREDIT | Settlement Ops  | True           |
| 2026-09-23   | NEFT RTN-BENE AC CLOSED 749014               |     0    | 12262.5  | PAYOUT_RETURN       | Payouts Ops     | False          |
| 2026-09-24   | BY CLG-UNIDENTIFIED CR                       |  4577.71 |     0    | UNIDENTIFIED_CREDIT | Settlement Ops  | True           |
| 2026-09-24   | ADJ DR-SETTLEMENT RECALL REF 166009          |     0    |   602.91 | REVERSAL            | Settlement Ops  | False          |
| 2026-09-24   | BANK CHGS GST ON SVC CHG                     |     0    | 12436.9  | BANK_CHARGES        | Finance / AP    | False          |
| 2026-09-25   | BANK CHGS GST ON SVC CHG                     |     0    |  7712.07 | BANK_CHARGES        | Finance / AP    | False          |
| 2026-09-30   | NEFT CR-HDFC0000001-CHGBK REVERSAL REF 88231 | 20872.5  |     0    | CHARGEBACK          | Disputes / Risk | False          |
| 2026-09-30   | RVSL OF CR DT 300926 DUP SETTLMNT            |     0    | 13096.3  | REVERSAL            | Settlement Ops  | False          |
| 2026-10-02   | CHGBK DR CASE#180064 DISPUTE DEBIT           |     0    | 13467.4  | CHARGEBACK          | Disputes / Risk | False          |

## Detection accuracy vs injected breaks

| level       | break_type                  |   injected |   detected |   flagged_total |   recall_pct |   precision_pct |
|:------------|:----------------------------|-----------:|-----------:|----------------:|-------------:|----------------:|
| Payment     | DUPLICATE_SETTLEMENT        |         13 |         13 |              13 |          100 |             100 |
| Payment     | EXCESS_FEE                  |         26 |         26 |              26 |          100 |             100 |
| Payment     | ID_FORMAT_MISMATCH          |         26 |         26 |              26 |          100 |             100 |
| Payment     | LATE_SETTLEMENT             |         50 |         50 |              50 |          100 |             100 |
| Payment     | NOT_SETTLED                 |         44 |         44 |              44 |          100 |             100 |
| Payment     | SHORT_SETTLED               |         26 |         26 |              26 |          100 |             100 |
| Batch       | BATCH_TOTAL_MISMATCH        |          8 |          8 |               8 |          100 |             100 |
| Batch       | MATCHED_WITHOUT_UTR         |          6 |          6 |               6 |          100 |             100 |
| Bank triage | Category assigned correctly |         19 |         19 |              19 |          100 |             100 |

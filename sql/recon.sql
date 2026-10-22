-- =====================================================================
-- Three-way payment reconciliation (SQLite; portable to Postgres/Snowflake)
--   ledger (our books)  <->  settlement_report (acquirer)  <->  bank_statement (nodal a/c)
-- Each block is "-- name: <key>" and is executed in order by src/recon.py.
-- Tolerance for amount comparison: ₹0.01
-- =====================================================================

-- name: normalise_ids
-- Bank/acquirer files often change case and separators of our IDs.
-- Build a normalised key so a second, fuzzy pass can still match them.
DROP TABLE IF EXISTS settlement_n;
CREATE TABLE settlement_n AS
SELECT s.*,
       ROW_NUMBER() OVER (PARTITION BY s.payment_id ORDER BY s.settlement_date, s.utr) AS occurrence,
       lower(replace(s.payment_id, '-', '_'))                                         AS pid_norm
FROM settlement_report s;

-- name: payment_match
-- Pass 1: exact payment_id. Pass 2: normalised payment_id (fuzzy). First occurrence only;
-- extra occurrences are duplicates handled below.
DROP TABLE IF EXISTS payment_match;
CREATE TABLE payment_match AS
WITH exact AS (
    SELECT l.payment_id, s.rowid AS s_rowid, 'EXACT_ID' AS match_type
    FROM ledger l
    JOIN settlement_n s ON s.payment_id = l.payment_id AND s.occurrence = 1
),
fuzzy AS (
    SELECT l.payment_id, s.rowid AS s_rowid, 'NORMALISED_ID' AS match_type
    FROM ledger l
    JOIN settlement_n s ON s.pid_norm = lower(l.payment_id) AND s.occurrence = 1
    WHERE l.payment_id NOT IN (SELECT payment_id FROM exact)
      AND s.payment_id <> l.payment_id
)
SELECT * FROM exact
UNION ALL
SELECT * FROM fuzzy;

-- name: payment_exceptions
-- One row per payment-level break, with a single primary category.
DROP TABLE IF EXISTS payment_exceptions;
CREATE TABLE payment_exceptions AS
WITH matched AS (
    SELECT l.payment_id, l.merchant_id, l.amount, l.fee AS ledger_fee, l.expected_net,
           l.expected_settlement_date, m.match_type,
           s.utr, s.fee AS settled_fee, s.settled_amount, s.settlement_date,
           ROUND(s.settled_amount - l.expected_net, 2) AS diff
    FROM ledger l
    JOIN payment_match m USING (payment_id)
    JOIN settlement_n s ON s.rowid = m.s_rowid
)
-- amount breaks (fee breaks take precedence: they explain the short amount)
SELECT payment_id, merchant_id, utr, amount, expected_net, settled_amount, diff,
       expected_settlement_date, settlement_date,
       CASE WHEN settled_fee > ledger_fee + 0.01 THEN 'EXCESS_FEE'
            WHEN diff < 0 THEN 'SHORT_SETTLED'
            ELSE 'OVER_SETTLED' END                         AS category,
       match_type
FROM matched
WHERE ABS(diff) > 0.01
UNION ALL
-- timing breaks
SELECT payment_id, merchant_id, utr, amount, expected_net, settled_amount, diff,
       expected_settlement_date, settlement_date,
       'LATE_SETTLEMENT', match_type
FROM matched
WHERE ABS(diff) <= 0.01 AND settlement_date > expected_settlement_date
UNION ALL
-- matched only after ID normalisation: money is right, but the bank file needs fixing
SELECT payment_id, merchant_id, utr, amount, expected_net, settled_amount, diff,
       expected_settlement_date, settlement_date,
       'ID_FORMAT_MISMATCH', match_type
FROM matched
WHERE ABS(diff) <= 0.01 AND settlement_date <= expected_settlement_date AND match_type = 'NORMALISED_ID'
UNION ALL
-- in our books, never settled (only if already due)
SELECT l.payment_id, l.merchant_id, NULL, l.amount, l.expected_net, NULL, -l.expected_net,
       l.expected_settlement_date, NULL,
       CASE WHEN l.expected_settlement_date <= :as_of THEN 'NOT_SETTLED' ELSE 'PENDING_NOT_DUE' END,
       NULL
FROM ledger l
WHERE l.payment_id NOT IN (SELECT payment_id FROM payment_match)
UNION ALL
-- settled more than once
SELECT COALESCE(l.payment_id, s.payment_id), s.merchant_id, s.utr, s.gross_amount, l.expected_net,
       s.settled_amount, s.settled_amount, l.expected_settlement_date, s.settlement_date,
       'DUPLICATE_SETTLEMENT', NULL
FROM settlement_n s
LEFT JOIN ledger l ON lower(l.payment_id) = s.pid_norm
WHERE s.occurrence > 1
UNION ALL
-- settled by acquirer but not in our books at all
SELECT s.payment_id, s.merchant_id, s.utr, s.gross_amount, NULL, s.settled_amount, s.settled_amount,
       NULL, s.settlement_date, 'NOT_IN_LEDGER', NULL
FROM settlement_n s
WHERE s.occurrence = 1
  AND s.rowid NOT IN (SELECT s_rowid FROM payment_match);

-- name: batch_recon
-- Level 2: each UTR batch total (acquirer) vs the credit that hit our bank account.
DROP TABLE IF EXISTS batch_recon;
CREATE TABLE batch_recon AS
WITH batches AS (
    SELECT utr, merchant_id, settlement_date, ROUND(SUM(settled_amount), 2) AS batch_total, COUNT(*) AS payments
    FROM settlement_report GROUP BY 1, 2, 3
),
bank AS (
    SELECT txn_ref, value_date, narration, credit, utr_extracted
    FROM bank_statement_parsed WHERE credit > 0
),
by_utr AS (
    SELECT b.*, k.txn_ref, k.credit, k.narration, 'UTR' AS match_type
    FROM batches b JOIN bank k ON k.utr_extracted = b.utr
),
-- bank dropped the UTR from the narration: fall back to same date + same amount, 1:1 only
by_amount AS (
    SELECT b.*, k.txn_ref, k.credit, k.narration, 'DATE_AND_AMOUNT' AS match_type
    FROM batches b
    JOIN bank k ON k.utr_extracted IS NULL
               AND k.value_date = b.settlement_date
               AND ABS(k.credit - b.batch_total) <= 0.01
    WHERE b.utr NOT IN (SELECT utr FROM by_utr)
      AND (SELECT COUNT(*) FROM bank k2 WHERE k2.utr_extracted IS NULL
             AND k2.value_date = b.settlement_date AND ABS(k2.credit - b.batch_total) <= 0.01) = 1
)
SELECT *, ROUND(credit - batch_total, 2) AS diff,
       CASE WHEN ABS(credit - batch_total) > 0.01 THEN 'BATCH_TOTAL_MISMATCH'
            WHEN match_type = 'DATE_AND_AMOUNT' THEN 'MATCHED_WITHOUT_UTR'
            ELSE 'MATCHED' END AS status
FROM (SELECT * FROM by_utr UNION ALL SELECT * FROM by_amount)
UNION ALL
SELECT b.*, NULL, NULL, NULL, 'NONE', -b.batch_total, 'BATCH_NOT_IN_BANK'
FROM batches b
WHERE b.utr NOT IN (SELECT utr FROM by_utr)
  AND b.utr NOT IN (SELECT utr FROM by_amount);

-- name: unexplained_bank
-- Bank lines that did not match any settlement batch: these need human (or AI) triage.
DROP TABLE IF EXISTS unexplained_bank;
CREATE TABLE unexplained_bank AS
SELECT k.txn_ref, k.value_date, k.narration, k.credit, k.debit
FROM bank_statement_parsed k
WHERE k.txn_ref NOT IN (SELECT txn_ref FROM batch_recon WHERE txn_ref IS NOT NULL);

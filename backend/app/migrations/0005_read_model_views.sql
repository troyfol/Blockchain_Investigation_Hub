-- depends: 0004_investigator_objects
-- Unified value-movement projection over the truthful-asymmetric base tables (decision #4).
-- EVM: a transfer is a directed A->B movement. BTC: an output is value arriving at an address;
-- src_address_id is DELIBERATELY NULL because which input funded it is NOT a ledger fact.
-- (docs/schema.md §3, verbatim.)

CREATE VIEW v_value_movement AS
  SELECT
    'evm'        AS paradigm,
    tr.id        AS movement_id,
    'transfer'   AS movement_kind,
    tr.transaction_id,
    tr.chain,
    tr.from_address_id AS src_address_id,
    tr.to_address_id   AS dst_address_id,
    tr.asset_id,
    tr.amount,
    tr.position,
    tx.finality_status
  FROM transfer tr
  JOIN transaction_ tx ON tx.id = tr.transaction_id
  UNION ALL
  SELECT
    'utxo'       AS paradigm,
    o.id         AS movement_id,
    'tx_output'  AS movement_kind,
    o.transaction_id,
    tx.chain,
    NULL         AS src_address_id,          -- never fabricate input->output
    o.address_id AS dst_address_id,
    (SELECT a.id FROM asset a
       WHERE a.chain = tx.chain AND a.contract_address IS NULL) AS asset_id,  -- native coin
    o.amount,
    o.output_index AS position,
    tx.finality_status
  FROM tx_output o
  JOIN transaction_ tx ON tx.id = o.transaction_id;

-- Optional helper: per-address net flow with USD when valued. Built on the view above.
CREATE VIEW v_address_flow AS
  SELECT
    m.dst_address_id AS address_id,
    m.chain,
    m.asset_id,
    m.amount,
    v.value AS usd_value
  FROM v_value_movement m
  LEFT JOIN valuation v
    ON v.subject_type = (CASE m.paradigm WHEN 'evm' THEN 'transfer' ELSE 'tx_output' END)
   AND v.subject_id   = m.movement_id;

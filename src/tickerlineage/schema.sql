-- Temporal identifier graph. PostgreSQL.
--
-- The only thing that matters here: an edge carries a VALIDITY RANGE, not a
-- date. "AAPL -> CIK 320193" is not a fact; "AAPL -> CIK 320193 during
-- [2017-08-28, ...)" is. Every join against this table must be time-qualified,
-- and the exclusion constraint makes an untimed contradiction impossible to
-- insert rather than merely unlikely.

CREATE TABLE IF NOT EXISTS snapshot (
    i             INTEGER PRIMARY KEY,
    quarter       TEXT    NOT NULL,
    wayback_date  DATE    NOT NULL,
    file_rows     INTEGER NOT NULL,
    regime        TEXT    NOT NULL DEFAULT 'stable'
                  CHECK (regime IN ('stable', 'artifact', 'rebuild', 'pre_expansion', 'post_expansion'))
);

CREATE TABLE IF NOT EXISTS identifier_edge (
    id          BIGSERIAL   PRIMARY KEY,
    cik         TEXT        NOT NULL,
    id_type     TEXT        NOT NULL CHECK (id_type IN ('ticker','name','figi','exchange')),
    id_value    TEXT        NOT NULL,
    title       TEXT,
    valid_from  DATE        NOT NULL,
    valid_to    DATE,                          -- NULL = still current
    source      TEXT        NOT NULL,          -- provenance is not optional
    confidence  NUMERIC(4,3) NOT NULL DEFAULT 1.000
                CHECK (confidence >= 0 AND confidence <= 1),
    resolved_by TEXT        NOT NULL DEFAULT 'deterministic'
                CHECK (resolved_by IN ('deterministic','normalized','fuzzy','unresolved')),
    CHECK (valid_to IS NULL OR valid_to > valid_from),
    UNIQUE (cik, id_type, id_value, valid_from)
);

-- Generated range column: the queries read cleanly and the index is on the
-- thing actually being queried.
ALTER TABLE identifier_edge
    ADD COLUMN IF NOT EXISTS validity daterange
    GENERATED ALWAYS AS (daterange(valid_from, valid_to, '[)')) STORED;

CREATE INDEX IF NOT EXISTS idx_edge_validity  ON identifier_edge USING gist (validity);
CREATE INDEX IF NOT EXISTS idx_edge_lookup    ON identifier_edge (id_type, id_value);
CREATE INDEX IF NOT EXISTS idx_edge_cik       ON identifier_edge (cik);

-- The constraint that earns its keep: one ticker cannot belong to two CIKs at
-- the same instant. Ticker REUSE is legal (disjoint ranges); ticker
-- AMBIGUITY is not. Requires btree_gist.
CREATE EXTENSION IF NOT EXISTS btree_gist;
ALTER TABLE identifier_edge
    ADD CONSTRAINT no_concurrent_ticker_owners
    EXCLUDE USING gist (id_value WITH =, validity WITH &&)
    WHERE (id_type = 'ticker');

-- ---------------------------------------------------------------- queries

-- What did this ticker mean on a given date?
--   SELECT cik, title FROM identifier_edge
--   WHERE id_type='ticker' AND id_value='S' AND validity @> DATE '2019-06-30';
--   -> Sprint Corporation
--   ... AND validity @> DATE '2024-06-30';
--   -> SentinelOne, Inc.

-- Every ticker ever held by more than one CIK.
CREATE OR REPLACE VIEW ticker_reuse AS
SELECT id_value AS ticker,
       count(DISTINCT cik)                        AS distinct_ciks,
       min(valid_from)                            AS first_seen,
       max(coalesce(valid_to, CURRENT_DATE))      AS last_seen,
       array_agg(DISTINCT title)                  AS titles
FROM identifier_edge
WHERE id_type = 'ticker'
GROUP BY id_value
HAVING count(DISTINCT cik) > 1;

-- CIKs whose history a ticker-keyed join would fracture: several tickers,
-- none of them overlapping in time.
CREATE OR REPLACE VIEW renamed_entities AS
SELECT e.cik,
       min(e.title)                  AS title,
       array_agg(DISTINCT e.id_value) AS tickers,
       count(DISTINCT e.id_value)     AS n_tickers
FROM identifier_edge e
WHERE e.id_type = 'ticker'
GROUP BY e.cik
HAVING count(DISTINCT e.id_value) > 1
   AND NOT EXISTS (
        SELECT 1 FROM identifier_edge a
        JOIN identifier_edge b
          ON a.cik = b.cik AND a.id_value <> b.id_value AND a.validity && b.validity
        WHERE a.cik = e.cik AND a.id_type = 'ticker' AND b.id_type = 'ticker'
   );

-- The naive-join cost, in SQL. Observations attributed to the first-seen CIK
-- that actually belonged to a later one.
CREATE OR REPLACE VIEW naive_join_cost AS
WITH first_owner AS (
    SELECT DISTINCT ON (id_value) id_value, cik
    FROM identifier_edge WHERE id_type = 'ticker'
    ORDER BY id_value, valid_from
)
SELECT
    count(*) FILTER (WHERE e.cik <> f.cik) AS misattributed_edges,
    count(*)                               AS total_edges,
    round(100.0 * count(*) FILTER (WHERE e.cik <> f.cik) / nullif(count(*), 0), 3) AS pct
FROM identifier_edge e
JOIN first_owner f USING (id_value)
WHERE e.id_type = 'ticker';

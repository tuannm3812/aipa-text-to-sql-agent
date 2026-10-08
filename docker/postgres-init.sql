-- The agent connects as this role. It can read every table in the sample
-- database and nothing else: no write, no DDL, and none of the file-reading
-- privileges (pg_read_server_files, pg_execute_server_program) that would let
-- pg_read_file() or COPY ... FROM PROGRAM reach the host filesystem.
CREATE ROLE aipa_ro LOGIN PASSWORD 'aipa_ro_pw';
GRANT CONNECT ON DATABASE aipa TO aipa_ro;
GRANT USAGE ON SCHEMA public TO aipa_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO aipa_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO aipa_ro;

-- `customers` stays exactly (customer_id, name) with exactly these two rows:
-- `tests/test_engine_conformance.py` and `tests/test_engine_postgres.py` both
-- pin the two-column positional form of `INSERT INTO customers VALUES (...)`
-- (to prove it is refused) and the exact `[('Alice',), ('Bob',)]` row content.
-- Do not add a column here or change/add a row - do that on `sales` instead,
-- which nothing pins.
CREATE TABLE customers (customer_id INTEGER PRIMARY KEY, name TEXT);
INSERT INTO customers VALUES (1, 'Alice'), (2, 'Bob');

-- `sales` carries every column Task 4's analytics corpus
-- (`tests/test_engine_postgres.py::ANALYTICS_CORPUS`) needs to ask a
-- realistic business question - aggregates, GROUP BY, date bucketing, CASE,
-- and ILIKE text matching over `category`/`status` - since `customers` above
-- cannot grow columns without breaking the pins noted there. Nothing pins a
-- specific row count or content for `sales` the way `customers` is pinned;
-- `test_engine_conformance.py` only checks the table and its columns exist.
CREATE TABLE sales (
    sale_id INTEGER PRIMARY KEY,
    customer_id INTEGER REFERENCES customers(customer_id),
    sale_date DATE,
    amount NUMERIC(10, 2),
    category TEXT,
    status TEXT,
    region TEXT
);
INSERT INTO sales (sale_id, customer_id, sale_date, amount, category, status, region) VALUES
    (1, 1, '2024-01-05', 100.00, 'widgets', 'completed', 'north'),
    (2, 1, '2024-01-20', 250.50, 'gadgets', 'completed', 'north'),
    (3, 2, '2024-02-10', 75.00, 'widgets', 'refunded', 'south'),
    (4, 2, '2024-02-15', 400.00, 'gizmos', 'completed', 'south'),
    (5, 1, '2024-02-28', 60.25, 'widgets', 'completed', 'north'),
    (6, 2, '2024-03-03', NULL, 'gadgets', 'pending', 'south'),
    (7, 1, '2024-03-10', 310.00, 'gizmos', 'completed', 'north'),
    (8, 2, '2024-03-18', 45.00, 'widgets', 'completed', 'south'),
    (9, 1, '2024-04-02', 220.75, 'gadgets', 'refunded', 'north'),
    (10, 2, '2024-04-09', 500.00, 'gizmos', 'completed', 'south'),
    (11, 1, '2024-04-21', 90.00, 'widgets', 'completed', 'north'),
    (12, 2, '2024-05-01', 150.00, 'gadgets', 'completed', 'south');

GRANT SELECT ON customers, sales TO aipa_ro;

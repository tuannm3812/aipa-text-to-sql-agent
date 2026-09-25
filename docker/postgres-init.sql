-- The agent connects as this role. It can read every table in the sample
-- database and nothing else: no write, no DDL, and none of the file-reading
-- privileges (pg_read_server_files, pg_execute_server_program) that would let
-- pg_read_file() or COPY ... FROM PROGRAM reach the host filesystem.
CREATE ROLE aipa_ro LOGIN PASSWORD 'aipa_ro_pw';
GRANT CONNECT ON DATABASE aipa TO aipa_ro;
GRANT USAGE ON SCHEMA public TO aipa_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO aipa_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO aipa_ro;

CREATE TABLE customers (customer_id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE sales (
    sale_id INTEGER PRIMARY KEY,
    customer_id INTEGER REFERENCES customers(customer_id)
);
INSERT INTO customers VALUES (1, 'Alice'), (2, 'Bob');
GRANT SELECT ON customers, sales TO aipa_ro;

-- Minimal PostgreSQL bootstrap. Run as a PostgreSQL superuser if database/user do not exist yet.
-- Adjust password before use.

CREATE USER grandgousier WITH PASSWORD 'glodga';
CREATE DATABASE grandgousier OWNER grandgousier;
GRANT ALL PRIVILEGES ON DATABASE grandgousier TO grandgousier;

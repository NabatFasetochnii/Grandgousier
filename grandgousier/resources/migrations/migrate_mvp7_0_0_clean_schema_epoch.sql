-- Grandgousier 0.7.0 is a fresh-baseline clean-schema epoch.
-- The schema itself is created from current SQLAlchemy metadata by init-db.
-- This managed marker exists so the migration ledger records the epoch;
-- migrate-db refuses pre-0.7 compatibility schemas before this is applied.
SELECT 1;

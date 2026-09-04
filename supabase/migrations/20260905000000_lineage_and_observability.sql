-- Phase 7: one lineage, joinable across the tables that already existed.
--
-- The commerce-core migration is `create table if not exists`, so it cannot add a
-- column to a table an earlier run already created. These are the same columns
-- 20260904010000_commerce_core.sql now declares, applied additively to a database
-- that predates them. Regenerating the core migration does not replace this file.

alter table cartisan.commerce_orders  add column if not exists correlation_id text;
alter table cartisan.commerce_orders  add column if not exists demo_run_id text;
alter table cartisan.payment_attempts add column if not exists correlation_id text;
alter table cartisan.payment_attempts add column if not exists demo_run_id text;
alter table cartisan.inbox_events     add column if not exists correlation_id text;
alter table cartisan.turns            add column if not exists correlation_id text;
alter table cartisan.turns            add column if not exists demo_run_id text;

create index if not exists commerce_orders_correlation_idx on cartisan.commerce_orders (correlation_id);
create index if not exists turns_correlation_idx on cartisan.turns (correlation_id);
create index if not exists evidence_demo_run_idx on cartisan.evidence_records (demo_run_id, recorded_at);
create index if not exists evidence_surface_idx on cartisan.evidence_records (surface, recorded_at);

-- Cartisan's backend is the only database client. Browser-facing Supabase roles
-- receive no policies, so these tables are not exposed through PostgREST.
create schema if not exists cartisan;

create table if not exists cartisan.carts (
  session_id text not null,
  product_id text not null,
  quantity integer not null check (quantity between 1 and 10),
  primary key (session_id, product_id)
);

create table if not exists cartisan.orders (
  id text primary key,
  session_id text not null,
  status text not null,
  amount integer not null check (amount >= 0),
  payment_link_id text,
  payment_url text,
  payload text not null,
  created_at text not null
);

create index if not exists orders_session_created_idx
  on cartisan.orders (session_id, created_at desc);
create index if not exists orders_payment_link_idx
  on cartisan.orders (payment_link_id)
  where payment_link_id is not null;

create table if not exists cartisan.approvals (
  id text primary key,
  kind text not null,
  target_id text,
  before_json text not null,
  after_json text not null,
  reasoning text not null,
  status text not null check (status in ('pending', 'approved', 'rejected')),
  created_at text not null,
  decided_at text
);

create index if not exists approvals_pending_created_idx
  on cartisan.approvals (created_at)
  where status = 'pending';

create table if not exists cartisan.audit (
  id text primary key,
  timestamp text not null,
  session_id text not null,
  agent text not null,
  action text not null,
  reasoning text not null,
  outcome text not null,
  gated integer not null check (gated in (0, 1)),
  result_json text not null
);

create index if not exists audit_timestamp_idx
  on cartisan.audit (timestamp desc);
create index if not exists audit_agent_timestamp_idx
  on cartisan.audit (agent, timestamp desc);

alter table cartisan.carts enable row level security;
alter table cartisan.orders enable row level security;
alter table cartisan.approvals enable row level security;
alter table cartisan.audit enable row level security;

revoke all on schema cartisan from anon, authenticated;
revoke all on all tables in schema cartisan from anon, authenticated;

-- Phase 1: establish authority (CARTISAN_COMMERCE_ARCHITECTURE.md, ADR 0010/0022/0029).
--
-- Identity comes from Supabase Auth. The backend derives the principal from a
-- verified access token; a client-supplied shopper id is never authority. A
-- customer has exactly one durable active cart, independent of any conversation.

-- ---------------------------------------------------------------- principals

create table if not exists cartisan.customers (
  id uuid primary key references auth.users (id) on delete cascade,
  email text not null,
  display_name text,
  origin text not null default 'live_app' check (origin in ('seeded', 'live_app', 'razorpay_test')),
  created_at timestamptz not null default now()
);

create table if not exists cartisan.merchant_operators (
  id uuid primary key references auth.users (id) on delete cascade,
  email text not null,
  display_name text,
  created_at timestamptz not null default now()
);

-- ---------------------------------------------------------- customer carts

create table if not exists cartisan.customer_carts (
  id text primary key,
  customer_id uuid not null references cartisan.customers (id) on delete cascade,
  status text not null default 'active' check (status in ('active', 'checked_out', 'abandoned')),
  -- Optimistic concurrency: every mutation bumps this, and a mutation carrying an
  -- expected version that no longer matches is a conflict rather than a lost update.
  state_version integer not null default 0 check (state_version >= 0),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

-- "A customer has one durable active cart independent of conversation identity."
create unique index if not exists customer_carts_one_active_idx
  on cartisan.customer_carts (customer_id)
  where status = 'active';

create table if not exists cartisan.cart_lines (
  cart_id text not null references cartisan.customer_carts (id) on delete cascade,
  product_id text not null,
  quantity integer not null check (quantity between 1 and 10),
  primary key (cart_id, product_id)
);

-- ------------------------------------------------------------- idempotency

-- Mutations require an idempotency key. A replay of the same key with the same
-- request returns the first recorded response instead of applying the effect twice.
-- The key is chosen by the client, so it is unique per principal rather than
-- globally: one customer's key must never collide with another's.
create table if not exists cartisan.idempotency_records (
  key text not null,
  principal_id uuid not null,
  operation text not null,
  request_fingerprint text not null,
  response_json text not null,
  created_at timestamptz not null default now(),
  primary key (principal_id, key)
);

create index if not exists idempotency_principal_created_idx
  on cartisan.idempotency_records (principal_id, created_at desc);

-- ------------------------------------------------------ order ownership

alter table cartisan.orders
  add column if not exists customer_id uuid references cartisan.customers (id);

alter table cartisan.orders
  add column if not exists origin text not null default 'live_app'
  check (origin in ('seeded', 'live_app', 'razorpay_test'));

create index if not exists orders_customer_created_idx
  on cartisan.orders (customer_id, created_at desc);

-- --------------------------------------------------------------------- RLS

-- The backend is the only writer and connects with a role that bypasses RLS.
-- These policies are the browser-facing second line of defence: if a table is
-- ever reachable through PostgREST, a signed-in principal still sees only its
-- own rows, and can never write.
alter table cartisan.customers enable row level security;
alter table cartisan.merchant_operators enable row level security;
alter table cartisan.customer_carts enable row level security;
alter table cartisan.cart_lines enable row level security;
alter table cartisan.idempotency_records enable row level security;

drop policy if exists customers_select_self on cartisan.customers;
create policy customers_select_self on cartisan.customers
  for select to authenticated using (id = auth.uid());

drop policy if exists customer_carts_select_own on cartisan.customer_carts;
create policy customer_carts_select_own on cartisan.customer_carts
  for select to authenticated using (customer_id = auth.uid());

drop policy if exists cart_lines_select_own on cartisan.cart_lines;
create policy cart_lines_select_own on cartisan.cart_lines
  for select to authenticated using (
    exists (
      select 1 from cartisan.customer_carts c
      where c.id = cart_lines.cart_id and c.customer_id = auth.uid()
    )
  );

drop policy if exists orders_select_own on cartisan.orders;
create policy orders_select_own on cartisan.orders
  for select to authenticated using (customer_id = auth.uid());

revoke all on all tables in schema cartisan from anon, authenticated;
grant usage on schema cartisan to authenticated;
grant select on cartisan.customers, cartisan.customer_carts, cartisan.cart_lines, cartisan.orders
  to authenticated;

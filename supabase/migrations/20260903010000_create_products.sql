create table if not exists cartisan.products (
  id text primary key,
  name text not null,
  category text not null,
  description text not null,
  price integer not null check (price > 0),
  stock integer not null check (stock >= 0),
  rating text,
  image_label text not null,
  cross_sell_of text,
  variant_of text,
  options_json text,
  option_values_json text,
  active integer not null default 1 check (active in (0, 1))
);

create index if not exists products_category_active_idx
  on cartisan.products (category, active);
create index if not exists products_variant_idx
  on cartisan.products (variant_of)
  where variant_of is not null;
create index if not exists products_cross_sell_idx
  on cartisan.products (cross_sell_of)
  where cross_sell_of is not null;

alter table cartisan.products enable row level security;
revoke all on cartisan.products from anon, authenticated;

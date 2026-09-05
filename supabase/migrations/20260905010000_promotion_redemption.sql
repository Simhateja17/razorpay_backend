-- Campaign attribution: record which promotion explains a discount.
--
-- The commerce-core migration is `create table if not exists`, so it cannot add a
-- column to a table an earlier run already created. These are the same columns
-- 20260904010000_commerce_core.sql now declares, applied additively to a database
-- that predates them. Regenerating the core migration does not replace this file.
--
-- Before this, a campaign carried a promotion code and an order carried a discount
-- amount, with nothing joining the two — so attributed orders and attributed revenue
-- could not be reported at all. An order names the promotion it actually redeemed,
-- which makes attribution a recorded redemption rather than an inference.

alter table cartisan.promotions      add column if not exists category_id text
  references cartisan.catalog_categories (id);
alter table cartisan.checkout_stages add column if not exists promotion_id text
  references cartisan.promotions (id);
alter table cartisan.commerce_orders add column if not exists promotion_id text
  references cartisan.promotions (id);

create index if not exists commerce_orders_promotion_idx
  on cartisan.commerce_orders (promotion_id, created_at);

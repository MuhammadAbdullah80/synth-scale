-- Adversarial fixture ("hard.sql") for the Synth-Scale datagen engine.
--
-- A realistic Supabase-style SaaS + e-commerce schema that deliberately
-- includes every hard case the original sample.sql fixture dodged
-- (see REVIEW_REPORT.md):
--
--   * UUID primary keys (Supabase default)           -> organizations, users,
--     products, subscriptions, orders
--   * NUMERIC(4,2) and NUMERIC(6,2) columns          -> products.tax_rate,
--     coupons.percent_off, products.price, orders.tax, order_items.unit_price
--   * date CHECK: col >= 'literal'                   -> coupons.valid_from,
--     orders.placed_on
--   * date CHECK: col BETWEEN 'a' AND 'b'            -> coupons.valid_until
--   * mixed composite UNIQUE (fk_col, plain_col)     -> products (org_id, sku)
--   * INT two-column CHECK                           -> inventory (max_qty > min_qty)
--   * NOT NULL column in a two-column CHECK with a
--     nullable partner                               -> subscriptions.trial_end
--     (NOT NULL) vs trial_start (nullable)
--   * enum via CHECK IN (...)                        -> products.status,
--     subscriptions.plan, orders.status
--   * junction table, composite PK of two FKs        -> order_items
--   * self-referencing FK                            -> users.referred_by
--   * tight VARCHAR lengths                          -> categories.code VARCHAR(4),
--     products.sku VARCHAR(8), inventory.warehouse VARCHAR(10)
--   * created_at / updated_at TIMESTAMP columns      -> most tables
--   * exotic CHECK the structural parser won't
--     recognize                                      -> order_items
--     (quantity * unit_price < 2000000; violated ~half the time by naive
--     unconstrained generation, easily satisfiable by a correct engine)

CREATE TABLE organizations (
  id UUID PRIMARY KEY,
  name VARCHAR(80) NOT NULL,
  slug VARCHAR(30) UNIQUE NOT NULL,
  created_at TIMESTAMP NOT NULL,
  updated_at TIMESTAMP
);

CREATE TABLE users (
  id UUID PRIMARY KEY,
  org_id UUID NOT NULL REFERENCES organizations(id),
  email VARCHAR(120) UNIQUE NOT NULL,
  full_name VARCHAR(60),
  referred_by UUID REFERENCES users(id),
  created_at TIMESTAMP NOT NULL,
  updated_at TIMESTAMP
);

CREATE TABLE categories (
  id SERIAL PRIMARY KEY,
  name VARCHAR(40) NOT NULL,
  code VARCHAR(4) NOT NULL
);

CREATE TABLE products (
  id UUID PRIMARY KEY,
  org_id UUID NOT NULL REFERENCES organizations(id),
  category_id INT REFERENCES categories(id),
  sku VARCHAR(8) NOT NULL,
  name VARCHAR(60) NOT NULL,
  price NUMERIC(6,2) NOT NULL CHECK (price > 0),
  tax_rate NUMERIC(4,2) NOT NULL CHECK (tax_rate >= 0),
  status VARCHAR(12) NOT NULL CHECK (status IN ('draft', 'active', 'archived')),
  created_at TIMESTAMP NOT NULL,
  UNIQUE (org_id, sku)
);

CREATE TABLE inventory (
  id SERIAL PRIMARY KEY,
  product_id UUID NOT NULL REFERENCES products(id),
  warehouse VARCHAR(10) NOT NULL,
  min_qty INT NOT NULL CHECK (min_qty >= 0),
  max_qty INT NOT NULL,
  CONSTRAINT chk_qty_window CHECK (max_qty > min_qty)
);

CREATE TABLE coupons (
  id SERIAL PRIMARY KEY,
  code VARCHAR(10) UNIQUE NOT NULL,
  percent_off NUMERIC(4,2) NOT NULL CHECK (percent_off > 0),
  valid_from DATE NOT NULL CHECK (valid_from >= '2024-01-01'),
  valid_until DATE NOT NULL CHECK (valid_until BETWEEN '2024-01-01' AND '2026-12-31'),
  max_uses INT
);

CREATE TABLE subscriptions (
  id UUID PRIMARY KEY,
  org_id UUID NOT NULL REFERENCES organizations(id),
  plan VARCHAR(10) NOT NULL CHECK (plan IN ('free', 'pro', 'team')),
  trial_start DATE,
  trial_end DATE NOT NULL,
  seats INT NOT NULL CHECK (seats > 0),
  created_at TIMESTAMP NOT NULL,
  CONSTRAINT chk_trial CHECK (trial_end > trial_start)
);

CREATE TABLE orders (
  id UUID PRIMARY KEY,
  user_id UUID NOT NULL REFERENCES users(id),
  status VARCHAR(10) NOT NULL CHECK (status IN ('pending', 'paid', 'shipped', 'refunded')),
  subtotal NUMERIC(8,2) NOT NULL CHECK (subtotal >= 0),
  tax NUMERIC(6,2) NOT NULL CHECK (tax >= 0),
  placed_on DATE NOT NULL CHECK (placed_on >= '2023-01-01'),
  created_at TIMESTAMP NOT NULL,
  updated_at TIMESTAMP
);

CREATE TABLE order_items (
  order_id UUID NOT NULL REFERENCES orders(id),
  product_id UUID NOT NULL REFERENCES products(id),
  quantity INT NOT NULL CHECK (quantity > 0),
  unit_price NUMERIC(6,2) NOT NULL CHECK (unit_price > 0),
  PRIMARY KEY (order_id, product_id),
  CONSTRAINT chk_line_total CHECK (quantity * unit_price < 2000000)
);

CREATE TABLE audit_log (
  id BIGSERIAL PRIMARY KEY,
  org_id UUID NOT NULL REFERENCES organizations(id),
  actor_id UUID REFERENCES users(id),
  action VARCHAR(16) NOT NULL,
  entity VARCHAR(12) NOT NULL,
  created_at TIMESTAMP NOT NULL
);

-- ============================================================================
-- demo_saas.sql — THE Demo Day schema for Synth-Scale (15 tables).
--
-- A believable B2B SaaS + commerce hybrid ("Acme Cloud" sells seats AND a
-- storefront), built to exercise ON PURPOSE every hard case the engine is
-- good at:
--
--   * UUID primary keys (Supabase default)      -> organizations, users,
--       projects, api_keys, subscriptions, orders, support_tickets
--   * deep FK chain, 4 levels                   -> organizations -> teams
--       -> projects -> api_keys
--   * junction table, composite PK of two FKs   -> team_members
--   * self-referencing FKs                      -> users.referred_by,
--       support_tickets.parent_ticket_id
--   * NUMERIC(6,2) / NUMERIC(8,2) money         -> plans.monthly_price,
--       products.price, orders.subtotal, order_items.unit_price,
--       invoices.amount_due, invoice_items.amount
--   * date CHECK: BETWEEN literals              -> subscriptions.current_period_start
--   * date CHECK: col >= literal                -> subscriptions.trial_ends_on,
--       invoices.issued_on
--   * two-column CHECK (constructed, not retried)
--       -> subscriptions (current_period_end > current_period_start)
--       -> teams (max_seats >= used_seats)
--   * enums via CHECK IN (...)                  -> status on projects,
--       api_keys, subscriptions, invoices, orders, support_tickets;
--       team_members.role, plans.billing_period, support_tickets.priority
--   * mixed composite UNIQUE (fk_col, plain)    -> projects (org_id, slug)
--   * tight VARCHARs                            -> api_keys.prefix VARCHAR(8),
--       invoices.currency VARCHAR(3), audit_logs.entity_type VARCHAR(12),
--       products.tier VARCHAR(10)
--   * created_at / updated_at                   -> every table (chronology
--       coherence: updated_at >= created_at, child newer than parent)
--   * correlated pools                          -> users.first_name + users.gender
--       (person pool), users.city + users.country (geo pool),
--       products.name + products.category + products.tier + products.price
--       (products pool: tier-consistent price bands; tier has NO CHECK on
--       purpose — an enum CHECK would override the pool binding)
--   * status-gated lifecycle columns            -> orders.shipped_at /
--       delivered_at / cancelled_at filled only when status says so
--   * zipfian FK fan-out                        -> a few power users own most
--       orders; a few orgs own most audit_logs
-- ============================================================================

CREATE TABLE organizations (
  id UUID PRIMARY KEY,
  name VARCHAR(80) NOT NULL,
  slug VARCHAR(30) UNIQUE NOT NULL,
  created_at TIMESTAMP NOT NULL,
  updated_at TIMESTAMP
);

CREATE TABLE plans (
  id SERIAL PRIMARY KEY,
  name VARCHAR(40) NOT NULL,
  monthly_price NUMERIC(6,2) NOT NULL CHECK (monthly_price >= 0),
  seat_limit INT NOT NULL CHECK (seat_limit > 0),
  billing_period VARCHAR(10) NOT NULL CHECK (billing_period IN ('monthly', 'yearly')),
  created_at TIMESTAMP NOT NULL,
  updated_at TIMESTAMP
);

CREATE TABLE users (
  id UUID PRIMARY KEY,
  org_id UUID NOT NULL REFERENCES organizations(id),
  email VARCHAR(120) UNIQUE NOT NULL,
  first_name VARCHAR(40) NOT NULL,
  last_name VARCHAR(40),
  gender VARCHAR(10),
  city VARCHAR(40),
  country VARCHAR(40),
  referred_by UUID REFERENCES users(id),
  is_active BOOLEAN NOT NULL,
  created_at TIMESTAMP NOT NULL,
  updated_at TIMESTAMP
);

CREATE TABLE teams (
  id SERIAL PRIMARY KEY,
  org_id UUID NOT NULL REFERENCES organizations(id),
  name VARCHAR(40) NOT NULL,
  max_seats INT NOT NULL CHECK (max_seats > 0),
  used_seats INT NOT NULL,
  created_at TIMESTAMP NOT NULL,
  updated_at TIMESTAMP,
  CONSTRAINT chk_seats CHECK (max_seats >= used_seats)
);

CREATE TABLE team_members (
  team_id INT NOT NULL REFERENCES teams(id),
  user_id UUID NOT NULL REFERENCES users(id),
  role VARCHAR(10) NOT NULL CHECK (role IN ('owner', 'admin', 'member')),
  created_at TIMESTAMP NOT NULL,
  PRIMARY KEY (team_id, user_id)
);

CREATE TABLE projects (
  id UUID PRIMARY KEY,
  org_id UUID NOT NULL REFERENCES organizations(id),
  team_id INT NOT NULL REFERENCES teams(id),
  name VARCHAR(60) NOT NULL,
  slug VARCHAR(30) NOT NULL,
  status VARCHAR(10) NOT NULL CHECK (status IN ('active', 'paused', 'archived')),
  created_at TIMESTAMP NOT NULL,
  updated_at TIMESTAMP,
  UNIQUE (org_id, slug)
);

CREATE TABLE api_keys (
  id UUID PRIMARY KEY,
  project_id UUID NOT NULL REFERENCES projects(id),
  prefix VARCHAR(8) NOT NULL,
  key_hash VARCHAR(64) UNIQUE NOT NULL,
  status VARCHAR(10) NOT NULL CHECK (status IN ('active', 'revoked', 'expired')),
  last_used_at TIMESTAMP,
  created_at TIMESTAMP NOT NULL,
  updated_at TIMESTAMP
);

CREATE TABLE subscriptions (
  id UUID PRIMARY KEY,
  org_id UUID NOT NULL REFERENCES organizations(id),
  plan_id INT NOT NULL REFERENCES plans(id),
  status VARCHAR(12) NOT NULL CHECK (status IN ('trialing', 'active', 'past_due', 'cancelled')),
  trial_ends_on DATE NOT NULL CHECK (trial_ends_on >= '2024-06-01'),
  current_period_start DATE NOT NULL CHECK (current_period_start BETWEEN '2024-01-01' AND '2026-12-31'),
  current_period_end DATE NOT NULL,
  created_at TIMESTAMP NOT NULL,
  updated_at TIMESTAMP,
  CONSTRAINT chk_period CHECK (current_period_end > current_period_start)
);

CREATE TABLE invoices (
  id SERIAL PRIMARY KEY,
  subscription_id UUID NOT NULL REFERENCES subscriptions(id),
  org_id UUID NOT NULL REFERENCES organizations(id),
  status VARCHAR(14) NOT NULL CHECK (status IN ('draft', 'open', 'paid', 'void', 'uncollectible')),
  amount_due NUMERIC(8,2) NOT NULL CHECK (amount_due >= 0),
  currency VARCHAR(3) NOT NULL,
  issued_on DATE NOT NULL CHECK (issued_on >= '2024-01-01'),
  created_at TIMESTAMP NOT NULL,
  updated_at TIMESTAMP
);

CREATE TABLE invoice_items (
  id SERIAL PRIMARY KEY,
  invoice_id INT NOT NULL REFERENCES invoices(id),
  description VARCHAR(80) NOT NULL,
  quantity INT NOT NULL CHECK (quantity > 0),
  amount NUMERIC(8,2) NOT NULL CHECK (amount >= 0),
  created_at TIMESTAMP NOT NULL
);

CREATE TABLE products (
  id SERIAL PRIMARY KEY,
  org_id UUID NOT NULL REFERENCES organizations(id),
  product_name VARCHAR(60) NOT NULL,
  category VARCHAR(30) NOT NULL,
  tier VARCHAR(10) NOT NULL,
  price NUMERIC(6,2) NOT NULL CHECK (price > 0),
  status VARCHAR(10) NOT NULL CHECK (status IN ('draft', 'active', 'archived')),
  created_at TIMESTAMP NOT NULL,
  updated_at TIMESTAMP
);

CREATE TABLE orders (
  id UUID PRIMARY KEY,
  user_id UUID NOT NULL REFERENCES users(id),
  status VARCHAR(10) NOT NULL CHECK (status IN ('pending', 'paid', 'shipped', 'delivered', 'cancelled')),
  subtotal NUMERIC(8,2) NOT NULL CHECK (subtotal >= 0),
  shipped_at TIMESTAMP,
  delivered_at TIMESTAMP,
  cancelled_at TIMESTAMP,
  created_at TIMESTAMP NOT NULL,
  updated_at TIMESTAMP
);

CREATE TABLE order_items (
  order_id UUID NOT NULL REFERENCES orders(id),
  product_id INT NOT NULL REFERENCES products(id),
  quantity INT NOT NULL CHECK (quantity > 0),
  unit_price NUMERIC(6,2) NOT NULL CHECK (unit_price > 0),
  created_at TIMESTAMP NOT NULL,
  PRIMARY KEY (order_id, product_id)
);

CREATE TABLE support_tickets (
  id UUID PRIMARY KEY,
  org_id UUID NOT NULL REFERENCES organizations(id),
  opened_by UUID NOT NULL REFERENCES users(id),
  assigned_to UUID REFERENCES users(id),
  parent_ticket_id UUID REFERENCES support_tickets(id),
  subject VARCHAR(80) NOT NULL,
  status VARCHAR(10) NOT NULL CHECK (status IN ('open', 'pending', 'resolved', 'closed')),
  priority VARCHAR(8) NOT NULL CHECK (priority IN ('low', 'medium', 'high', 'urgent')),
  created_at TIMESTAMP NOT NULL,
  updated_at TIMESTAMP
);

CREATE TABLE audit_logs (
  id BIGSERIAL PRIMARY KEY,
  org_id UUID NOT NULL REFERENCES organizations(id),
  actor_id UUID REFERENCES users(id),
  action VARCHAR(16) NOT NULL,
  entity_type VARCHAR(12) NOT NULL,
  created_at TIMESTAMP NOT NULL
);

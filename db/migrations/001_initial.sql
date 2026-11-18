-- Elevate Manager - Phase 1 schema (PostgreSQL 14+)
-- Tested on PostgreSQL 16 together with 002 (see tests/test_schema.py).
-- Conventions:
--   * All timestamps are timestamptz (stored in UTC, displayed in Asia/Kolkata)
--   * All money is integer paise (never floats). 1 rupee = 100 paise
--   * Tax rates are basis points (bps). 1800 = 18.00%. Default 0, nothing invented

CREATE EXTENSION IF NOT EXISTS btree_gist;
CREATE EXTENSION IF NOT EXISTS citext;

CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END $$ LANGUAGE plpgsql;

-- 1. Users and roles (staff logins only; customers do not log in)
CREATE TABLE users (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  email         citext NOT NULL UNIQUE,
  password_hash text   NOT NULL,
  full_name     text   NOT NULL,
  role          text   NOT NULL CHECK (role IN ('owner','receptionist','trainer')),
  is_active     boolean NOT NULL DEFAULT true,
  last_login_at timestamptz,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);

-- 2. Customers
CREATE TABLE customers (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  full_name        text NOT NULL,
  phone_e164       text NOT NULL UNIQUE
                   CHECK (phone_e164 ~ '^\+[1-9][0-9]{7,14}$'),
  email            citext,
  whatsapp_opt_in  boolean NOT NULL DEFAULT false,
  opt_in_at        timestamptz,
  notes            text,
  anonymized_at    timestamptz,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now()
);

-- 3. Trainers, services, and which trainer can teach what
CREATE TABLE trainers (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id    uuid UNIQUE REFERENCES users(id),
  full_name  text NOT NULL,
  phone_e164 text,
  is_active  boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE services (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name              text NOT NULL UNIQUE,
  description       text,
  duration_minutes  integer NOT NULL CHECK (duration_minutes > 0),
  default_capacity  integer NOT NULL DEFAULT 1 CHECK (default_capacity > 0),
  price_paise       integer NOT NULL CHECK (price_paise >= 0),
  tax_rate_bps      integer NOT NULL DEFAULT 0 CHECK (tax_rate_bps BETWEEN 0 AND 10000),
  is_active         boolean NOT NULL DEFAULT true,
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now()
);

-- Trainer-to-service mapping: a trainer can only be booked for services listed here.
CREATE TABLE trainer_services (
  trainer_id uuid NOT NULL REFERENCES trainers(id) ON DELETE CASCADE,
  service_id uuid NOT NULL REFERENCES services(id) ON DELETE CASCADE,
  PRIMARY KEY (trainer_id, service_id)
);

-- Weekly recurring availability (0 = Sunday ... 6 = Saturday), in business-local time
CREATE TABLE trainer_working_hours (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  trainer_id uuid NOT NULL REFERENCES trainers(id) ON DELETE CASCADE,
  weekday    smallint NOT NULL CHECK (weekday BETWEEN 0 AND 6),
  start_time time NOT NULL,
  end_time   time NOT NULL,
  CHECK (end_time > start_time)
);
CREATE INDEX idx_twh_trainer ON trainer_working_hours (trainer_id, weekday);

-- One-off unavailability (leave, sickness)
CREATE TABLE trainer_time_off (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  trainer_id uuid NOT NULL REFERENCES trainers(id) ON DELETE CASCADE,
  starts_at  timestamptz NOT NULL,
  ends_at    timestamptz NOT NULL,
  reason     text,
  CHECK (ends_at > starts_at)
);
CREATE INDEX idx_tto_trainer ON trainer_time_off (trainer_id, starts_at);

-- 4. Business configuration: hours, holidays, settings
CREATE TABLE business_hours (
  weekday    smallint PRIMARY KEY CHECK (weekday BETWEEN 0 AND 6),
  is_closed  boolean NOT NULL DEFAULT false,
  open_time  time,
  close_time time,
  CHECK (is_closed OR (open_time IS NOT NULL AND close_time IS NOT NULL AND close_time > open_time))
);

CREATE TABLE business_holidays (
  holiday_date date PRIMARY KEY,
  name         text NOT NULL
);

-- Key/value settings so policies are configurable without code changes.
-- Expected keys (values are jsonb):
--   timezone                    "Asia/Kolkata"
--   slot_interval_minutes       30
--   min_booking_lead_minutes    60
--   max_advance_booking_days    30
--   cancellation_window_hours   6     (decided; see seed.sql)
--   reminder_offsets_minutes    [1440, 120]
--   reminder_quiet_hours        {"start":"21:00","end":"06:00"}   (OWNER TO DECIDE)
--   business_name / address / phone / gstin
--   tax_enabled                 false
CREATE TABLE business_settings (
  key        text PRIMARY KEY,
  value      jsonb NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now(),
  updated_by uuid REFERENCES users(id)
);

-- 5. Sessions: a trainer's time block for a service.
--    Personal training: capacity 1, created when booked.
--    Group classes: capacity N, many customers share one session.
--    The overlap rule lives HERE, enforced by the database itself.
CREATE TABLE sessions (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  service_id uuid NOT NULL REFERENCES services(id),
  trainer_id uuid NOT NULL REFERENCES trainers(id),
  starts_at  timestamptz NOT NULL,
  ends_at    timestamptz NOT NULL,
  capacity   integer NOT NULL CHECK (capacity > 0),
  status     text NOT NULL DEFAULT 'scheduled' CHECK (status IN ('scheduled','cancelled')),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (ends_at > starts_at),
  -- trainer must be mapped to the service
  FOREIGN KEY (trainer_id, service_id) REFERENCES trainer_services (trainer_id, service_id),
  -- a trainer cannot have two overlapping scheduled sessions (half-open ranges, so 9-10 and 10-11 are fine)
  CONSTRAINT no_trainer_overlap EXCLUDE USING gist (
    trainer_id WITH =,
    tstzrange(starts_at, ends_at, '[)') WITH &&
  ) WHERE (status = 'scheduled')
);
CREATE INDEX idx_sessions_starts ON sessions (starts_at);
CREATE INDEX idx_sessions_service ON sessions (service_id, starts_at);

-- 6. Bookings: one customer's seat in a session
CREATE TABLE bookings (
  id                         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  booking_ref                text NOT NULL UNIQUE,
  session_id                 uuid NOT NULL REFERENCES sessions(id),
  customer_id                uuid NOT NULL REFERENCES customers(id),
  status                     text NOT NULL DEFAULT 'confirmed'
                             CHECK (status IN ('confirmed','cancelled','completed','no_show')),
  price_paise                integer NOT NULL CHECK (price_paise >= 0),
  source                     text NOT NULL DEFAULT 'website' CHECK (source IN ('website','admin','phone','whatsapp')),
  created_by_user_id         uuid REFERENCES users(id),
  idempotency_key            text UNIQUE,
  rescheduled_from_booking_id uuid REFERENCES bookings(id),
  cancelled_at               timestamptz,
  cancelled_by_user_id       uuid REFERENCES users(id),
  cancellation_reason        text,
  is_late_cancellation       boolean NOT NULL DEFAULT false,
  created_at                 timestamptz NOT NULL DEFAULT now(),
  updated_at                 timestamptz NOT NULL DEFAULT now(),
  CHECK (status <> 'cancelled' OR cancelled_at IS NOT NULL)
);
-- same customer cannot hold two confirmed seats in the same session
CREATE UNIQUE INDEX uq_booking_customer_session
  ON bookings (session_id, customer_id) WHERE status = 'confirmed';
CREATE INDEX idx_bookings_customer ON bookings (customer_id);
CREATE INDEX idx_bookings_session  ON bookings (session_id);
CREATE INDEX idx_bookings_status   ON bookings (status);

-- Capacity enforcement: lock the session row, count confirmed seats, reject if full.
CREATE OR REPLACE FUNCTION enforce_session_capacity() RETURNS trigger AS $$
DECLARE
  cap   integer;
  sstat text;
  taken integer;
BEGIN
  SELECT capacity, status INTO cap, sstat FROM sessions WHERE id = NEW.session_id FOR UPDATE;
  IF sstat <> 'scheduled' THEN
    RAISE EXCEPTION 'session % is not open for booking', NEW.session_id USING ERRCODE = '23514';
  END IF;
  SELECT count(*) INTO taken FROM bookings
   WHERE session_id = NEW.session_id AND status = 'confirmed' AND id <> NEW.id;
  IF taken >= cap THEN
    RAISE EXCEPTION 'session % is full', NEW.session_id USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_booking_capacity
  BEFORE INSERT OR UPDATE OF status, session_id ON bookings
  FOR EACH ROW WHEN (NEW.status = 'confirmed')
  EXECUTE FUNCTION enforce_session_capacity();

-- 7. Invoices (immutable once issued; corrections via void + new invoice)
-- Gapless numbering per Indian financial year (April-March), e.g. EL/2026-27/000001
CREATE TABLE invoice_counters (
  financial_year text PRIMARY KEY,
  last_seq       integer NOT NULL DEFAULT 0
);

CREATE TABLE invoices (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  invoice_number  text NOT NULL UNIQUE,
  financial_year  text NOT NULL,
  seq             integer NOT NULL,
  booking_id      uuid NOT NULL REFERENCES bookings(id),
  customer_id     uuid NOT NULL REFERENCES customers(id),
  issue_date      date NOT NULL,
  subtotal_paise  integer NOT NULL CHECK (subtotal_paise >= 0),
  tax_total_paise integer NOT NULL DEFAULT 0 CHECK (tax_total_paise >= 0),
  total_paise     integer NOT NULL CHECK (total_paise >= 0),
  status          text NOT NULL DEFAULT 'issued' CHECK (status IN ('issued','void')),
  pdf_storage_key text,
  void_reason     text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (financial_year, seq),
  CHECK (total_paise = subtotal_paise + tax_total_paise)
);
-- one live invoice per booking (a voided one can be replaced)
CREATE UNIQUE INDEX uq_invoice_per_booking ON invoices (booking_id) WHERE status = 'issued';
CREATE INDEX idx_invoices_customer ON invoices (customer_id);

CREATE TABLE invoice_items (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  invoice_id       uuid NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
  description      text NOT NULL,
  quantity         integer NOT NULL DEFAULT 1 CHECK (quantity > 0),
  unit_price_paise integer NOT NULL CHECK (unit_price_paise >= 0),
  tax_rate_bps     integer NOT NULL DEFAULT 0 CHECK (tax_rate_bps BETWEEN 0 AND 10000),
  tax_amount_paise integer NOT NULL DEFAULT 0 CHECK (tax_amount_paise >= 0),
  line_total_paise integer NOT NULL CHECK (line_total_paise >= 0)
);
CREATE INDEX idx_items_invoice ON invoice_items (invoice_id);

-- 8. Payments (append-only ledger; independent of booking status)
CREATE TABLE payments (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  invoice_id          uuid NOT NULL REFERENCES invoices(id),
  kind                text NOT NULL DEFAULT 'payment' CHECK (kind IN ('payment','refund')),
  amount_paise        integer NOT NULL CHECK (amount_paise > 0),
  method              text NOT NULL CHECK (method IN ('cash','upi','card','bank_transfer','other')),
  reference           text,
  received_at         timestamptz NOT NULL DEFAULT now(),
  recorded_by_user_id uuid REFERENCES users(id),
  notes               text,
  created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_payments_invoice ON payments (invoice_id);

-- Payment status is DERIVED from the ledger, never typed in by hand,
-- so balances cannot drift out of sync.
CREATE VIEW invoice_balances AS
SELECT
  i.id AS invoice_id,
  i.invoice_number,
  i.total_paise,
  COALESCE(SUM(p.amount_paise) FILTER (WHERE p.kind = 'payment'), 0) AS paid_paise,
  COALESCE(SUM(p.amount_paise) FILTER (WHERE p.kind = 'refund'), 0)  AS refunded_paise,
  i.total_paise
    - (COALESCE(SUM(p.amount_paise) FILTER (WHERE p.kind = 'payment'), 0)
     - COALESCE(SUM(p.amount_paise) FILTER (WHERE p.kind = 'refund'), 0)) AS balance_paise,
  CASE
    WHEN i.status = 'void' THEN 'cancelled'
    WHEN COALESCE(SUM(p.amount_paise) FILTER (WHERE p.kind = 'payment'), 0) > 0
         AND COALESCE(SUM(p.amount_paise) FILTER (WHERE p.kind = 'payment'), 0)
           = COALESCE(SUM(p.amount_paise) FILTER (WHERE p.kind = 'refund'), 0) THEN 'refunded'
    WHEN COALESCE(SUM(p.amount_paise) FILTER (WHERE p.kind = 'payment'), 0)
       - COALESCE(SUM(p.amount_paise) FILTER (WHERE p.kind = 'refund'), 0) <= 0 THEN 'pending'
    WHEN COALESCE(SUM(p.amount_paise) FILTER (WHERE p.kind = 'payment'), 0)
       - COALESCE(SUM(p.amount_paise) FILTER (WHERE p.kind = 'refund'), 0) < i.total_paise THEN 'partially_paid'
    ELSE 'paid'
  END AS payment_status
FROM invoices i
LEFT JOIN payments p ON p.invoice_id = i.id
GROUP BY i.id;

-- 9. Notifications (confirmations, invoices, reminders) with honest status
CREATE TABLE notifications (
  id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id             uuid NOT NULL REFERENCES customers(id),
  booking_id              uuid REFERENCES bookings(id),
  invoice_id              uuid REFERENCES invoices(id),
  channel                 text NOT NULL DEFAULT 'whatsapp' CHECK (channel IN ('whatsapp','sms','email')),
  type                    text NOT NULL CHECK (type IN
                           ('booking_confirmation','invoice','reminder','cancellation','reschedule')),
  reminder_offset_minutes integer,
  provider                text NOT NULL CHECK (provider IN ('mock','whatsapp_cloud')),
  status                  text NOT NULL DEFAULT 'scheduled' CHECK (status IN
                           ('scheduled','queued','sent','delivered','read','failed','cancelled','skipped')),
  provider_message_id     text,
  scheduled_for           timestamptz NOT NULL,
  sent_at                 timestamptz,
  delivered_at            timestamptz,
  attempts                integer NOT NULL DEFAULT 0,
  next_retry_at           timestamptz,
  last_error              text,
  dedupe_key              text NOT NULL UNIQUE,
  created_at              timestamptz NOT NULL DEFAULT now(),
  updated_at              timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_notif_due ON notifications (scheduled_for) WHERE status IN ('scheduled','failed');
CREATE INDEX idx_notif_booking ON notifications (booking_id);

-- 10. Audit log (append-only)
CREATE TABLE audit_logs (
  id            bigserial PRIMARY KEY,
  actor_user_id uuid REFERENCES users(id),
  actor_type    text NOT NULL CHECK (actor_type IN ('user','customer','system')),
  action        text NOT NULL,
  entity_type   text NOT NULL,
  entity_id     text NOT NULL,
  before_data   jsonb,
  after_data    jsonb,
  ip_address    inet,
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_audit_entity ON audit_logs (entity_type, entity_id);
CREATE INDEX idx_audit_created ON audit_logs (created_at);

-- updated_at triggers
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['users','customers','trainers','services','sessions',
                           'bookings','invoices','notifications']
  LOOP
    EXECUTE format('CREATE TRIGGER trg_%1$s_updated BEFORE UPDATE ON %1$I
                    FOR EACH ROW EXECUTE FUNCTION set_updated_at()', t);
  END LOOP;
END $$;

-- NOT YET MODELLED: monthly memberships, credit notes for refunds,
-- recurring group-class schedules.

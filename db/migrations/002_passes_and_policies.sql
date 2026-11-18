-- Elevate Manager - Migration 002: passes, policies and database-level guards
-- Applies on top of 001_initial.sql.
--
-- Decisions from the owner that this migration encodes:
--   * Free cancellation / rescheduling up to 6 hours before the session
--   * Maximum 1 reschedule per booking
--   * Memberships are session-count passes (e.g. 10 sessions, 30 days)
--   * Strict trainer-to-service mapping (already in 001), group capacities
--
-- Principle: policy VALUES live in business_settings (changeable),
-- but the rules that must never be broken are enforced by the database.

-- 1. Helpers

-- Indian financial year (April-March): 2027-03-31 -> '2026-27', 2027-04-01 -> '2027-28'
CREATE OR REPLACE FUNCTION financial_year_of(d date) RETURNS text AS $$
  SELECT CASE WHEN EXTRACT(month FROM d) >= 4
              THEN EXTRACT(year FROM d)::int || '-' || right((EXTRACT(year FROM d)::int + 1)::text, 2)
              ELSE (EXTRACT(year FROM d)::int - 1) || '-' || right(EXTRACT(year FROM d)::int::text, 2)
         END;
$$ LANGUAGE sql IMMUTABLE;

-- Gapless invoice numbers. Call this INSIDE the same transaction that inserts the invoice:
-- if the transaction rolls back, the counter rolls back too, so no number is ever skipped.
CREATE OR REPLACE FUNCTION next_invoice_number(p_fy text, p_prefix text DEFAULT 'EL')
RETURNS TABLE (o_seq integer, o_number text) AS $$
BEGIN
  RETURN QUERY
  WITH bumped AS (
    INSERT INTO invoice_counters AS c (financial_year, last_seq) VALUES (p_fy, 1)
    ON CONFLICT (financial_year) DO UPDATE SET last_seq = c.last_seq + 1
    RETURNING c.last_seq
  )
  SELECT b.last_seq, p_prefix || '/' || p_fy || '/' || lpad(b.last_seq::text, 6, '0')
  FROM bumped b;
END $$ LANGUAGE plpgsql;

-- Single source of truth for "is it too late?" so the cancellation rule is not
-- re-implemented (and mis-implemented) in several places.
-- Exactly N hours before the start still counts as in time (free).
CREATE OR REPLACE FUNCTION is_too_late(p_starts_at timestamptz, p_at timestamptz, p_setting_key text)
RETURNS boolean AS $$
DECLARE
  hrs integer;
BEGIN
  SELECT (value #>> '{}')::integer INTO hrs FROM business_settings WHERE key = p_setting_key;
  IF hrs IS NULL THEN
    RAISE EXCEPTION 'business setting % is not configured', p_setting_key USING ERRCODE = '23514';
  END IF;
  RETURN (p_starts_at - p_at) < make_interval(hours => hrs);
END $$ LANGUAGE plpgsql STABLE;

CREATE OR REPLACE FUNCTION forbid_modification() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION '% rows cannot be % (append-only)', TG_TABLE_NAME, lower(TG_OP)
    USING ERRCODE = 'integrity_constraint_violation';
END $$ LANGUAGE plpgsql;

-- 2. Sessions must respect operating hours, holidays, trainer hours,
--    trainer time off, active flags and the service duration
CREATE OR REPLACE FUNCTION validate_session_slot() RETURNS trigger AS $$
DECLARE
  tz          text;
  l_start     timestamp;
  l_end       timestamp;
  dow         integer;
  bh          business_hours%ROWTYPE;
  dur         integer;
  svc_active  boolean;
  trn_active  boolean;
BEGIN
  SELECT value #>> '{}' INTO tz FROM business_settings WHERE key = 'timezone';
  tz := COALESCE(tz, 'Asia/Kolkata');
  l_start := NEW.starts_at AT TIME ZONE tz;
  l_end   := NEW.ends_at   AT TIME ZONE tz;
  dow     := EXTRACT(dow FROM l_start)::integer;

  SELECT duration_minutes, is_active INTO dur, svc_active FROM services WHERE id = NEW.service_id;
  SELECT is_active INTO trn_active FROM trainers WHERE id = NEW.trainer_id;
  IF NOT COALESCE(svc_active, false) THEN
    RAISE EXCEPTION 'service is not active' USING ERRCODE = '23514';
  END IF;
  IF NOT COALESCE(trn_active, false) THEN
    RAISE EXCEPTION 'trainer is not active' USING ERRCODE = '23514';
  END IF;
  IF NEW.ends_at - NEW.starts_at <> make_interval(mins => dur) THEN
    RAISE EXCEPTION 'session length must equal the service duration (% minutes)', dur USING ERRCODE = '23514';
  END IF;

  IF l_start::date <> l_end::date THEN
    RAISE EXCEPTION 'session must not span midnight' USING ERRCODE = '23514';
  END IF;

  SELECT * INTO bh FROM business_hours WHERE weekday = dow;
  IF NOT FOUND OR bh.is_closed OR l_start::time < bh.open_time OR l_end::time > bh.close_time THEN
    RAISE EXCEPTION 'session is outside operating hours' USING ERRCODE = '23514';
  END IF;

  IF EXISTS (SELECT 1 FROM business_holidays WHERE holiday_date = l_start::date) THEN
    RAISE EXCEPTION 'studio is closed on this date (holiday)' USING ERRCODE = '23514';
  END IF;

  IF NOT EXISTS (SELECT 1 FROM trainer_working_hours w
                  WHERE w.trainer_id = NEW.trainer_id AND w.weekday = dow
                    AND w.start_time <= l_start::time AND w.end_time >= l_end::time) THEN
    RAISE EXCEPTION 'trainer does not work at this time' USING ERRCODE = '23514';
  END IF;

  IF EXISTS (SELECT 1 FROM trainer_time_off o
              WHERE o.trainer_id = NEW.trainer_id
                AND tstzrange(o.starts_at, o.ends_at, '[)') && tstzrange(NEW.starts_at, NEW.ends_at, '[)')) THEN
    RAISE EXCEPTION 'trainer is on leave at this time' USING ERRCODE = '23514';
  END IF;

  RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_session_validate_slot
  BEFORE INSERT OR UPDATE OF starts_at, ends_at, trainer_id, service_id ON sessions
  FOR EACH ROW WHEN (NEW.status = 'scheduled')
  EXECUTE FUNCTION validate_session_slot();

-- 3. Session-count passes
CREATE TABLE pass_plans (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name          text NOT NULL UNIQUE,
  session_count integer NOT NULL CHECK (session_count > 0),
  validity_days integer NOT NULL CHECK (validity_days > 0),
  price_paise   integer NOT NULL CHECK (price_paise >= 0),
  tax_rate_bps  integer NOT NULL DEFAULT 0 CHECK (tax_rate_bps BETWEEN 0 AND 10000),
  is_active     boolean NOT NULL DEFAULT true,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);

-- Which services a plan can be spent on
CREATE TABLE pass_plan_services (
  pass_plan_id uuid NOT NULL REFERENCES pass_plans(id) ON DELETE CASCADE,
  service_id   uuid NOT NULL REFERENCES services(id)   ON DELETE CASCADE,
  PRIMARY KEY (pass_plan_id, service_id)
);

-- A pass a customer has bought (plan terms are copied so later plan edits don't change it)
CREATE TABLE member_passes (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id    uuid NOT NULL REFERENCES customers(id),
  pass_plan_id   uuid NOT NULL REFERENCES pass_plans(id),
  sessions_total integer NOT NULL CHECK (sessions_total > 0),
  purchased_at   timestamptz NOT NULL DEFAULT now(),
  expires_at     timestamptz NOT NULL,
  status         text NOT NULL DEFAULT 'active' CHECK (status IN ('active','expired','cancelled')),
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now(),
  CHECK (expires_at > purchased_at),
  UNIQUE (id, customer_id)
);
CREATE INDEX idx_member_passes_customer ON member_passes (customer_id);

-- Credits are an append-only ledger. Balance = SUM(delta); it can never go below zero.
-- Late cancel / no-show = no refund row is written, so the credit stays forfeited.
CREATE TABLE pass_credit_ledger (
  id                  bigserial PRIMARY KEY,
  member_pass_id      uuid NOT NULL REFERENCES member_passes(id),
  booking_id          uuid REFERENCES bookings(id),
  delta               integer NOT NULL CHECK (delta <> 0),
  reason              text NOT NULL CHECK (reason IN
                       ('purchase','booking_use','cancel_refund','expiry','manual_adjustment')),
  note                text,
  created_by_user_id  uuid REFERENCES users(id),
  created_at          timestamptz NOT NULL DEFAULT now(),
  CHECK (reason NOT IN ('booking_use','cancel_refund') OR booking_id IS NOT NULL),
  CHECK (reason <> 'booking_use'   OR delta < 0),
  CHECK (reason <> 'cancel_refund' OR delta > 0)
);
CREATE INDEX idx_pass_ledger_pass ON pass_credit_ledger (member_pass_id);
-- one use and at most one refund per booking
CREATE UNIQUE INDEX uq_pass_use_per_booking    ON pass_credit_ledger (booking_id) WHERE reason = 'booking_use';
CREATE UNIQUE INDEX uq_pass_refund_per_booking ON pass_credit_ledger (booking_id) WHERE reason = 'cancel_refund';

CREATE OR REPLACE FUNCTION enforce_pass_ledger() RETURNS trigger AS $$
DECLARE
  bal      integer;
  p_status text;
  p_expiry timestamptz;
  s_start  timestamptz;
BEGIN
  -- serialise all credit movements for this pass
  SELECT status, expires_at INTO p_status, p_expiry
    FROM member_passes WHERE id = NEW.member_pass_id FOR UPDATE;

  SELECT COALESCE(SUM(delta), 0) INTO bal FROM pass_credit_ledger WHERE member_pass_id = NEW.member_pass_id;
  IF bal + NEW.delta < 0 THEN
    RAISE EXCEPTION 'insufficient pass credits (balance %, change %)', bal, NEW.delta USING ERRCODE = '23514';
  END IF;

  IF NEW.reason = 'booking_use' THEN
    IF p_status <> 'active' THEN
      RAISE EXCEPTION 'pass is not active' USING ERRCODE = '23514';
    END IF;
    SELECT s.starts_at INTO s_start
      FROM bookings b JOIN sessions s ON s.id = b.session_id WHERE b.id = NEW.booking_id;
    IF s_start > p_expiry THEN
      RAISE EXCEPTION 'pass expires before this session' USING ERRCODE = '23514';
    END IF;
    IF NOT EXISTS (SELECT 1
                     FROM member_passes mp
                     JOIN pass_plan_services pps ON pps.pass_plan_id = mp.pass_plan_id
                     JOIN bookings b ON b.id = NEW.booking_id
                     JOIN sessions s ON s.id = b.session_id AND s.service_id = pps.service_id
                    WHERE mp.id = NEW.member_pass_id) THEN
      RAISE EXCEPTION 'pass does not cover this service' USING ERRCODE = '23514';
    END IF;
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_pass_ledger_guard BEFORE INSERT ON pass_credit_ledger
  FOR EACH ROW EXECUTE FUNCTION enforce_pass_ledger();
CREATE TRIGGER trg_pass_ledger_append_only BEFORE UPDATE OR DELETE ON pass_credit_ledger
  FOR EACH ROW EXECUTE FUNCTION forbid_modification();

CREATE VIEW pass_balances AS
SELECT mp.id AS member_pass_id, mp.customer_id, mp.status, mp.expires_at,
       COALESCE(SUM(l.delta), 0) AS credits_remaining
FROM member_passes mp
LEFT JOIN pass_credit_ledger l ON l.member_pass_id = mp.id
GROUP BY mp.id;

-- 4. Bookings: pass link + reschedule limit
ALTER TABLE bookings ADD COLUMN member_pass_id uuid;
ALTER TABLE bookings ADD COLUMN reschedule_count integer NOT NULL DEFAULT 0 CHECK (reschedule_count >= 0);
-- composite FK: the pass must belong to the same customer as the booking
ALTER TABLE bookings ADD CONSTRAINT fk_booking_pass_owner
  FOREIGN KEY (member_pass_id, customer_id) REFERENCES member_passes (id, customer_id);

-- reschedule_count is set by the database (previous + 1) so the app cannot bypass the limit.
-- The limit itself comes from business_settings.max_reschedules_per_booking.
CREATE OR REPLACE FUNCTION enforce_reschedule_limit() RETURNS trigger AS $$
DECLARE
  prev_count  integer;
  prev_cust   uuid;
  max_allowed integer;
BEGIN
  IF NEW.rescheduled_from_booking_id IS NOT NULL THEN
    SELECT reschedule_count, customer_id INTO prev_count, prev_cust
      FROM bookings WHERE id = NEW.rescheduled_from_booking_id;
    IF prev_count IS NULL THEN
      RAISE EXCEPTION 'original booking not found' USING ERRCODE = '23503';
    END IF;
    IF prev_cust <> NEW.customer_id THEN
      RAISE EXCEPTION 'a booking can only be rescheduled by the same customer' USING ERRCODE = '23514';
    END IF;
    NEW.reschedule_count := prev_count + 1;
    SELECT (value #>> '{}')::integer INTO max_allowed
      FROM business_settings WHERE key = 'max_reschedules_per_booking';
    IF max_allowed IS NULL THEN
      RAISE EXCEPTION 'business setting max_reschedules_per_booking is not configured' USING ERRCODE = '23514';
    END IF;
    IF NEW.reschedule_count > max_allowed THEN
      RAISE EXCEPTION 'reschedule limit reached (max % per booking)', max_allowed USING ERRCODE = '23514';
    END IF;
  ELSE
    NEW.reschedule_count := 0;
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_booking_reschedule_limit BEFORE INSERT ON bookings
  FOR EACH ROW EXECUTE FUNCTION enforce_reschedule_limit();

-- 5. Invoices can be for a booking OR a pass purchase; issued invoices are immutable
ALTER TABLE invoices ALTER COLUMN booking_id DROP NOT NULL;
ALTER TABLE invoices ADD COLUMN member_pass_id uuid REFERENCES member_passes(id);
ALTER TABLE invoices ADD CONSTRAINT invoice_has_one_subject
  CHECK ((booking_id IS NOT NULL)::int + (member_pass_id IS NOT NULL)::int = 1);
CREATE UNIQUE INDEX uq_invoice_per_pass ON invoices (member_pass_id)
  WHERE status = 'issued' AND member_pass_id IS NOT NULL;

CREATE OR REPLACE FUNCTION guard_invoice_update() RETURNS trigger AS $$
BEGIN
  IF OLD.status = 'void' THEN
    RAISE EXCEPTION 'a void invoice cannot be changed' USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  IF NEW.invoice_number  IS DISTINCT FROM OLD.invoice_number
  OR NEW.financial_year  IS DISTINCT FROM OLD.financial_year
  OR NEW.seq             IS DISTINCT FROM OLD.seq
  OR NEW.booking_id      IS DISTINCT FROM OLD.booking_id
  OR NEW.member_pass_id  IS DISTINCT FROM OLD.member_pass_id
  OR NEW.customer_id     IS DISTINCT FROM OLD.customer_id
  OR NEW.issue_date      IS DISTINCT FROM OLD.issue_date
  OR NEW.subtotal_paise  IS DISTINCT FROM OLD.subtotal_paise
  OR NEW.tax_total_paise IS DISTINCT FROM OLD.tax_total_paise
  OR NEW.total_paise     IS DISTINCT FROM OLD.total_paise THEN
    RAISE EXCEPTION 'an issued invoice cannot be edited; void it and issue a new one'
      USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  IF NEW.status = 'void' AND NEW.void_reason IS NULL THEN
    RAISE EXCEPTION 'a void reason is required' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_invoice_guard_update BEFORE UPDATE ON invoices
  FOR EACH ROW EXECUTE FUNCTION guard_invoice_update();
CREATE TRIGGER trg_invoice_no_delete BEFORE DELETE ON invoices
  FOR EACH ROW EXECUTE FUNCTION forbid_modification();
CREATE TRIGGER trg_invoice_items_append_only BEFORE UPDATE OR DELETE ON invoice_items
  FOR EACH ROW EXECUTE FUNCTION forbid_modification();

-- 6. Payments: append-only, no overpayment, refunds cannot exceed payments
CREATE OR REPLACE FUNCTION enforce_payment_rules() RETURNS trigger AS $$
DECLARE
  inv_total integer;
  inv_status text;
  paid     integer;
  refunded integer;
BEGIN
  SELECT total_paise, status INTO inv_total, inv_status FROM invoices WHERE id = NEW.invoice_id FOR UPDATE;
  IF inv_status <> 'issued' THEN
    RAISE EXCEPTION 'payments cannot be recorded against a void invoice' USING ERRCODE = '23514';
  END IF;
  SELECT COALESCE(SUM(amount_paise) FILTER (WHERE kind = 'payment'), 0),
         COALESCE(SUM(amount_paise) FILTER (WHERE kind = 'refund'), 0)
    INTO paid, refunded FROM payments WHERE invoice_id = NEW.invoice_id;

  IF NEW.kind = 'payment' AND (paid - refunded + NEW.amount_paise) > inv_total THEN
    RAISE EXCEPTION 'payment would exceed the invoice total' USING ERRCODE = '23514';
  END IF;
  IF NEW.kind = 'refund' AND (refunded + NEW.amount_paise) > paid THEN
    RAISE EXCEPTION 'refund would exceed the amount paid' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_payment_rules BEFORE INSERT ON payments
  FOR EACH ROW EXECUTE FUNCTION enforce_payment_rules();
CREATE TRIGGER trg_payment_append_only BEFORE UPDATE OR DELETE ON payments
  FOR EACH ROW EXECUTE FUNCTION forbid_modification();

-- 7. Audit log is append-only
CREATE TRIGGER trg_audit_append_only BEFORE UPDATE OR DELETE ON audit_logs
  FOR EACH ROW EXECUTE FUNCTION forbid_modification();

-- updated_at triggers for the new tables
CREATE TRIGGER trg_pass_plans_updated    BEFORE UPDATE ON pass_plans    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_member_passes_updated BEFORE UPDATE ON member_passes FOR EACH ROW EXECUTE FUNCTION set_updated_at();

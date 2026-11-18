-- Elevate Manager - Migration 006: application authentication and local payment intents

ALTER TABLE customers ADD COLUMN password_hash text;
CREATE UNIQUE INDEX uq_customer_email ON customers (email) WHERE email IS NOT NULL;

CREATE TABLE payment_intents (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id     uuid NOT NULL REFERENCES customers(id),
  pass_plan_id    uuid NOT NULL REFERENCES pass_plans(id),
  idempotency_key text NOT NULL UNIQUE,
  amount_paise    integer NOT NULL CHECK (amount_paise >= 0),
  provider        text NOT NULL CHECK (provider IN ('local_test', 'gateway_pending')),
  status          text NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','succeeded','failed')),
  failure_reason  text,
  member_pass_id  uuid UNIQUE REFERENCES member_passes(id),
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_payment_intents_customer ON payment_intents (customer_id);

CREATE TRIGGER trg_payment_intents_updated
  BEFORE UPDATE ON payment_intents
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE attendance_records (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  booking_id  uuid NOT NULL UNIQUE REFERENCES bookings(id),
  status      text NOT NULL CHECK (status IN ('present','absent','excused')),
  marked_by   uuid REFERENCES users(id),
  marked_at   timestamptz NOT NULL DEFAULT now(),
  notes       text
);

-- =====================================================================
-- Elevate Manager - Migration 003: payment holds, waitlist, quiet hours
-- Applies on top of 001 and 002.
--
-- Owner decisions encoded here:
--   * Instant booking + pay online first: an unpaid online booking is a
--     'pending_payment' hold that is released after 15 minutes.
--   * Pay-at-studio bookings are created directly as 'confirmed' (no hold).
--   * Waitlist: when a seat frees up, the first person waiting gets a
--     30-minute offer to claim it (the seat is reserved for them).
--   * Reminders are never scheduled between 21:00 and 06:00. A reminder that
--     would land in that window is sent at 20:00 the evening before instead.
-- Policy VALUES live in business_settings (payment_hold_minutes,
-- waitlist_offer_minutes, quiet_hours_*). The rules below are enforced by the DB.
-- =====================================================================

-- ---------------------------------------------------------------------
-- 1. Waitlist table (created first: capacity logic below refers to it)
-- ---------------------------------------------------------------------
CREATE TABLE waitlist_entries (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id       uuid NOT NULL REFERENCES sessions(id),
  customer_id      uuid NOT NULL REFERENCES customers(id),
  status           text NOT NULL DEFAULT 'waiting'
                   CHECK (status IN ('waiting','offered','claimed','expired','withdrawn')),
  offered_at       timestamptz,
  offer_expires_at timestamptz,
  booking_id       uuid REFERENCES bookings(id),       -- set when the offer is claimed
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  CHECK (status <> 'offered' OR offer_expires_at IS NOT NULL)
);
-- a customer can be waiting (or holding an offer) for a session only once
CREATE UNIQUE INDEX uq_waitlist_active ON waitlist_entries (session_id, customer_id)
  WHERE status IN ('waiting','offered');
CREATE INDEX idx_waitlist_queue ON waitlist_entries (session_id, created_at) WHERE status = 'waiting';
CREATE TRIGGER trg_waitlist_updated BEFORE UPDATE ON waitlist_entries
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------------
-- 2. Booking statuses: pending_payment (held) and expired
-- ---------------------------------------------------------------------
ALTER TABLE bookings DROP CONSTRAINT bookings_status_check;
ALTER TABLE bookings ADD CONSTRAINT bookings_status_check
  CHECK (status IN ('pending_payment','confirmed','cancelled','expired','completed','no_show'));

ALTER TABLE bookings ADD COLUMN hold_expires_at timestamptz;
-- exactly the held bookings carry a hold deadline
ALTER TABLE bookings ADD CONSTRAINT booking_hold_consistency
  CHECK ((status = 'pending_payment') = (hold_expires_at IS NOT NULL));

-- a held seat still belongs to the customer: no duplicates for the same session
DROP INDEX uq_booking_customer_session;
CREATE UNIQUE INDEX uq_booking_customer_session ON bookings (session_id, customer_id)
  WHERE status IN ('confirmed','pending_payment');

-- Capacity now counts confirmed + held seats + seats reserved for waitlist offers
-- (an offer reserves a seat for that one customer, so nobody else can grab it).
CREATE OR REPLACE FUNCTION enforce_session_capacity() RETURNS trigger AS $$
DECLARE
  cap    integer;
  sstat  text;
  taken  integer;
  offers integer;
BEGIN
  SELECT capacity, status INTO cap, sstat FROM sessions WHERE id = NEW.session_id FOR UPDATE;
  IF sstat <> 'scheduled' THEN
    RAISE EXCEPTION 'session % is not open for booking', NEW.session_id USING ERRCODE = '23514';
  END IF;
  SELECT count(*) INTO taken FROM bookings
   WHERE session_id = NEW.session_id AND status IN ('confirmed','pending_payment') AND id <> NEW.id;
  SELECT count(*) INTO offers FROM waitlist_entries
   WHERE session_id = NEW.session_id AND status = 'offered' AND offer_expires_at > now()
     AND customer_id <> NEW.customer_id;
  IF taken + offers >= cap THEN
    RAISE EXCEPTION 'session % is full', NEW.session_id USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER trg_booking_capacity ON bookings;
CREATE TRIGGER trg_booking_capacity
  BEFORE INSERT OR UPDATE OF status, session_id ON bookings
  FOR EACH ROW WHEN (NEW.status IN ('confirmed','pending_payment'))
  EXECUTE FUNCTION enforce_session_capacity();

-- When a customer books a session they were waiting for, close their waitlist entry.
CREATE OR REPLACE FUNCTION close_waitlist_entry_on_booking() RETURNS trigger AS $$
BEGIN
  UPDATE waitlist_entries
     SET status = CASE WHEN status = 'offered' AND offer_expires_at > now() THEN 'claimed' ELSE 'withdrawn' END,
         booking_id = NEW.id
   WHERE session_id = NEW.session_id AND customer_id = NEW.customer_id
     AND status IN ('waiting','offered');
  RETURN NULL;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_booking_closes_waitlist
  AFTER INSERT ON bookings
  FOR EACH ROW WHEN (NEW.status IN ('confirmed','pending_payment'))
  EXECUTE FUNCTION close_waitlist_entry_on_booking();

-- ---------------------------------------------------------------------
-- 3. Releasing unpaid holds
--    Run every minute by the background scheduler (Phase 6).
-- ---------------------------------------------------------------------
-- A full payment turns a held booking into a confirmed one.
CREATE OR REPLACE FUNCTION confirm_booking_on_full_payment() RETURNS trigger AS $$
BEGIN
  UPDATE bookings b
     SET status = 'confirmed', hold_expires_at = NULL
    FROM invoices i
   WHERE i.id = NEW.invoice_id AND b.id = i.booking_id AND b.status = 'pending_payment'
     AND (SELECT COALESCE(SUM(CASE kind WHEN 'payment' THEN amount_paise ELSE -amount_paise END), 0)
            FROM payments WHERE invoice_id = i.id) >= i.total_paise;
  RETURN NULL;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_payment_confirms_booking
  AFTER INSERT ON payments
  FOR EACH ROW WHEN (NEW.kind = 'payment')
  EXECUTE FUNCTION confirm_booking_on_full_payment();

-- Releases held seats whose deadline has passed and voids their unpaid invoice.
-- If some money WAS received (partial payment), the invoice is left alone and the
-- booking shows up in paid_but_inactive_bookings for a human to refund or resolve.
-- Returns the number of released bookings. The caller should then call
-- offer_next_waitlist_seat() for the affected sessions (expire_unpaid_holds does it for you).
CREATE OR REPLACE FUNCTION expire_unpaid_holds(p_now timestamptz DEFAULT now()) RETURNS integer AS $$
DECLARE
  sessions_freed uuid[];
  n   integer;
  sid uuid;
BEGIN
  WITH expired AS (
    UPDATE bookings
       SET status = 'expired', hold_expires_at = NULL, cancellation_reason = 'payment hold expired'
     WHERE status = 'pending_payment' AND hold_expires_at <= p_now
    RETURNING id, session_id
  ), voided AS (
    UPDATE invoices i
       SET status = 'void', void_reason = 'payment hold expired'
     WHERE i.booking_id IN (SELECT id FROM expired) AND i.status = 'issued'
       AND NOT EXISTS (SELECT 1 FROM payments p WHERE p.invoice_id = i.id)
    RETURNING i.id
  )
  SELECT array_agg(session_id), count(*) INTO sessions_freed, n FROM expired;

  IF sessions_freed IS NOT NULL THEN
    FOREACH sid IN ARRAY sessions_freed LOOP
      PERFORM offer_next_waitlist_seat(sid, p_now);
    END LOOP;
  END IF;
  RETURN n;
END $$ LANGUAGE plpgsql;

-- Bookings that are expired/cancelled but still hold customer money: needs a human.
CREATE VIEW paid_but_inactive_bookings AS
SELECT b.id AS booking_id, b.booking_ref, b.status AS booking_status, i.id AS invoice_id, i.invoice_number,
       COALESCE(SUM(CASE p.kind WHEN 'payment' THEN p.amount_paise ELSE -p.amount_paise END), 0) AS net_paid_paise
FROM bookings b
JOIN invoices i ON i.booking_id = b.id AND i.status = 'issued'
JOIN payments p ON p.invoice_id = i.id
WHERE b.status IN ('expired','cancelled')
GROUP BY b.id, i.id
HAVING COALESCE(SUM(CASE p.kind WHEN 'payment' THEN p.amount_paise ELSE -p.amount_paise END), 0) > 0;

-- ---------------------------------------------------------------------
-- 4. Waitlist logic
-- ---------------------------------------------------------------------
-- Joining is only allowed for a full session the customer has no seat in.
CREATE OR REPLACE FUNCTION guard_waitlist_join() RETURNS trigger AS $$
DECLARE
  cap    integer;
  sstat  text;
  taken  integer;
  offers integer;
BEGIN
  SELECT capacity, status INTO cap, sstat FROM sessions WHERE id = NEW.session_id FOR UPDATE;
  IF sstat <> 'scheduled' THEN
    RAISE EXCEPTION 'cannot join the waitlist of a cancelled session' USING ERRCODE = '23514';
  END IF;
  IF EXISTS (SELECT 1 FROM bookings WHERE session_id = NEW.session_id AND customer_id = NEW.customer_id
                AND status IN ('confirmed','pending_payment')) THEN
    RAISE EXCEPTION 'customer already has a seat in this session' USING ERRCODE = '23514';
  END IF;
  SELECT count(*) INTO taken FROM bookings
   WHERE session_id = NEW.session_id AND status IN ('confirmed','pending_payment');
  SELECT count(*) INTO offers FROM waitlist_entries
   WHERE session_id = NEW.session_id AND status = 'offered' AND offer_expires_at > now();
  IF taken + offers < cap THEN
    RAISE EXCEPTION 'session still has free seats; book directly instead of joining the waitlist'
      USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_waitlist_join_guard BEFORE INSERT ON waitlist_entries
  FOR EACH ROW EXECUTE FUNCTION guard_waitlist_join();

-- If a seat is free and someone is waiting, give the first person in line an offer.
-- The offer lasts waitlist_offer_minutes (never beyond the session start) and reserves
-- the seat for them. Returns the offered entry id, or NULL if nothing was offered.
-- Call this after every cancellation / hold expiry / offer expiry.
CREATE OR REPLACE FUNCTION offer_next_waitlist_seat(p_session_id uuid, p_now timestamptz DEFAULT now())
RETURNS uuid AS $$
DECLARE
  cap     integer;
  sstat   text;
  s_start timestamptz;
  taken   integer;
  offers  integer;
  win     integer;
  eid     uuid;
BEGIN
  SELECT capacity, status, starts_at INTO cap, sstat, s_start
    FROM sessions WHERE id = p_session_id FOR UPDATE;
  IF sstat IS DISTINCT FROM 'scheduled' OR s_start <= p_now THEN
    RETURN NULL;
  END IF;
  SELECT count(*) INTO taken FROM bookings
   WHERE session_id = p_session_id AND status IN ('confirmed','pending_payment');
  SELECT count(*) INTO offers FROM waitlist_entries
   WHERE session_id = p_session_id AND status = 'offered' AND offer_expires_at > p_now;
  IF taken + offers >= cap THEN
    RETURN NULL;
  END IF;

  SELECT (value #>> '{}')::integer INTO win FROM business_settings WHERE key = 'waitlist_offer_minutes';
  IF win IS NULL THEN
    RAISE EXCEPTION 'business setting waitlist_offer_minutes is not configured' USING ERRCODE = '23514';
  END IF;

  SELECT id INTO eid FROM waitlist_entries
   WHERE session_id = p_session_id AND status = 'waiting'
   ORDER BY created_at, id LIMIT 1 FOR UPDATE SKIP LOCKED;
  IF eid IS NULL THEN
    RETURN NULL;
  END IF;

  UPDATE waitlist_entries
     SET status = 'offered', offered_at = p_now,
         offer_expires_at = LEAST(p_now + make_interval(mins => win), s_start)
   WHERE id = eid;
  RETURN eid;
END $$ LANGUAGE plpgsql;

-- Marks unclaimed offers as expired and passes each freed seat to the next person.
-- Returns the number of expired offers. Run every minute by the scheduler.
CREATE OR REPLACE FUNCTION expire_waitlist_offers(p_now timestamptz DEFAULT now()) RETURNS integer AS $$
DECLARE
  sessions_freed uuid[];
  n   integer;
  sid uuid;
BEGIN
  WITH u AS (
    UPDATE waitlist_entries SET status = 'expired'
     WHERE status = 'offered' AND offer_expires_at <= p_now
    RETURNING session_id
  )
  SELECT array_agg(session_id), count(*) INTO sessions_freed, n FROM u;

  IF sessions_freed IS NOT NULL THEN
    FOREACH sid IN ARRAY sessions_freed LOOP
      PERFORM offer_next_waitlist_seat(sid, p_now);
    END LOOP;
  END IF;
  RETURN n;
END $$ LANGUAGE plpgsql;

-- ---------------------------------------------------------------------
-- 5. Reminder quiet hours
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION in_quiet_hours(p_ts timestamptz) RETURNS boolean AS $$
DECLARE
  tz text; qs time; qe time; lt time;
BEGIN
  SELECT value #>> '{}' INTO tz FROM business_settings WHERE key = 'timezone';
  SELECT (value #>> '{}')::time INTO qs FROM business_settings WHERE key = 'quiet_hours_start';
  SELECT (value #>> '{}')::time INTO qe FROM business_settings WHERE key = 'quiet_hours_end';
  IF qs IS NULL OR qe IS NULL THEN
    RAISE EXCEPTION 'quiet_hours_start / quiet_hours_end are not configured' USING ERRCODE = '23514';
  END IF;
  lt := (p_ts AT TIME ZONE COALESCE(tz, 'Asia/Kolkata'))::time;
  -- the quiet window runs overnight (e.g. 21:00 -> 06:00)
  RETURN lt >= qs OR lt < qe;
END $$ LANGUAGE plpgsql STABLE;

-- When should a reminder go out? Normally starts_at minus the offset. If that moment
-- falls in quiet hours it moves to quiet_hours_send_time (20:00) on the evening before.
-- The caller must skip the reminder if the returned time is already in the past.
CREATE OR REPLACE FUNCTION reminder_send_time(p_starts_at timestamptz, p_offset_minutes integer)
RETURNS timestamptz AS $$
DECLARE
  tz     text;
  raw    timestamptz := p_starts_at - make_interval(mins => p_offset_minutes);
  l_raw  timestamp;
  qs     time;
  sendt  time;
  d      date;
BEGIN
  IF NOT in_quiet_hours(raw) THEN
    RETURN raw;
  END IF;
  SELECT value #>> '{}' INTO tz FROM business_settings WHERE key = 'timezone';
  SELECT (value #>> '{}')::time INTO qs    FROM business_settings WHERE key = 'quiet_hours_start';
  SELECT (value #>> '{}')::time INTO sendt FROM business_settings WHERE key = 'quiet_hours_send_time';
  IF sendt IS NULL OR sendt >= qs THEN
    RAISE EXCEPTION 'quiet_hours_send_time must be configured and earlier than quiet_hours_start'
      USING ERRCODE = '23514';
  END IF;
  tz := COALESCE(tz, 'Asia/Kolkata');
  l_raw := raw AT TIME ZONE tz;
  -- late evening part of the window -> same evening; early-morning part -> the evening before
  d := CASE WHEN l_raw::time >= qs THEN l_raw::date ELSE l_raw::date - 1 END;
  RETURN (d + sendt) AT TIME ZONE tz;
END $$ LANGUAGE plpgsql STABLE;

-- Reminders can never be scheduled inside quiet hours.
CREATE OR REPLACE FUNCTION guard_reminder_quiet_hours() RETURNS trigger AS $$
BEGIN
  IF in_quiet_hours(NEW.scheduled_for) THEN
    RAISE EXCEPTION 'reminders cannot be scheduled during quiet hours' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_reminder_quiet_hours
  BEFORE INSERT OR UPDATE OF scheduled_for ON notifications
  FOR EACH ROW WHEN (NEW.type = 'reminder')
  EXECUTE FUNCTION guard_reminder_quiet_hours();

ALTER TABLE notifications DROP CONSTRAINT notifications_type_check;
ALTER TABLE notifications ADD CONSTRAINT notifications_type_check
  CHECK (type IN ('booking_confirmation','invoice','reminder','cancellation','reschedule','waitlist_offer'));

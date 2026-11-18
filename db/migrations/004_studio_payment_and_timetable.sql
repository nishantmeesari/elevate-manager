-- Elevate Manager - Migration 004: pay-at-studio rule and class timetable
-- Applies on top of 001-003.
--
-- Owner decisions encoded here:
--   * "Pay at Studio" bookings must be paid at the front desk at least 2 hours
--     before the class. If unpaid by then they are released (and the freed seat
--     goes to the waitlist). This reuses the payment-hold mechanism from 003:
--     a studio booking is a 'pending_payment' booking whose deadline is
--     (class start - 2 hours) instead of (now + 15 minutes).
--   * Weekly class timetable, from which class sessions are generated.
--
-- The database computes the payment deadline itself, so the application cannot
-- accidentally give a booking a longer hold than the policy allows.

-- 1. How a booking is being paid
ALTER TABLE bookings ADD COLUMN payment_mode text NOT NULL DEFAULT 'online'
  CHECK (payment_mode IN ('online','studio','pass'));
-- a pass booking has a pass, and only a pass booking has one
ALTER TABLE bookings ADD CONSTRAINT booking_pass_mode_consistency
  CHECK ((payment_mode = 'pass') = (member_pass_id IS NOT NULL));

-- 2. Pay-at-studio deadline
--    Normally class start minus studio_payment_deadline_hours (2).
--    If the front desk is closed at that moment (for example the deadline for a
--    7:00 AM class would be 5:00 AM, before the studio opens), the deadline moves
--    to the closing time of the last open day before it.
CREATE OR REPLACE FUNCTION studio_payment_deadline(p_starts_at timestamptz) RETURNS timestamptz AS $$
DECLARE
  tz    text;
  hrs   integer;
  raw   timestamptz;
  l_raw timestamp;
  ot    time;
  ct    time;
  d     date;
  i     integer;
BEGIN
  SELECT value #>> '{}' INTO tz FROM business_settings WHERE key = 'timezone';
  tz := COALESCE(tz, 'Asia/Kolkata');
  SELECT (value #>> '{}')::integer INTO hrs FROM business_settings WHERE key = 'studio_payment_deadline_hours';
  IF hrs IS NULL THEN
    RAISE EXCEPTION 'business setting studio_payment_deadline_hours is not configured' USING ERRCODE = '23514';
  END IF;

  raw   := p_starts_at - make_interval(hours => hrs);
  l_raw := raw AT TIME ZONE tz;

  SELECT open_time INTO ot FROM business_hours
   WHERE weekday = EXTRACT(dow FROM l_raw)::integer AND NOT is_closed;
  IF ot IS NOT NULL AND l_raw::time >= ot
     AND NOT EXISTS (SELECT 1 FROM business_holidays WHERE holiday_date = l_raw::date) THEN
    RETURN raw;
  END IF;

  d := l_raw::date;
  FOR i IN 1..14 LOOP
    d := d - 1;
    SELECT close_time INTO ct FROM business_hours
     WHERE weekday = EXTRACT(dow FROM d)::integer AND NOT is_closed;
    IF ct IS NOT NULL AND NOT EXISTS (SELECT 1 FROM business_holidays WHERE holiday_date = d) THEN
      RETURN (d + ct) AT TIME ZONE tz;
    END IF;
  END LOOP;
  RAISE EXCEPTION 'no open day found before the session' USING ERRCODE = '23514';
END $$ LANGUAGE plpgsql STABLE;

-- 3. The database sets the payment deadline and refuses unpaid shortcuts
CREATE OR REPLACE FUNCTION set_booking_payment_rules() RETURNS trigger AS $$
DECLARE
  s_start  timestamptz;
  hold_min integer;
  dl       timestamptz;
BEGIN
  -- Customers booking through the website must either pay (pass) or be held pending payment.
  -- They can never create an already-confirmed unpaid booking. (Staff can: a walk-in paying at the desk.)
  IF NEW.source = 'website' AND NEW.payment_mode IN ('online','studio') AND NEW.status <> 'pending_payment' THEN
    RAISE EXCEPTION 'website bookings must start as pending_payment' USING ERRCODE = '23514';
  END IF;

  IF NEW.status <> 'pending_payment' THEN
    RETURN NEW;
  END IF;

  SELECT starts_at INTO s_start FROM sessions WHERE id = NEW.session_id;

  IF NEW.payment_mode = 'online' THEN
    SELECT (value #>> '{}')::integer INTO hold_min FROM business_settings WHERE key = 'payment_hold_minutes';
    IF hold_min IS NULL THEN
      RAISE EXCEPTION 'business setting payment_hold_minutes is not configured' USING ERRCODE = '23514';
    END IF;
    NEW.hold_expires_at := LEAST(now() + make_interval(mins => hold_min), s_start);
  ELSIF NEW.payment_mode = 'studio' THEN
    dl := studio_payment_deadline(s_start);
    IF dl <= now() THEN
      RAISE EXCEPTION 'pay-at-studio is no longer available for this session; pay online instead'
        USING ERRCODE = '23514';
    END IF;
    NEW.hold_expires_at := dl;
  ELSE
    RAISE EXCEPTION 'a pass booking cannot be pending payment' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_booking_payment_rules BEFORE INSERT ON bookings
  FOR EACH ROW EXECUTE FUNCTION set_booking_payment_rules();

-- 4. Weekly class timetable and session generator
CREATE TABLE class_timetable (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  service_id  uuid NOT NULL REFERENCES services(id),
  trainer_id  uuid NOT NULL REFERENCES trainers(id),
  weekday     smallint NOT NULL CHECK (weekday BETWEEN 0 AND 6),
  start_time  time NOT NULL,
  capacity    integer CHECK (capacity > 0),
  is_active   boolean NOT NULL DEFAULT true,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (service_id, trainer_id, weekday, start_time),
  FOREIGN KEY (trainer_id, service_id) REFERENCES trainer_services (trainer_id, service_id)
);
CREATE TRIGGER trg_class_timetable_updated BEFORE UPDATE ON class_timetable
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Creates the real, bookable sessions for a date range (inclusive). Safe to run repeatedly:
-- sessions that already exist (scheduled OR cancelled) are left alone. Holidays and trainer leave are skipped and
-- counted. Any other problem (for example two timetable rows overlapping for one trainer)
-- raises an error instead of being skipped silently.
CREATE OR REPLACE FUNCTION generate_class_sessions(p_from date, p_to date)
RETURNS TABLE (created integer, skipped_holiday integer, skipped_leave integer) AS $$
DECLARE
  tz      text;
  d       date;
  r       record;
  s_start timestamptz;
  s_end   timestamptz;
  c       integer := 0;
  h       integer := 0;
  l       integer := 0;
BEGIN
  IF p_to < p_from THEN
    RAISE EXCEPTION 'p_to must not be before p_from' USING ERRCODE = '23514';
  END IF;
  SELECT value #>> '{}' INTO tz FROM business_settings WHERE key = 'timezone';
  tz := COALESCE(tz, 'Asia/Kolkata');

  FOR d IN SELECT g::date FROM generate_series(p_from, p_to, interval '1 day') AS g LOOP
    FOR r IN
      SELECT t.service_id, t.trainer_id, t.start_time, t.capacity, s.duration_minutes, s.default_capacity
        FROM class_timetable t JOIN services s ON s.id = t.service_id
       WHERE t.is_active AND t.weekday = EXTRACT(dow FROM d)::integer
       ORDER BY t.start_time
    LOOP
      s_start := (d + r.start_time) AT TIME ZONE tz;
      s_end   := s_start + make_interval(mins => r.duration_minutes);

      IF EXISTS (SELECT 1 FROM business_holidays WHERE holiday_date = d) THEN
        h := h + 1; CONTINUE;
      END IF;
      IF EXISTS (SELECT 1 FROM trainer_time_off o
                  WHERE o.trainer_id = r.trainer_id
                    AND tstzrange(o.starts_at, o.ends_at, '[)') && tstzrange(s_start, s_end, '[)')) THEN
        l := l + 1; CONTINUE;
      END IF;
      -- An existing session in this slot is respected whatever its status. A CANCELLED session
      -- must stay cancelled: re-creating it would silently undo the cancellation every night.
      IF EXISTS (SELECT 1 FROM sessions x
                  WHERE x.service_id = r.service_id AND x.trainer_id = r.trainer_id
                    AND x.starts_at = s_start) THEN
        CONTINUE;
      END IF;

      INSERT INTO sessions (service_id, trainer_id, starts_at, ends_at, capacity)
      VALUES (r.service_id, r.trainer_id, s_start, s_end, COALESCE(r.capacity, r.default_capacity));
      c := c + 1;
    END LOOP;
  END LOOP;

  RETURN QUERY SELECT c, h, l;
END $$ LANGUAGE plpgsql;

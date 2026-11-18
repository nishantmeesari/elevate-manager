-- Elevate Manager - Migration 005: quiet hours for ALL automated notifications
-- Applies on top of 001-004 (which are left untouched).
--
-- Owner decisions encoded here:
--   * No AUTOMATED customer notification of any type is sent between 21:00 and 06:00
--     (confirmation, invoice, reminder, cancellation, reschedule, waitlist offer).
--   * Manual receptionist contact (a phone call, or a message the receptionist sends
--     by hand) is NOT an automated notification. It never goes through the
--     notifications table, so it is not restricted. Record it in audit_logs
--     (action 'notification.manual') so it stays accountable.
--   * Waitlist overnight: a freed seat is reserved for the first person in line at once,
--     but their 30-minute claim clock starts when messages are allowed again (06:00).
--   * Minimum claim window: 15 minutes. If less than that remains before the class
--     starts, no offer is made and the seat is open to the public.
--   * An accepted claim is an ordinary website booking, so it gets the standard
--     15-minute payment hold from migration 003/004 (nothing to change here).
--
-- Policy VALUES live in business_settings (quiet_hours_start / quiet_hours_end,
-- waitlist_offer_minutes, waitlist_min_claim_minutes). Quiet hours are assumed to
-- run overnight (start later in the day than end), as in migration 003.

-- 1. The earliest moment a message may go out
--    Not in quiet hours -> the moment itself. In quiet hours -> the next 06:00 local.
--    Application code uses this to set notifications.scheduled_for / next_retry_at.
--    (Reminders keep using reminder_send_time(), which moves EARLIER, to 20:00 the evening
--    before, because a reminder sent after the class makes no sense.)
CREATE OR REPLACE FUNCTION next_allowed_send_time(p_ts timestamptz) RETURNS timestamptz AS $$
DECLARE
  tz text;
  qs time;
  qe time;
  l  timestamp;
  d  date;
BEGIN
  IF NOT in_quiet_hours(p_ts) THEN
    RETURN p_ts;
  END IF;
  SELECT value #>> '{}' INTO tz FROM business_settings WHERE key = 'timezone';
  SELECT (value #>> '{}')::time INTO qs FROM business_settings WHERE key = 'quiet_hours_start';
  SELECT (value #>> '{}')::time INTO qe FROM business_settings WHERE key = 'quiet_hours_end';
  tz := COALESCE(tz, 'Asia/Kolkata');
  l  := p_ts AT TIME ZONE tz;
  -- late evening part of the window -> the end of the window is tomorrow morning;
  -- early-morning part -> the end of the window is this morning
  d  := CASE WHEN l::time >= qs THEN l::date + 1 ELSE l::date END;
  RETURN (d + qe) AT TIME ZONE tz;
END $$ LANGUAGE plpgsql STABLE;

-- 2. Every automated notification type, including retries
--    Replaces the reminder-only trigger from migration 003.
DROP TRIGGER trg_reminder_quiet_hours ON notifications;
DROP FUNCTION guard_reminder_quiet_hours();

CREATE OR REPLACE FUNCTION guard_notification_quiet_hours() RETURNS trigger AS $$
BEGIN
  IF in_quiet_hours(NEW.scheduled_for) THEN
    RAISE EXCEPTION 'automated notifications cannot be scheduled during quiet hours (use next_allowed_send_time)'
      USING ERRCODE = '23514';
  END IF;
  IF NEW.next_retry_at IS NOT NULL AND in_quiet_hours(NEW.next_retry_at) THEN
    RAISE EXCEPTION 'a notification retry cannot be scheduled during quiet hours (use next_allowed_send_time)'
      USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_notification_quiet_hours
  BEFORE INSERT OR UPDATE OF scheduled_for, next_retry_at ON notifications
  FOR EACH ROW EXECUTE FUNCTION guard_notification_quiet_hours();

-- 3. Sender gate
--    The message sender must take its work from this function, never straight from the
--    table. It hands out nothing during quiet hours, so a late worker (down at 20:58,
--    back at 21:10) or a retry cannot send at night: the rows simply wait until 06:00.
--    Returns the claimed rows, now marked 'queued' with attempts + 1.
--    A scheduled row is claimed when its time has come. A failed row is claimed again
--    only when the sender has set a next_retry_at that has come (no endless retry loop).
CREATE OR REPLACE FUNCTION claim_due_notifications(p_limit integer DEFAULT 50, p_now timestamptz DEFAULT now())
RETURNS SETOF notifications AS $$
BEGIN
  IF in_quiet_hours(p_now) THEN
    RETURN;
  END IF;
  RETURN QUERY
  UPDATE notifications n
     SET status = 'queued', attempts = n.attempts + 1
   WHERE n.id IN (SELECT d.id FROM notifications d
                   WHERE (d.status = 'scheduled' AND d.scheduled_for <= p_now)
                      OR (d.status = 'failed' AND d.next_retry_at IS NOT NULL AND d.next_retry_at <= p_now)
                   ORDER BY COALESCE(d.next_retry_at, d.scheduled_for), d.id
                   LIMIT p_limit
                   FOR UPDATE SKIP LOCKED)
  RETURNING n.*;
END $$ LANGUAGE plpgsql;

-- 4. Waitlist offers: overnight reservation, 06:00 clock start, 15-minute minimum
--    Replaces offer_next_waitlist_seat() from migration 003 (same signature, so
--    expire_waitlist_offers() and expire_unpaid_holds() pick it up automatically).
--
--    The entry becomes 'offered' immediately, so the seat is reserved for the first
--    person in line (capacity counts offers that have not expired). offered_at is the
--    moment the claim clock starts, and the earliest moment the offer message may be
--    sent: the application must create the waitlist_offer notification with
--    scheduled_for = offered_at. offer_expires_at is offered_at + the offer window,
--    never later than the class start.
--
--    If less than waitlist_min_claim_minutes would remain before the class starts, no offer
--    is made: the entry stays 'waiting', the function returns NULL, and the seat is open
--    to the public.
CREATE OR REPLACE FUNCTION offer_next_waitlist_seat(p_session_id uuid, p_now timestamptz DEFAULT now())
RETURNS uuid AS $$
DECLARE
  cap       integer;
  sstat     text;
  s_start   timestamptz;
  taken     integer;
  offers    integer;
  win       integer;
  min_claim integer;
  w_open    timestamptz;
  w_close   timestamptz;
  eid       uuid;
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
  SELECT (value #>> '{}')::integer INTO min_claim FROM business_settings WHERE key = 'waitlist_min_claim_minutes';
  IF min_claim IS NULL THEN
    RAISE EXCEPTION 'business setting waitlist_min_claim_minutes is not configured' USING ERRCODE = '23514';
  END IF;

  w_open  := next_allowed_send_time(p_now);
  w_close := LEAST(w_open + make_interval(mins => win), s_start);
  IF w_close - w_open < make_interval(mins => min_claim) THEN
    RETURN NULL;
  END IF;

  SELECT id INTO eid FROM waitlist_entries
   WHERE session_id = p_session_id AND status = 'waiting'
   ORDER BY created_at, id LIMIT 1 FOR UPDATE SKIP LOCKED;
  IF eid IS NULL THEN
    RETURN NULL;
  END IF;

  UPDATE waitlist_entries
     SET status = 'offered', offered_at = w_open, offer_expires_at = w_close
   WHERE id = eid;
  RETURN eid;
END $$ LANGUAGE plpgsql;

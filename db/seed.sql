-- Elevate Manager - seed data for the Elevate Studio simulation
--
-- HOW TO RUN
--   1. Create an empty database.
--   2. Apply the migrations IN ORDER: 001, 002, 003, 004, 005, 006.
--   3. Run this file ONCE (it is not repeatable: a second run fails on duplicate keys).
--
-- WHY ALL SIX MIGRATIONS ARE REQUIRED
--   001  tables this file inserts into: business_settings, business_hours, services,
--        trainers, trainer_services, trainer_working_hours
--   002  pass_plans and pass_plan_services (the 10-session pass)
--   003  the payment-hold, waitlist and quiet-hours functions that READ the settings seeded
--        here (payment_hold_minutes, waitlist_offer_minutes, quiet_hours_*); it also adds the
--        booking columns that migration 004 relies on
--   004  class_timetable (the weekly timetable) and the studio-payment deadline function that
--        reads studio_payment_deadline_hours
--   005  the quiet-hours and overnight-waitlist functions that read waitlist_min_claim_minutes
--        (and, with 003, the quiet_hours_* settings)
--   006  customer authentication and local/test payment-intent tables used by the API
--   The check below stops the seed with a clear message if any of them is missing.
--
-- Values marked [DECIDED] come from the owner's answers.
-- Values marked [ASSUMED] were NOT specified and are placeholders to confirm.
-- Not seeded on purpose: users (need real password hashes, comes with auth),
-- holidays (list not provided). Class sessions are NOT seeded either: they are
-- generated from class_timetable by generate_class_sessions(from, to).

-- Prerequisite check: one object from each migration ---------------------
DO $$
DECLARE
  missing text[] := '{}';
BEGIN
  IF to_regclass('business_settings') IS NULL THEN missing := array_append(missing, '001'); END IF;
  IF to_regclass('pass_plans')        IS NULL THEN missing := array_append(missing, '002'); END IF;
  IF to_regclass('waitlist_entries')  IS NULL THEN missing := array_append(missing, '003'); END IF;
  IF to_regclass('class_timetable')   IS NULL THEN missing := array_append(missing, '004'); END IF;
  IF to_regproc('next_allowed_send_time') IS NULL THEN missing := array_append(missing, '005'); END IF;
  IF NOT EXISTS (
    SELECT 1
    FROM information_schema.columns
    WHERE table_name = 'customers' AND column_name = 'password_hash'
  ) OR to_regclass('payment_intents') IS NULL THEN
    missing := array_append(missing, '006');
  END IF;
  IF cardinality(missing) > 0 THEN
    RAISE EXCEPTION 'seed.sql needs migrations 001 through 006 applied first, in order; missing: %',
      array_to_string(missing, ', ') USING ERRCODE = 'object_not_in_prerequisite_state';
  END IF;
END $$;

-- Business settings ---------------------------------------------------
INSERT INTO business_settings (key, value) VALUES
  ('business_name',              '"Elevate Studio"'),
  ('business_city',               '"Pune, Maharashtra, India"'),
  ('timezone',                    '"Asia/Kolkata"'),
  ('cancellation_window_hours',   '6'),
  ('reschedule_window_hours',     '6'),
  ('max_reschedules_per_booking', '1'),
  ('reminder_offsets_minutes',    '[1440, 120]'),
  ('tax_enabled',                 'false'),
  ('slot_interval_minutes',       '60'),
  ('min_booking_lead_minutes',    '60'),
  ('max_advance_booking_days',    '30');

-- Operating hours: 06:00-21:00 [DECIDED]; open all 7 days [ASSUMED] ----
INSERT INTO business_hours (weekday, is_closed, open_time, close_time)
SELECT d, false, '06:00', '21:00' FROM generate_series(0, 6) AS d;

-- Services [DECIDED prices/capacities; ASSUMED 60-minute duration] ------
INSERT INTO services (name, duration_minutes, default_capacity, price_paise) VALUES
  ('Personal Training', 60,  1, 50000),
  ('Yoga',              60, 10, 30000),
  ('Group Fitness',     60, 15, 20000);

-- Trainers [DECIDED mapping] -------------------------------------------
INSERT INTO trainers (full_name) VALUES ('Trainer A'), ('Trainer B');

INSERT INTO trainer_services (trainer_id, service_id)
SELECT t.id, s.id
FROM (VALUES ('Trainer A', 'Personal Training'),
             ('Trainer A', 'Group Fitness'),
             ('Trainer B', 'Yoga'),
             ('Trainer B', 'Personal Training')) AS m(trainer, service)
JOIN trainers t ON t.full_name = m.trainer
JOIN services s ON s.name = m.service;

-- Trainer working hours: same as studio hours [ASSUMED] ------------------
INSERT INTO trainer_working_hours (trainer_id, weekday, start_time, end_time)
SELECT t.id, d, '06:00', '21:00' FROM trainers t, generate_series(0, 6) AS d;

-- Policies decided after migration 003 ----------------------------------
INSERT INTO business_settings (key, value) VALUES
  ('payment_hold_minutes',    '15'),
  ('waitlist_offer_minutes',  '30'),
  ('quiet_hours_start',       '"21:00"'),
  ('quiet_hours_end',         '"06:00"'),
  ('quiet_hours_send_time',   '"20:00"');

-- 10-session pass [DECIDED: Rs 2,500, 30 days, Group Fitness + Yoga only]
-- Rs 250 per session. Note: still above the Rs 200 Group Fitness drop-in price (Yoga drop-in is Rs 300).
INSERT INTO pass_plans (name, session_count, validity_days, price_paise)
VALUES ('10-Session Pass', 10, 30, 250000);

INSERT INTO pass_plan_services (pass_plan_id, service_id)
SELECT p.id, s.id FROM pass_plans p, services s
WHERE p.name = '10-Session Pass' AND s.name IN ('Yoga', 'Group Fitness');

-- Pay-at-studio rule [DECIDED]: pay at the front desk at least 2 hours before class,
-- otherwise the booking is released (and the seat offered to the waitlist).
INSERT INTO business_settings (key, value) VALUES ('studio_payment_deadline_hours', '2');

-- Weekly class timetable [DECIDED]: Monday-Saturday (weekday 1-6), no Sunday classes.
-- Capacities come from the service defaults (Yoga 10, Group Fitness 15), as decided.
INSERT INTO class_timetable (service_id, trainer_id, weekday, start_time, capacity)
SELECT s.id, t.id, d, c.start_time::time, c.capacity
FROM (VALUES ('Yoga',          'Trainer B', '07:00', 10),
             ('Group Fitness', 'Trainer A', '08:30', 15),
             ('Group Fitness', 'Trainer A', '18:00', 15),
             ('Yoga',          'Trainer B', '19:30', 10)
     ) AS c(service, trainer, start_time, capacity)
JOIN services s ON s.name = c.service
JOIN trainers t ON t.full_name = c.trainer
CROSS JOIN generate_series(1, 6) AS d;

-- Waitlist minimum claim window [DECIDED]: if less than 15 minutes would remain before the
-- class starts, no waitlist offer is made and the seat is open to the public.
INSERT INTO business_settings (key, value) VALUES ('waitlist_min_claim_minutes', '15');

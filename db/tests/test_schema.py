#!/usr/bin/env python3
"""
Database rule tests for Elevate Manager.

Builds a fresh database from 001 + 002 + seed, then tries to BREAK each business rule.
A test passes only when the database itself rejects the bad action (or allows the good one).

Usage:  python3 -I test_schema.py
Env:    PG_SOCKET_DIR (default /var/lib/postgresql/elevate_test/run), PG_PORT (default 54329)
"""
import os
import re
import sys
import threading
import traceback
import uuid
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import psycopg

HOST = os.environ.get("PG_SOCKET_DIR", "/var/lib/postgresql/elevate_test/run")
PORT = int(os.environ.get("PG_PORT", "54329"))
DB = "elevate_test"
HERE = os.path.dirname(os.path.abspath(__file__))
SQL_FILES = [os.path.join("migrations", f) for f in
             ["001_initial.sql", "002_passes_and_policies.sql", "003_holds_waitlist_quiet_hours.sql",
              "004_studio_payment_and_timetable.sql", "005_quiet_hours_all_notifications.sql",
              "006_application_auth_and_payments.sql"]]
SQL_FILES.append("seed.sql")
IST = ZoneInfo("Asia/Kolkata")

# SQLSTATE codes
CHECK, FK, UNIQUE, EXCLUSION, INTEGRITY = "23514", "23503", "23505", "23P01", "23000"


def conn(db=DB, autocommit=False):
    return psycopg.connect(host=HOST, port=PORT, user="postgres", dbname=db, autocommit=autocommit)


@contextmanager
def raises(sqlstate):
    """Run a block in its own transaction; it must fail with the given SQLSTATE."""
    c = conn()
    try:
        yield c
    except psycopg.Error as e:
        c.rollback()
        assert e.sqlstate == sqlstate, f"expected SQLSTATE {sqlstate}, got {e.sqlstate}: {str(e).splitlines()[0]}"
    else:
        c.rollback()
        raise AssertionError(f"expected SQLSTATE {sqlstate} but the statement succeeded")
    finally:
        c.close()


def build_scratch_database(name, filenames):
    """Create database `name` and apply only the given SQL files (used for prerequisite tests)."""
    with conn("postgres", autocommit=True) as c:
        c.execute(f"drop database if exists {name} with (force)")
        c.execute(f"create database {name}")
    with conn(name, autocommit=True) as c:
        for f in filenames:
            with open(os.path.join(HERE, "..", f)) as fh:
                c.execute(fh.read())


def drop_scratch_database(name):
    with conn("postgres", autocommit=True) as c:
        c.execute(f"drop database if exists {name} with (force)")


_day = [0]


def next_day():
    """A fresh calendar day for each test so tests never collide."""
    _day[0] += 1
    return date(2027, 3, 1) + timedelta(days=_day[0])


def at(d, hhmm):
    h, m = map(int, hhmm.split(":"))
    return datetime.combine(d, time(h, m), tzinfo=IST)


def one(c, sql, params=()):
    return c.execute(sql, params).fetchone()[0]


def mk_customer(c):
    phone = "+9198" + str(uuid.uuid4().int)[:8]
    return one(c, "insert into customers (full_name, phone_e164) values ('Test Customer', %s) returning id", (phone,))


def mk_session(c, trainer, service, d, hhmm):
    t = one(c, "select id from trainers where full_name=%s", (trainer,))
    s, dur, cap = c.execute(
        "select id, duration_minutes, default_capacity from services where name=%s", (service,)
    ).fetchone()
    start = at(d, hhmm)
    return one(
        c,
        "insert into sessions (service_id, trainer_id, starts_at, ends_at, capacity) "
        "values (%s,%s,%s,%s,%s) returning id",
        (s, t, start, start + timedelta(minutes=dur), cap),
    )


def mk_booking(c, session, customer, **kw):
    cols = {"booking_ref": "T-" + uuid.uuid4().hex[:12], "session_id": session, "customer_id": customer,
            "price_paise": 0, "source": "admin"}   # staff-created: may be confirmed directly
    if kw.get("member_pass_id") is not None:
        cols["payment_mode"] = "pass"
    cols.update(kw)
    names = ", ".join(cols)
    marks = ", ".join(["%s"] * len(cols))
    return one(c, f"insert into bookings ({names}) values ({marks}) returning id", tuple(cols.values()))


def cancel_booking(c, booking):
    c.execute("update bookings set status='cancelled', cancelled_at=now() where id=%s", (booking,))


def mk_invoice(c, booking, total=50000, tax=0, issue=date(2027, 3, 10), member_pass=None):
    fy = one(c, "select financial_year_of(%s)", (issue,))
    seq, number = c.execute("select * from next_invoice_number(%s)", (fy,)).fetchone()
    inv = one(
        c,
        "insert into invoices (invoice_number, financial_year, seq, booking_id, member_pass_id, customer_id, "
        "issue_date, subtotal_paise, tax_total_paise, total_paise) "
        "select %s,%s,%s,%s,%s,%s,%s,%s,%s,%s returning id",
        (number, fy, seq, booking, member_pass,
         one(c, "select customer_id from bookings where id=%s", (booking,)) if booking else
         one(c, "select customer_id from member_passes where id=%s", (member_pass,)),
         issue, total - tax, tax, total),
    )
    c.execute("insert into invoice_items (invoice_id, description, unit_price_paise, line_total_paise) "
              "values (%s,'Test item',%s,%s)", (inv, total - tax, total - tax))
    return inv


def pay(c, invoice, amount, kind="payment"):
    c.execute("insert into payments (invoice_id, kind, amount_paise, method) values (%s,%s,%s,'upi')",
              (invoice, kind, amount))


def status_of(c, invoice):
    return c.execute("select payment_status, paid_paise, refunded_paise, balance_paise "
                     "from invoice_balances where invoice_id=%s", (invoice,)).fetchone()


def fresh_booking(c, trainer="Trainer A", service="Personal Training", hhmm="10:00", d=None):
    d = d or next_day()
    s = mk_session(c, trainer, service, d, hhmm)
    cust = mk_customer(c)
    return mk_booking(c, s, cust), s, cust


def run_concurrently(fns):
    """Run callables in threads released together; return list of ('ok', value) / ('err', sqlstate)."""
    barrier = threading.Barrier(len(fns))
    results = [None] * len(fns)

    def worker(i, fn):
        c = conn()
        try:
            barrier.wait()
            val = fn(c)
            c.commit()
            results[i] = ("ok", val)
        except psycopg.Error as e:
            c.rollback()
            results[i] = ("err", e.sqlstate)
        finally:
            c.close()

    threads = [threading.Thread(target=worker, args=(i, fn)) for i, fn in enumerate(fns)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    return results


# ----------------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------------

def t_seed_matches_owner_decisions():
    with conn() as c:
        prices = dict(c.execute("select name, price_paise from services").fetchall())
        assert prices == {"Personal Training": 50000, "Yoga": 30000, "Group Fitness": 20000}, prices
        caps = dict(c.execute("select name, default_capacity from services").fetchall())
        assert caps == {"Personal Training": 1, "Yoga": 10, "Group Fitness": 15}, caps
        mapping = set(c.execute(
            "select t.full_name, s.name from trainer_services ts "
            "join trainers t on t.id=ts.trainer_id join services s on s.id=ts.service_id").fetchall())
        assert mapping == {("Trainer A", "Personal Training"), ("Trainer A", "Group Fitness"),
                           ("Trainer B", "Yoga"), ("Trainer B", "Personal Training")}, mapping
        st = {k: v for k, v in c.execute("select key, value from business_settings").fetchall()}
        assert st["cancellation_window_hours"] == 6 and st["reschedule_window_hours"] == 6
        assert st["max_reschedules_per_booking"] == 1
        assert st["reminder_offsets_minutes"] == [1440, 120]


def t_operating_hours_enforced():
    d = next_day()
    with raises(CHECK) as c:
        mk_session(c, "Trainer A", "Personal Training", d, "05:00")       # before 6 AM
    with raises(CHECK) as c:
        mk_session(c, "Trainer A", "Personal Training", d, "20:30")       # ends 9:30 PM
    with raises(CHECK) as c:
        mk_session(c, "Trainer A", "Personal Training", d, "23:30")       # spans midnight
    with conn() as c:
        mk_session(c, "Trainer A", "Personal Training", d, "06:00")       # exactly opening: ok
        mk_session(c, "Trainer A", "Personal Training", d, "20:00")       # ends exactly 9 PM: ok


def t_holiday_blocks_sessions():
    d = next_day()
    with conn() as c:
        c.execute("insert into business_holidays (holiday_date, name) values (%s, 'Test holiday')", (d,))
        c.commit()
    with raises(CHECK) as c:
        mk_session(c, "Trainer A", "Personal Training", d, "10:00")


def t_session_length_must_match_service():
    d = next_day()
    with raises(CHECK) as c:
        t = one(c, "select id from trainers where full_name='Trainer A'")
        s = one(c, "select id from services where name='Personal Training'")
        c.execute("insert into sessions (service_id, trainer_id, starts_at, ends_at, capacity) values (%s,%s,%s,%s,1)",
                  (s, t, at(d, "10:00"), at(d, "10:30")))


def t_trainer_service_mapping():
    d = next_day()
    with raises(FK) as c:
        mk_session(c, "Trainer A", "Yoga", d, "10:00")            # A does not teach yoga
    with raises(FK) as c:
        mk_session(c, "Trainer B", "Group Fitness", d, "10:00")   # B does not teach group fitness
    with conn() as c:
        mk_session(c, "Trainer B", "Yoga", d, "10:00")
        mk_session(c, "Trainer A", "Group Fitness", d, "12:00")
        mk_session(c, "Trainer B", "Personal Training", d, "14:00")


def t_trainer_cannot_overlap():
    d = next_day()
    with conn() as c:
        s1 = mk_session(c, "Trainer A", "Personal Training", d, "10:00")
        c.commit()
    with raises(EXCLUSION) as c:
        mk_session(c, "Trainer A", "Group Fitness", d, "10:00")   # same time, same trainer
    with conn() as c:
        mk_session(c, "Trainer A", "Personal Training", d, "11:00")   # back-to-back is fine
        mk_session(c, "Trainer B", "Personal Training", d, "10:00")   # other trainer is fine
        c.commit()
        # a cancelled session frees the trainer
        c.execute("update sessions set status='cancelled' where id=%s", (s1,))
        mk_session(c, "Trainer A", "Group Fitness", d, "10:00")


def t_trainer_leave_blocks_sessions():
    d = next_day()
    with conn() as c:
        tid = one(c, "select id from trainers where full_name='Trainer B'")
        c.execute("insert into trainer_time_off (trainer_id, starts_at, ends_at, reason) values (%s,%s,%s,'sick')",
                  (tid, at(d, "08:00"), at(d, "13:00")))
        c.commit()
    with raises(CHECK) as c:
        mk_session(c, "Trainer B", "Yoga", d, "10:00")
    with conn() as c:
        mk_session(c, "Trainer B", "Yoga", d, "14:00")


def t_inactive_trainer_or_service_blocked():
    d = next_day()
    with conn() as c:
        c.execute("update services set is_active=false where name='Yoga'")
        c.commit()
    try:
        with raises(CHECK) as c:
            mk_session(c, "Trainer B", "Yoga", d, "10:00")
    finally:
        with conn() as c:
            c.execute("update services set is_active=true where name='Yoga'")
            c.commit()


def t_group_capacity_under_concurrency():
    d = next_day()
    with conn() as c:
        s = mk_session(c, "Trainer B", "Yoga", d, "07:00")        # capacity 10
        customers = [mk_customer(c) for _ in range(14)]
        c.commit()
    res = run_concurrently([(lambda cc, cu=cu: mk_booking(cc, s, cu)) for cu in customers])
    ok = [r for r in res if r[0] == "ok"]
    errs = [r[1] for r in res if r[0] == "err"]
    assert len(ok) == 10, f"expected exactly 10 seats sold, got {len(ok)} ({res})"
    assert errs == [CHECK] * 4, errs
    with conn() as c:
        assert one(c, "select count(*) from bookings where session_id=%s and status='confirmed'", (s,)) == 10


def t_same_slot_race_one_winner():
    d = next_day()
    with conn() as c:
        c1, c2 = mk_customer(c), mk_customer(c)
        c.commit()

    def attempt(cust):
        def fn(cc):
            s = mk_session(cc, "Trainer A", "Personal Training", d, "09:00")
            return mk_booking(cc, s, cust)
        return fn

    res = run_concurrently([attempt(c1), attempt(c2)])
    assert sorted(r[0] for r in res) == ["err", "ok"], res
    assert [r[1] for r in res if r[0] == "err"] == [EXCLUSION], res


def t_cancel_frees_a_seat_and_full_class_rejects():
    d = next_day()
    with conn() as c:
        s = mk_session(c, "Trainer B", "Yoga", d, "07:00")
        bookings = [mk_booking(c, s, mk_customer(c)) for _ in range(10)]
        c.commit()
    with raises(CHECK) as c:
        mk_booking(c, s, mk_customer(c))                           # 11th person
    with conn() as c:
        cancel_booking(c, bookings[0])
        c.commit()
        mk_booking(c, s, mk_customer(c))                           # now there is room


def t_cannot_book_cancelled_session():
    d = next_day()
    with conn() as c:
        s = mk_session(c, "Trainer A", "Group Fitness", d, "07:00")
        c.execute("update sessions set status='cancelled' where id=%s", (s,))
        c.commit()
    with raises(CHECK) as c:
        mk_booking(c, s, mk_customer(c))


def t_same_customer_cannot_double_book_a_session():
    d = next_day()
    with conn() as c:
        s = mk_session(c, "Trainer A", "Group Fitness", d, "07:00")
        cust = mk_customer(c)
        mk_booking(c, s, cust)
        c.commit()
    with raises(UNIQUE) as c:
        mk_booking(c, s, cust)


def t_idempotency_key_blocks_duplicate_submit():
    d = next_day()
    with conn() as c:
        s1 = mk_session(c, "Trainer A", "Personal Training", d, "07:00")
        s2 = mk_session(c, "Trainer A", "Personal Training", d, "09:00")
        cust = mk_customer(c)
        key = "idem-" + uuid.uuid4().hex
        mk_booking(c, s1, cust, idempotency_key=key)
        c.commit()
    with raises(UNIQUE) as c:
        mk_booking(c, s2, cust, idempotency_key=key)


def t_phone_format_validated():
    with raises(CHECK) as c:
        c.execute("insert into customers (full_name, phone_e164) values ('Bad', '98765')")
    with raises(CHECK) as c:
        c.execute("insert into customers (full_name, phone_e164) values ('Bad', '+91 98765 43210')")


def t_cancellation_window_boundary():
    start = datetime(2027, 6, 1, 10, 0, tzinfo=IST)
    with conn() as c:
        q = "select is_too_late(%s, %s, 'cancellation_window_hours')"
        assert one(c, q, (start, start - timedelta(hours=6))) is False            # exactly 6h: still free
        assert one(c, q, (start, start - timedelta(hours=6, minutes=1))) is False
        assert one(c, q, (start, start - timedelta(hours=5, minutes=59))) is True  # inside window: late
        assert one(c, q, (start, start + timedelta(minutes=5))) is True            # after start: late
    with raises(CHECK) as c:
        c.execute("select is_too_late(now(), now(), 'no_such_setting')")


def t_reschedule_limit_is_one():
    d = next_day()
    with conn() as c:
        cust = mk_customer(c)
        s1 = mk_session(c, "Trainer A", "Personal Training", d, "07:00")
        s2 = mk_session(c, "Trainer A", "Personal Training", d, "09:00")
        s3 = mk_session(c, "Trainer A", "Personal Training", d, "11:00")
        b1 = mk_booking(c, s1, cust)
        cancel_booking(c, b1)
        # the app tries to cheat by sending reschedule_count=0; the database overrides it
        b2 = mk_booking(c, s2, cust, rescheduled_from_booking_id=b1, reschedule_count=0)
        assert one(c, "select reschedule_count from bookings where id=%s", (b2,)) == 1
        cancel_booking(c, b2)
        c.commit()
    with raises(CHECK) as c:
        mk_booking(c, s3, cust, rescheduled_from_booking_id=b2)    # second reschedule: blocked


def t_reschedule_must_be_same_customer():
    d = next_day()
    with conn() as c:
        b1, s1, cust1 = fresh_booking(c, d=d, hhmm="07:00")
        s2 = mk_session(c, "Trainer A", "Personal Training", d, "09:00")
        other = mk_customer(c)
        cancel_booking(c, b1)
        c.commit()
    with raises(CHECK) as c:
        mk_booking(c, s2, other, rescheduled_from_booking_id=b1)


def t_financial_year_and_gapless_numbers():
    with conn() as c:
        assert one(c, "select financial_year_of('2027-03-31')") == "2026-27"
        assert one(c, "select financial_year_of('2027-04-01')") == "2027-28"
        assert one(c, "select financial_year_of('2026-12-25')") == "2026-27"
    fy = "2090-91"
    c1 = conn()
    assert c1.execute("select * from next_invoice_number(%s)", (fy,)).fetchone() == (1, f"EL/{fy}/000001")
    c1.rollback()                                       # failed invoice creation: number must not be burned
    c1.close()
    with conn() as c:
        assert c.execute("select * from next_invoice_number(%s)", (fy,)).fetchone() == (1, f"EL/{fy}/000001")
        c.commit()
        assert c.execute("select * from next_invoice_number(%s)", (fy,)).fetchone() == (2, f"EL/{fy}/000002")
        c.commit()
    # concurrent callers never get the same number
    res = run_concurrently([lambda cc: cc.execute("select o_seq from next_invoice_number(%s)", (fy,)).fetchone()[0]
                            for _ in range(8)])
    seqs = sorted(r[1] for r in res if r[0] == "ok")
    assert seqs == list(range(3, 11)), seqs


def t_invoice_totals_must_add_up_and_one_live_invoice_per_booking():
    with conn() as c:
        b, _, _ = fresh_booking(c)
        inv = mk_invoice(c, b)
        c.commit()
    with raises(UNIQUE) as c:
        mk_invoice(c, b)                                # second live invoice for the same booking
    with raises(CHECK) as c:
        c.execute("insert into invoices (invoice_number, financial_year, seq, booking_id, customer_id, issue_date, "
                  "subtotal_paise, tax_total_paise, total_paise) "
                  "select 'X-BAD', '2026-27', 999999, id, customer_id, '2027-03-10', 100, 0, 999 "
                  "from bookings where id=%s", (b,))
    with conn() as c:                                   # voiding allows a replacement
        c.execute("update invoices set status='void', void_reason='wrong amount' where id=%s", (inv,))
        mk_invoice(c, b)


def t_invoice_must_have_exactly_one_subject():
    with raises(CHECK) as c:
        c.execute("insert into invoices (invoice_number, financial_year, seq, customer_id, issue_date, "
                  "subtotal_paise, total_paise) "
                  "select 'X-NONE', '2026-27', 999998, id, '2027-03-10', 100, 100 from customers limit 1")


def t_issued_invoice_is_immutable():
    with conn() as c:
        b, _, _ = fresh_booking(c)
        inv = mk_invoice(c, b)
        c.commit()
    with raises(INTEGRITY) as c:
        c.execute("update invoices set total_paise=1, subtotal_paise=1 where id=%s", (inv,))
    with raises(INTEGRITY) as c:
        c.execute("delete from invoices where id=%s", (inv,))
    with raises(INTEGRITY) as c:
        c.execute("update invoice_items set unit_price_paise=1 where invoice_id=%s", (inv,))
    with raises(CHECK) as c:
        c.execute("update invoices set status='void' where id=%s", (inv,))       # void needs a reason
    with conn() as c:
        c.execute("update invoices set pdf_storage_key='invoices/x.pdf' where id=%s", (inv,))   # allowed
        c.execute("update invoices set status='void', void_reason='test' where id=%s", (inv,))  # allowed
        c.commit()
    with raises(INTEGRITY) as c:
        c.execute("update invoices set void_reason='changed' where id=%s", (inv,))  # void is final


def t_payment_status_follows_the_ledger():
    with conn() as c:
        b, _, _ = fresh_booking(c)
        inv = mk_invoice(c, b, total=50000)
        c.commit()
        assert status_of(c, inv) == ("pending", 0, 0, 50000)
        pay(c, inv, 20000)
        assert status_of(c, inv) == ("partially_paid", 20000, 0, 30000)
        pay(c, inv, 30000)
        assert status_of(c, inv) == ("paid", 50000, 0, 0)
        pay(c, inv, 50000, kind="refund")
        assert status_of(c, inv) == ("refunded", 50000, 50000, 50000)
        c.commit()
        b2, _, _ = fresh_booking(c)
        inv2 = mk_invoice(c, b2)
        c.execute("update invoices set status='void', void_reason='test' where id=%s", (inv2,))
        assert status_of(c, inv2)[0] == "cancelled"
        c.commit()


def t_payments_cannot_overpay_overrefund_or_be_edited():
    with conn() as c:
        b, _, _ = fresh_booking(c)
        inv = mk_invoice(c, b, total=50000)
        pay(c, inv, 30000)
        c.commit()
    with raises(CHECK) as c:
        pay(c, inv, 20001)                               # would exceed total
    with raises(CHECK) as c:
        pay(c, inv, 30001, kind="refund")                # more than was paid
    with raises(INTEGRITY) as c:
        c.execute("update payments set amount_paise=1 where invoice_id=%s", (inv,))
    with raises(INTEGRITY) as c:
        c.execute("delete from payments where invoice_id=%s", (inv,))
    with conn() as c:
        c.execute("update invoices set status='void', void_reason='test' where id=%s", (inv,))
        c.commit()
    with raises(CHECK) as c:
        pay(c, inv, 100)                                 # void invoice takes no payments


def t_concurrent_payments_cannot_overpay():
    with conn() as c:
        b, _, _ = fresh_booking(c)
        inv = mk_invoice(c, b, total=50000)
        c.commit()
    res = run_concurrently([(lambda cc: pay(cc, inv, 30000)) for _ in range(2)])
    assert sorted(r[0] for r in res) == ["err", "ok"], res
    with conn() as c:
        assert status_of(c, inv)[1] == 30000


def _mk_plan(c, name, services, sessions=2):
    plan = one(c, "insert into pass_plans (name, session_count, validity_days, price_paise) values (%s,%s,30,100000) "
                  "returning id", (name + uuid.uuid4().hex[:6], sessions))
    for s in services:
        c.execute("insert into pass_plan_services select %s, id from services where name=%s", (plan, s))
    return plan


def _mk_pass(c, plan, cust, sessions=2, purchased="2027-02-20", expires="2027-04-30"):
    mp = one(c, "insert into member_passes (customer_id, pass_plan_id, sessions_total, purchased_at, expires_at) "
                "values (%s,%s,%s,%s,%s) returning id", (cust, plan, sessions, purchased, expires))
    c.execute("insert into pass_credit_ledger (member_pass_id, delta, reason) values (%s,%s,'purchase')",
              (mp, sessions))
    return mp


def _use(c, mp, booking):
    c.execute("insert into pass_credit_ledger (member_pass_id, booking_id, delta, reason) "
              "values (%s,%s,-1,'booking_use')", (mp, booking))


def t_pass_credits_never_negative_and_refund_once():
    d = next_day()
    with conn() as c:
        plan = _mk_plan(c, "PT2", ["Personal Training"])
        cust = mk_customer(c)
        mp = _mk_pass(c, plan, cust)
        bks = []
        for h in ("07:00", "09:00", "11:00"):
            s = mk_session(c, "Trainer A", "Personal Training", d, h)
            bks.append(mk_booking(c, s, cust, member_pass_id=mp))
        _use(c, mp, bks[0])
        c.commit()
    with raises(UNIQUE) as c:
        _use(c, mp, bks[0])                              # same booking cannot spend twice (1 credit still left)
    with conn() as c:
        _use(c, mp, bks[1])
        assert one(c, "select credits_remaining from pass_balances where member_pass_id=%s", (mp,)) == 0
        c.commit()
    with raises(CHECK) as c:
        _use(c, mp, bks[2])                              # out of credits
    with conn() as c:                                  # free cancellation gives the credit back, once
        c.execute("insert into pass_credit_ledger (member_pass_id, booking_id, delta, reason) "
                  "values (%s,%s,1,'cancel_refund')", (mp, bks[0]))
        assert one(c, "select credits_remaining from pass_balances where member_pass_id=%s", (mp,)) == 1
        c.commit()
    with raises(UNIQUE) as c:
        c.execute("insert into pass_credit_ledger (member_pass_id, booking_id, delta, reason) "
                  "values (%s,%s,1,'cancel_refund')", (mp, bks[0]))
    with raises(INTEGRITY) as c:
        c.execute("update pass_credit_ledger set delta=5 where member_pass_id=%s", (mp,))


def t_pass_must_cover_service_be_unexpired_and_owned():
    d = next_day()
    with conn() as c:
        plan = _mk_plan(c, "PTonly", ["Personal Training"])
        cust = mk_customer(c)
        mp = _mk_pass(c, plan, cust)
        yoga = mk_session(c, "Trainer B", "Yoga", d, "07:00")
        b_yoga = mk_booking(c, yoga, cust, member_pass_id=mp)
        expired = _mk_pass(c, plan, cust, purchased="2026-11-01", expires="2027-01-01")
        pt = mk_session(c, "Trainer A", "Personal Training", d, "09:00")
        b_pt = mk_booking(c, pt, cust, member_pass_id=expired)
        c.commit()
    with raises(CHECK) as c:
        _use(c, mp, b_yoga)                              # plan does not include yoga
    with raises(CHECK) as c:
        _use(c, expired, b_pt)                           # pass expires before the session
    with raises(FK) as c:                                # someone else's pass
        other = mk_customer(c)
        s = mk_session(c, "Trainer A", "Personal Training", next_day(), "07:00")
        mk_booking(c, s, other, member_pass_id=mp)


def t_invoice_can_be_for_a_pass_purchase():
    with conn() as c:
        plan = _mk_plan(c, "Inv", ["Personal Training"])
        cust = mk_customer(c)
        mp = _mk_pass(c, plan, cust)
        inv = mk_invoice(c, None, total=100000, member_pass=mp)
        c.commit()
    with raises(UNIQUE) as c:
        mk_invoice(c, None, total=100000, member_pass=mp)


def t_notification_dedupe_blocks_duplicate_reminders():
    with conn() as c:
        b, _, cust = fresh_booking(c)
        key = f"reminder:{b}:1440"
        q = ("insert into notifications (customer_id, booking_id, type, reminder_offset_minutes, provider, "
             "scheduled_for, dedupe_key) values (%s,%s,'reminder',1440,'mock','2027-03-01 12:00+05:30',%s)")
        c.execute(q, (cust, b, key))
        c.commit()
    with raises(UNIQUE) as c:
        c.execute(q, (cust, b, key))
    with raises(CHECK) as c:                             # provider must be explicit and known
        c.execute("insert into notifications (customer_id, type, provider, scheduled_for, dedupe_key) "
                  "values (%s,'reminder','pretend','2027-03-01 12:00+05:30','k-' || gen_random_uuid())", (cust,))


def mk_held(c, session, cust, minutes=15, mode="online"):
    """An unpaid booking holding a seat. The database sets the real deadline; tests then move it."""
    b = mk_booking(c, session, cust, status="pending_payment", payment_mode=mode)
    c.execute("update bookings set hold_expires_at = %s where id = %s",
              (datetime.now(IST) + timedelta(minutes=minutes), b))
    return b


def join_waitlist(c, session, cust):
    return one(c, "insert into waitlist_entries (session_id, customer_id) values (%s,%s) returning id",
               (session, cust))


def entry_state(c, entry):
    return c.execute("select status, booking_id, offered_at, offer_expires_at from waitlist_entries where id=%s",
                     (entry,)).fetchone()


def booking_status(c, booking):
    return one(c, "select status from bookings where id=%s", (booking,))


def t_new_decisions_seeded():
    with conn() as c:
        st = {k: v for k, v in c.execute("select key, value from business_settings").fetchall()}
        assert st["payment_hold_minutes"] == 15 and st["waitlist_offer_minutes"] == 30
        assert st["waitlist_min_claim_minutes"] == 15
        assert (st["quiet_hours_start"], st["quiet_hours_end"], st["quiet_hours_send_time"]) == ("21:00", "06:00", "20:00")
        n, days, price = c.execute("select session_count, validity_days, price_paise from pass_plans "
                                   "where name='10-Session Pass'").fetchone()
        assert (n, days, price) == (10, 30, 250000)
        covered = {r[0] for r in c.execute(
            "select s.name from pass_plan_services pps join pass_plans p on p.id=pps.pass_plan_id "
            "join services s on s.id=pps.service_id where p.name='10-Session Pass'").fetchall()}
        assert covered == {"Yoga", "Group Fitness"}, covered


def t_held_seats_count_toward_capacity_and_block_duplicates():
    d = next_day()
    with conn() as c:
        s = mk_session(c, "Trainer B", "Yoga", d, "07:00")
        s2 = mk_session(c, "Trainer B", "Yoga", d, "09:00")
        for _ in range(10):
            mk_held(c, s, mk_customer(c))
        cust = mk_customer(c)
        mk_held(c, s2, cust)
        c.commit()
    with raises(CHECK) as c:
        mk_booking(c, s, mk_customer(c))                 # 10 held seats = full class
    with raises(UNIQUE) as c:
        mk_booking(c, s2, cust)                          # a held seat is still that customer's seat


def t_hold_deadline_must_match_status():
    d = next_day()
    with conn() as c:
        s = mk_session(c, "Trainer A", "Group Fitness", d, "07:00")
        cust = mk_customer(c)
        b = mk_booking(c, s, cust, status="pending_payment")        # app forgot the deadline: DB sets it
        left = one(c, "select hold_expires_at - now() from bookings where id=%s", (b,))
        assert timedelta(minutes=14) < left <= timedelta(minutes=15), left
        c.commit()
    with raises(CHECK) as c:
        s2 = mk_session(c, "Trainer A", "Group Fitness", d, "12:00")
        mk_booking(c, s2, cust, hold_expires_at=datetime.now(IST) + timedelta(minutes=5))   # confirmed + deadline


def t_unpaid_hold_expires_and_frees_the_seat():
    d = next_day()
    with conn() as c:
        s = mk_session(c, "Trainer B", "Yoga", d, "07:00")
        for _ in range(9):
            mk_booking(c, s, mk_customer(c))
        stale = mk_held(c, s, mk_customer(c), minutes=-1)            # deadline already passed
        inv = mk_invoice(c, stale, total=30000)
        other_s = mk_session(c, "Trainer B", "Yoga", d, "09:00")
        fresh = mk_held(c, other_s, mk_customer(c), minutes=10)      # still within its 15 minutes
        c.commit()
    with raises(CHECK) as c:
        mk_booking(c, s, mk_customer(c))                              # full while the stale hold exists
    with conn() as c:
        assert one(c, "select expire_unpaid_holds()") == 1
        c.commit()
        assert booking_status(c, stale) == "expired"
        assert booking_status(c, fresh) == "pending_payment"          # untouched
        assert one(c, "select status from invoices where id=%s", (inv,)) == "void"
        mk_booking(c, s, mk_customer(c))                              # seat is back
        c.commit()
        assert one(c, "select expire_unpaid_holds()") == 0            # idempotent


def t_full_payment_confirms_a_held_booking():
    d = next_day()
    with conn() as c:
        s = mk_session(c, "Trainer B", "Yoga", d, "07:00")
        b = mk_held(c, s, mk_customer(c))
        inv = mk_invoice(c, b, total=30000)
        c.commit()
        pay(c, inv, 10000)
        c.commit()
        assert booking_status(c, b) == "pending_payment"              # part payment does not confirm
        pay(c, inv, 20000)
        c.commit()
        assert c.execute("select status, hold_expires_at from bookings where id=%s", (b,)).fetchone() == ("confirmed", None)
        assert one(c, "select expire_unpaid_holds()") == 0


def t_partial_payment_then_expiry_is_flagged_for_a_human():
    d = next_day()
    with conn() as c:
        s = mk_session(c, "Trainer B", "Yoga", d, "07:00")
        b = mk_held(c, s, mk_customer(c), minutes=-1)
        inv = mk_invoice(c, b, total=30000)
        pay(c, inv, 10000)
        c.commit()
        assert one(c, "select expire_unpaid_holds()") == 1
        c.commit()
        assert booking_status(c, b) == "expired"
        assert one(c, "select status from invoices where id=%s", (inv,)) == "issued"   # money was received: not voided
        row = c.execute("select net_paid_paise from paid_but_inactive_bookings where booking_id=%s", (b,)).fetchone()
        assert row == (10000,), row


def t_waitlist_flow_with_reserved_seat_claim_and_offer_expiry():
    d = next_day()
    with conn() as c:
        s = mk_session(c, "Trainer B", "Yoga", d, "07:00")
        s_free = mk_session(c, "Trainer B", "Yoga", d, "09:00")
        booked = [mk_customer(c) for _ in range(10)]
        bks = [mk_booking(c, s, cu) for cu in booked]
        w = [mk_customer(c) for _ in range(3)]
        stranger = mk_customer(c)
        c.commit()
    entries = []
    for cu in w:                                                       # joined in this order
        with conn() as c:
            entries.append(join_waitlist(c, s, cu))
    with raises(CHECK) as c:
        join_waitlist(c, s_free, stranger)                             # seats available: book instead
    with raises(CHECK) as c:
        join_waitlist(c, s, booked[0])                                 # already has a seat
    with conn() as c:
        cancel_booking(c, bks[0])
        offered = one(c, "select offer_next_waitlist_seat(%s)", (s,))
        assert offered == entries[0], "first in line gets the offer"
        status, _, off_at, off_exp = entry_state(c, offered)
        assert status == "offered" and timedelta(minutes=29, seconds=59) <= off_exp - off_at <= timedelta(minutes=30)
        assert one(c, "select offer_next_waitlist_seat(%s)", (s,)) is None    # one free seat, one offer
        c.commit()
    with raises(CHECK) as c:
        mk_booking(c, s, stranger)                                     # the seat is reserved for the first in line
    with raises(CHECK) as c:
        mk_booking(c, s, w[1])                                         # ... not for the second in line either
    with conn() as c:
        b_w1 = mk_booking(c, s, w[0])                                  # the offered customer can take it
        status, booking_id, _, _ = entry_state(c, entries[0])
        assert status == "claimed" and booking_id == b_w1
        assert one(c, "select offer_next_waitlist_seat(%s)", (s,)) is None    # full again
        cancel_booking(c, b_w1)
        assert one(c, "select offer_next_waitlist_seat(%s)", (s,)) == entries[1]
        c.commit()
        c.execute("update waitlist_entries set offer_expires_at = now() - interval '1 minute' where id=%s", (entries[1],))
        c.commit()
        assert one(c, "select expire_waitlist_offers()") == 1          # unclaimed in 30 minutes
        c.commit()
        assert entry_state(c, entries[1])[0] == "expired"
        assert entry_state(c, entries[2])[0] == "offered"              # passed to the next person


def t_waitlist_offer_never_outlasts_the_session():
    d = next_day()
    with conn() as c:
        s = mk_session(c, "Trainer B", "Yoga", d, "07:00")
        bks = [mk_booking(c, s, mk_customer(c)) for _ in range(10)]
        w = mk_customer(c)
        c.commit()
        e = join_waitlist(c, s, w)
        cancel_booking(c, bks[0])
        c.commit()
        start = one(c, "select starts_at from sessions where id=%s", (s,))
        assert one(c, "select offer_next_waitlist_seat(%s, %s)", (s, start + timedelta(minutes=1))) is None
        assert one(c, "select offer_next_waitlist_seat(%s, %s)", (s, start - timedelta(minutes=20))) == e
        assert entry_state(c, e)[3] == start                           # 20 minutes left, not 30
        c.commit()


def t_reminders_avoid_quiet_hours():
    d = next_day()
    prev = d - timedelta(days=1)
    with conn() as c:
        def rst(start, offset):
            return one(c, "select reminder_send_time(%s,%s)", (start, offset))
        assert rst(at(d, "07:00"), 120) == at(prev, "20:00")           # would be 05:00 -> evening before
        assert rst(at(d, "06:00"), 120) == at(prev, "20:00")           # would be 04:00 -> evening before
        assert rst(at(d, "08:00"), 120) == at(d, "06:00")              # 06:00 is outside quiet hours
        assert rst(at(d, "06:00"), 1440) == at(prev, "06:00")          # 24h reminder is fine as is
        assert rst(at(d, "14:00"), 1440) == at(prev, "14:00")
        assert rst(at(d, "20:00"), 120) == at(d, "18:00")
        assert rst(at(d, "23:30"), 90) == at(d, "20:00")               # 22:00 -> same evening at 20:00

        def quiet(hhmm):
            return one(c, "select in_quiet_hours(%s)", (at(d, hhmm),))
        assert all(quiet(t) for t in ("21:00", "23:59", "00:00", "05:59"))
        assert not any(quiet(t) for t in ("06:00", "12:00", "20:59"))

        cust = mk_customer(c)
        ins = ("insert into notifications (customer_id, type, provider, scheduled_for, dedupe_key) "
               "values (%s,%s,'mock',%s,%s)")
        c.execute(ins, (cust, "reminder", at(d, "20:00"), "q-" + uuid.uuid4().hex))
        c.commit()
    with raises(CHECK) as c:
        c.execute(ins, (cust, "reminder", at(d, "05:00"), "q-" + uuid.uuid4().hex))
    with raises(CHECK) as c:
        c.execute(ins, (cust, "reminder", at(d, "21:00"), "q-" + uuid.uuid4().hex))


def t_timetable_seeded_as_decided():
    with conn() as c:
        assert one(c, "select (value #>> '{}')::int from business_settings where key='studio_payment_deadline_hours'") == 2
        rows = c.execute(
            "select s.name, t.full_name, to_char(ct.start_time,'HH24:MI'), ct.capacity, "
            "       array_agg(ct.weekday order by ct.weekday) "
            "from class_timetable ct join services s on s.id=ct.service_id join trainers t on t.id=ct.trainer_id "
            "group by 1,2,3,4 order by 3").fetchall()
        mon_sat = [1, 2, 3, 4, 5, 6]
        assert rows == [("Yoga", "Trainer B", "07:00", 10, mon_sat),
                        ("Group Fitness", "Trainer A", "08:30", 15, mon_sat),
                        ("Group Fitness", "Trainer A", "18:00", 15, mon_sat),
                        ("Yoga", "Trainer B", "19:30", 10, mon_sat)], rows


def t_timetable_respects_trainer_mapping():
    with raises(FK) as c:
        c.execute("insert into class_timetable (service_id, trainer_id, weekday, start_time) "
                  "select s.id, t.id, 1, '10:00' from services s, trainers t "
                  "where s.name='Yoga' and t.full_name='Trainer A'")


def t_generate_sessions_from_timetable():
    with conn() as c:
        assert c.execute("select * from generate_class_sessions('2028-01-03','2028-01-09')").fetchone() == (24, 0, 0)
        c.commit()
        assert c.execute("select * from generate_class_sessions('2028-01-03','2028-01-09')").fetchone() == (0, 0, 0)  # repeat-safe
        rows = c.execute(
            "select to_char(starts_at at time zone 'Asia/Kolkata','HH24:MI'), s.name, t.full_name, x.capacity, "
            "       to_char(ends_at at time zone 'Asia/Kolkata','HH24:MI') "
            "from sessions x join services s on s.id=x.service_id join trainers t on t.id=x.trainer_id "
            "where (x.starts_at at time zone 'Asia/Kolkata')::date = '2028-01-03' order by x.starts_at").fetchall()
        assert rows == [("07:00", "Yoga", "Trainer B", 10, "08:00"),
                        ("08:30", "Group Fitness", "Trainer A", 15, "09:30"),
                        ("18:00", "Group Fitness", "Trainer A", 15, "19:00"),
                        ("19:30", "Yoga", "Trainer B", 10, "20:30")], rows
        assert one(c, "select count(*) from sessions where (starts_at at time zone 'Asia/Kolkata')::date = '2028-01-09'") == 0  # Sunday


def t_generator_skips_holidays_and_trainer_leave():
    with conn() as c:
        c.execute("insert into business_holidays (holiday_date, name) values ('2028-02-08', 'Test holiday')")
        tb = one(c, "select id from trainers where full_name='Trainer B'")
        c.execute("insert into trainer_time_off (trainer_id, starts_at, ends_at) values (%s,%s,%s)",
                  (tb, at(date(2028, 2, 9), "00:00"), at(date(2028, 2, 10), "00:00")))
        c.commit()
        # Mon 7 Feb: 4 classes. Tue 8 Feb: holiday, skips 4. Wed 9 Feb: Trainer B on leave, so 2 yoga skipped, 2 group created
        assert c.execute("select * from generate_class_sessions('2028-02-07','2028-02-09')").fetchone() == (6, 4, 2)


def t_studio_payment_deadline_is_two_hours_but_never_when_the_desk_is_closed():
    d = date(2027, 6, 2)                                    # a Wednesday
    prev = d - timedelta(days=1)
    with conn() as c:
        def dl(hhmm, day=d):
            return one(c, "select studio_payment_deadline(%s)", (at(day, hhmm),))
        assert dl("18:00") == at(d, "16:00")
        assert dl("19:30") == at(d, "17:30")
        assert dl("08:30") == at(d, "06:30")                # desk opens 06:00: fine
        assert dl("07:00") == at(prev, "21:00")             # 05:00 would be before opening: previous evening close
        assert dl("06:00") == at(prev, "21:00")
        # a closed previous day pushes the deadline back to the last open day
        c.execute("update business_hours set is_closed=true, open_time=null, close_time=null where weekday=0")
        c.commit()
    try:
        with conn() as c:
            monday = date(2027, 6, 7)
            assert one(c, "select studio_payment_deadline(%s)", (at(monday, "07:00"),)) == at(date(2027, 6, 5), "21:00")  # Saturday
    finally:
        with conn() as c:
            c.execute("update business_hours set is_closed=false, open_time='06:00', close_time='21:00' where weekday=0")
            c.commit()


def t_studio_booking_gets_deadline_from_the_database():
    d = next_day()
    with conn() as c:
        s = mk_session(c, "Trainer B", "Yoga", d, "19:30")
        b = mk_booking(c, s, mk_customer(c), source="website", payment_mode="studio", status="pending_payment",
                       hold_expires_at=at(d, "23:00"))       # the app tries to grant a longer hold: ignored
        assert one(c, "select hold_expires_at from bookings where id=%s", (b,)) == at(d, "17:30")
        c.commit()
    # too late to choose pay-at-studio: the 2-hour mark has already passed (a class in the past)
    with raises(CHECK) as c:
        s = mk_session(c, "Trainer B", "Yoga", date(2020, 1, 6), "07:00")
        mk_booking(c, s, mk_customer(c), source="website", payment_mode="studio", status="pending_payment")


def t_online_hold_never_outlasts_the_session():
    c = conn()
    try:
        s = mk_session(c, "Trainer B", "Yoga", date(2020, 1, 7), "07:00")
        b = mk_booking(c, s, mk_customer(c), source="website", status="pending_payment")
        start = one(c, "select starts_at from sessions where id=%s", (s,))
        assert one(c, "select hold_expires_at from bookings where id=%s", (b,)) == start
    finally:
        c.rollback()          # leave no expired hold behind for the other tests
        c.close()


def t_website_bookings_cannot_skip_payment():
    d = next_day()
    with conn() as c:
        s = mk_session(c, "Trainer B", "Yoga", d, "07:00")
        cust = mk_customer(c)
        c.commit()
    with raises(CHECK) as c:
        mk_booking(c, s, cust, source="website", payment_mode="online", status="confirmed")
    with raises(CHECK) as c:
        mk_booking(c, s, cust, source="website", payment_mode="studio", status="confirmed")
    with conn() as c:
        mk_booking(c, s, cust, source="admin", payment_mode="studio", status="confirmed")   # staff walk-in at the desk


def t_pass_mode_and_pass_must_go_together():
    d = next_day()
    with conn() as c:
        plan = _mk_plan(c, "Mode", ["Yoga"])
        cust = mk_customer(c)
        mp = _mk_pass(c, plan, cust)
        s = mk_session(c, "Trainer B", "Yoga", d, "07:00")
        c.commit()
    with raises(CHECK) as c:
        mk_booking(c, s, cust, payment_mode="pass")                       # pass mode without a pass
    with raises(CHECK) as c:
        mk_booking(c, s, cust, member_pass_id=mp, payment_mode="online")  # pass attached but not pass mode
    with raises(CHECK) as c:
        mk_booking(c, s, cust, member_pass_id=mp, status="pending_payment")   # a pass booking is never "unpaid"
    with conn() as c:
        mk_booking(c, s, cust, member_pass_id=mp, source="website")       # a pass booking may be confirmed instantly


def t_unpaid_studio_booking_is_released_to_the_waitlist():
    d = next_day()
    with conn() as c:
        s = mk_session(c, "Trainer B", "Yoga", d, "19:30")
        for _ in range(9):
            mk_booking(c, s, mk_customer(c))
        unpaid = mk_held(c, s, mk_customer(c), mode="studio")             # 10th seat, pay-at-studio
        paid_s = mk_session(c, "Trainer B", "Yoga", d, "07:00")
        paid = mk_held(c, paid_s, mk_customer(c), mode="studio")
        paid_inv = mk_invoice(c, paid, total=30000)
        unpaid_inv = mk_invoice(c, unpaid, total=30000)
        waiter = mk_customer(c)
        c.commit()
        entry = join_waitlist(c, s, waiter)                               # class is full, so joining is allowed
        c.execute("update bookings set hold_expires_at = now() - interval '1 minute' where id in (%s,%s)", (unpaid, paid))
        c.commit()
        pay(c, paid_inv, 30000)                                           # paid at the desk before the deadline
        c.commit()
        assert one(c, "select expire_unpaid_holds()") == 1                # only the unpaid one is released
        c.commit()
        assert booking_status(c, unpaid) == "expired" and booking_status(c, paid) == "confirmed"
        assert one(c, "select status from invoices where id=%s", (unpaid_inv,)) == "void"
        assert entry_state(c, entry)[0] == "offered"                      # the waitlist gets the seat


def t_generator_never_recreates_a_cancelled_session():
    d = date(2028, 3, 6)                                              # a Monday: 4 classes in the timetable
    with conn() as c:
        assert c.execute("select * from generate_class_sessions(%s,%s)", (d, d)).fetchone() == (4, 0, 0)
        yoga = one(c, "select id from sessions where service_id=(select id from services where name='Yoga') "
                      "and starts_at=%s", (at(d, "07:00"),))
        c.execute("update sessions set status='cancelled' where id=%s", (yoga,))
        c.commit()
        # the nightly run must NOT put the cancelled class back
        assert c.execute("select * from generate_class_sessions(%s,%s)", (d, d)).fetchone() == (0, 0, 0)
        c.commit()
        assert one(c, "select count(*) from sessions where starts_at >= %s and starts_at < %s",
                   (at(d, "00:00"), at(d + timedelta(days=1), "00:00"))) == 4          # no replacement row
        assert one(c, "select count(*) from sessions where starts_at=%s and service_id="
                      "(select id from services where name='Yoga')", (at(d, "07:00"),)) == 1
        assert one(c, "select status from sessions where id=%s", (yoga,)) == "cancelled"
        assert one(c, "select count(*) from sessions where status='scheduled' and starts_at >= %s and starts_at < %s",
                   (at(d, "00:00"), at(d + timedelta(days=1), "00:00"))) == 3


SEED_FILES = {"001": os.path.join("migrations", "001_initial.sql"),
              "002": os.path.join("migrations", "002_passes_and_policies.sql"),
              "003": os.path.join("migrations", "003_holds_waitlist_quiet_hours.sql"),
              "004": os.path.join("migrations", "004_studio_payment_and_timetable.sql"),
              "005": os.path.join("migrations", "005_quiet_hours_all_notifications.sql")}


def t_seed_refuses_to_run_without_all_migrations():
    seed_sql = open(os.path.join(HERE, "..", "seed.sql")).read()
    cases = [(["001"], "002, 003, 004, 005"),
             (["001", "002"], "003, 004, 005"),
             (["001", "002", "003"], "004, 005"),
             (["001", "002", "003", "004"], "005"),
             (["001", "002", "004"], "003, 005")]                     # 004 applies without 003 but would be broken
    for applied, expected_missing in cases:
        name = "elevate_seedcheck"
        build_scratch_database(name, [SEED_FILES[n] for n in applied])
        try:
            with conn(name, autocommit=True) as c:
                try:
                    c.execute(seed_sql)
                except psycopg.Error as e:
                    assert e.sqlstate == "55000", f"{applied}: expected SQLSTATE 55000, got {e.sqlstate}: {e}"
                    assert f"missing: {expected_missing}" in str(e), f"{applied}: {e}"
                else:
                    raise AssertionError(f"seed ran with only migrations {applied}")
                assert one(c, "select count(*) from business_settings") == 0, "seed must not leave partial data"
        finally:
            drop_scratch_database(name)


def t_seed_and_readme_state_the_same_prerequisites():
    db_dir = os.path.join(HERE, "..")
    migrations_dir = os.path.join(db_dir, "migrations")
    seed_sql = open(os.path.join(db_dir, "seed.sql")).read()
    readme = open(os.path.join(db_dir, "..", "README.md")).read()
    # the migrations the seed actually checks for are the single source of truth
    required = re.findall(r"array_append\(missing, '(\d{3})'\)", seed_sql)
    assert required == ["001", "002", "003", "004", "005"], required
    for n in required:
        assert any(f.startswith(n + "_") for f in os.listdir(migrations_dir)), f"migration {n} file not found"
    listed = ", ".join(required)
    header = seed_sql.split("-- Prerequisite check", 1)[0]
    assert listed in header and "IN ORDER" in header and "ONCE" in header, "seed.sql header instructions"
    assert "Setting up a database" in readme and listed in readme, "README setup instructions"
    assert "**once**" in readme and "in order" in readme, "README must say: in order, run seed once"
    for text, where in ((header, "seed.sql"), (readme, "README")):
        assert "after 001 and 002" not in text.lower(), f"stale prerequisite wording left in {where}"
    # the old narrow quiet-hours wording must not survive in the docs
    assert "only reminders are restricted" not in readme.lower(), "README still says only reminders are restricted"


NOTIFICATION_TYPES = ["booking_confirmation", "invoice", "reminder", "cancellation", "reschedule", "waitlist_offer"]


def mk_notification(c, ntype, when):
    return one(c, "insert into notifications (customer_id, type, provider, scheduled_for, dedupe_key) "
                  "values (%s,%s,'mock',%s,%s) returning id", (mk_customer(c), ntype, when, "n-" + uuid.uuid4().hex))


def full_yoga_class(c, d, hhmm, waiting=3):
    """A full Yoga class (10 seats, all confirmed) with `waiting` people queued in order.
    Returns (session, bookings, waiting_customers, waitlist_entries)."""
    s = mk_session(c, "Trainer B", "Yoga", d, hhmm)
    bks = [mk_booking(c, s, mk_customer(c)) for _ in range(10)]
    ws = [mk_customer(c) for _ in range(waiting)]
    c.commit()
    entries = []
    for w in ws:
        entries.append(join_waitlist(c, s, w))
        c.commit()                                    # separate transactions: queue order is unambiguous
    return s, bks, ws, entries


def t_next_allowed_send_time():
    d = next_day()
    nxt = d + timedelta(days=1)
    with conn() as c:
        def nat(day, hhmm):
            return one(c, "select next_allowed_send_time(%s)", (at(day, hhmm),))
        for hhmm in ("06:00", "12:00", "20:59"):
            assert nat(d, hhmm) == at(d, hhmm), hhmm                  # allowed times are untouched (06:00 is allowed)
        for hhmm in ("21:00", "23:59"):
            assert nat(d, hhmm) == at(nxt, "06:00"), hhmm             # evening -> tomorrow 06:00
        for hhmm in ("00:00", "03:00", "05:59"):
            assert nat(d, hhmm) == at(d, "06:00"), hhmm               # small hours -> this morning 06:00


def t_every_automated_notification_type_respects_quiet_hours():
    d = next_day()
    with conn() as c:
        for t in NOTIFICATION_TYPES:
            mk_notification(c, t, at(d, "06:00"))                     # edges of the allowed window are fine
            mk_notification(c, t, at(d, "20:59"))
        n = mk_notification(c, "invoice", at(d, "12:00"))
        c.commit()
    for t in NOTIFICATION_TYPES:
        for hhmm in ("21:00", "23:30", "00:00", "03:00", "05:59"):
            with raises(CHECK) as c:
                mk_notification(c, t, at(d, hhmm))
    with raises(CHECK) as c:                                          # moving an existing one into quiet hours
        c.execute("update notifications set scheduled_for=%s where id=%s", (at(d, "22:00"), n))
    with raises(CHECK) as c:                                          # a retry at night
        c.execute("update notifications set status='failed', next_retry_at=%s where id=%s", (at(d, "02:00"), n))
    with conn() as c:
        c.execute("update notifications set status='failed', next_retry_at=%s where id=%s", (at(d, "13:00"), n))


def t_sender_gate_never_hands_out_notifications_in_quiet_hours():
    d = next_day()
    nxt = d + timedelta(days=1)
    with conn() as c:
        late = mk_notification(c, "invoice", at(d, "20:30"))          # due 20:30, but the worker was down
        retry = mk_notification(c, "invoice", at(d, "12:00"))
        c.execute("update notifications set status='failed', next_retry_at=%s where id=%s", (at(nxt, "10:00"), retry))
        no_retry = mk_notification(c, "invoice", at(d, "12:30"))
        c.execute("update notifications set status='failed' where id=%s", (no_retry,))   # failed, no retry planned
        c.commit()

        def claim(ts):
            ids = {r[0] for r in c.execute("select id from claim_due_notifications(1000, %s)", (ts,)).fetchall()}
            c.commit()
            return ids

        assert claim(at(d, "21:10")) == set()                         # quiet hours: nothing is handed out at all
        assert one(c, "select status from notifications where id=%s", (late,)) == "scheduled"
        got = claim(at(nxt, "06:00"))                                 # quiet hours over: the late one goes out
        assert late in got and retry not in got and no_retry not in got
        assert c.execute("select status, attempts from notifications where id=%s", (late,)).fetchone() == ("queued", 1)
        assert late not in claim(at(nxt, "06:01"))                    # never handed out twice
        assert retry in claim(at(nxt, "10:00"))                       # the retry waits for its own time
        assert c.execute("select status, attempts from notifications where id=%s", (retry,)).fetchone() == ("queued", 1)
        assert no_retry not in claim(at(nxt, "12:00"))                # no retry time set: not retried automatically


def t_overnight_seat_is_reserved_and_claim_clock_starts_at_0600():
    d = next_day()
    with conn() as c:
        s, bks, ws, es = full_yoga_class(c, d, "07:00", waiting=3)
        stranger = mk_customer(c)
        cancel_booking(c, bks[0])
        c.commit()
        freed = at(d - timedelta(days=1), "21:00")                    # e.g. the pay-at-studio release for a 7 AM class
        assert one(c, "select offer_next_waitlist_seat(%s,%s)", (s, freed)) == es[0]
        c.commit()
        st, _, off_at, off_exp = entry_state(c, es[0])
        assert (st, off_at, off_exp) == ("offered", at(d, "06:00"), at(d, "06:30"))    # clock starts at 06:00
        assert one(c, "select offer_next_waitlist_seat(%s,%s)", (s, freed)) is None    # already reserved: one seat, one offer
    with raises(CHECK) as c:
        mk_booking(c, s, stranger)                                    # the public cannot take it overnight
    with conn() as c:
        assert one(c, "select expire_waitlist_offers(%s)", (at(d, "06:30"),)) >= 1     # first person did not claim
        c.commit()
        assert entry_state(c, es[0])[0] == "expired"
        st, _, off_at, off_exp = entry_state(c, es[1])
        assert (st, off_at, off_exp) == ("offered", at(d, "06:30"), at(d, "07:00"))    # 30 minutes, ends at class start
        one(c, "select expire_waitlist_offers(%s)", (at(d, "07:00"),))
        c.commit()
        assert entry_state(c, es[1])[0] == "expired"
        assert entry_state(c, es[2])[0] == "waiting"                  # the class has started: no more offers


def t_early_class_waitlist_gets_exactly_the_chances_that_fit():
    d = next_day()
    with conn() as c:
        s, bks, ws, es = full_yoga_class(c, d, "08:30", waiting=6)
        cancel_booking(c, bks[0])
        c.commit()
        assert one(c, "select offer_next_waitlist_seat(%s,%s)", (s, at(d - timedelta(days=1), "23:00"))) == es[0]
        c.commit()
        assert entry_state(c, es[0])[2] == at(d, "06:00")
        for i, t in enumerate(("06:30", "07:00", "07:30", "08:00", "08:30"), start=1):
            one(c, "select expire_waitlist_offers(%s)", (at(d, t),))
            c.commit()
            assert entry_state(c, es[i - 1])[0] == "expired", (i, t)
            if t != "08:30":
                st, _, off_at, off_exp = entry_state(c, es[i])
                assert (st, off_at, off_exp - off_at) == ("offered", at(d, t), timedelta(minutes=30)), (i, t)
        assert entry_state(c, es[5])[0] == "waiting"                  # five people got a chance, the sixth did not


def t_waitlist_is_bypassed_when_less_than_15_minutes_remain():
    d = next_day()
    d2 = next_day()
    with conn() as c:
        s1, b1, w1, e1 = full_yoga_class(c, d, "07:00", waiting=1)
        s2, b2, w2, e2 = full_yoga_class(c, d, "09:00", waiting=1)
        s3, b3, w3, e3 = full_yoga_class(c, d2, "06:10", waiting=1)
        strangers = [mk_customer(c) for _ in range(2)]
        for b in (b1[0], b2[0], b3[0]):
            cancel_booking(c, b)
        c.commit()
        # 10 minutes before a 07:00 class: no offer, the seat is open to the public
        assert one(c, "select offer_next_waitlist_seat(%s,%s)", (s1, at(d, "06:50"))) is None
        assert entry_state(c, e1[0])[0] == "waiting"
        mk_booking(c, s1, strangers[0])
        # exactly 15 minutes before a 09:00 class: still offered
        assert one(c, "select offer_next_waitlist_seat(%s,%s)", (s2, at(d, "08:45"))) == e2[0]
        assert entry_state(c, e2[0])[3] == at(d, "09:00")
        # freed at 05:55 for a 06:10 class: the window would open at 06:00 with 10 minutes left -> bypass
        assert one(c, "select offer_next_waitlist_seat(%s,%s)", (s3, at(d2, "05:55"))) is None
        assert entry_state(c, e3[0])[0] == "waiting"
        mk_booking(c, s3, strangers[1])
        c.commit()


def t_accepted_waitlist_claim_gets_the_standard_15_minute_hold():
    d = next_day()
    with conn() as c:
        s, bks, ws, es = full_yoga_class(c, d, "07:00", waiting=2)
        stranger = mk_customer(c)
        cancel_booking(c, bks[0])
        c.commit()
        assert one(c, "select offer_next_waitlist_seat(%s)", (s,)) == es[0]      # real clock: valid at any hour
        c.commit()
    with raises(CHECK) as c:
        mk_booking(c, s, stranger, source="website", status="pending_payment")   # cannot jump the queue
    with conn() as c:
        b = mk_booking(c, s, ws[0], source="website", status="pending_payment")  # the offered person claims
        st, booking_id, _, _ = entry_state(c, es[0])
        assert (st, booking_id) == ("claimed", b)
        left = one(c, "select hold_expires_at - now() from bookings where id=%s", (b,))
        assert timedelta(minutes=14) < left <= timedelta(minutes=15), left          # the standard payment hold
        c.execute("update bookings set hold_expires_at = now() - interval '1 minute' where id=%s", (b,))
        c.commit()
        assert one(c, "select expire_unpaid_holds()") == 1                          # not paid in time
        c.commit()
        assert booking_status(c, b) == "expired"
        assert entry_state(c, es[1])[0] == "offered"                                # the next person in line


def t_pay_at_studio_release_for_7am_class_is_offered_at_0600():
    d = next_day()
    with conn() as c:
        s = mk_session(c, "Trainer B", "Yoga", d, "07:00")
        for _ in range(9):
            mk_booking(c, s, mk_customer(c))
        held = mk_held(c, s, mk_customer(c), mode="studio")
        waiter = mk_customer(c)
        c.commit()
        e = join_waitlist(c, s, waiter)
        c.commit()
        deadline = one(c, "select studio_payment_deadline(%s)", (at(d, "07:00"),))
        assert deadline == at(d - timedelta(days=1), "21:00")                       # desk closes 21:00 the evening before
        c.execute("update bookings set hold_expires_at=%s where id=%s", (deadline, held))
        c.commit()
        assert one(c, "select expire_unpaid_holds(%s)", (deadline,)) >= 1
        c.commit()
        assert booking_status(c, held) == "expired"
        st, _, off_at, off_exp = entry_state(c, e)
        assert (st, off_at, off_exp) == ("offered", at(d, "06:00"), at(d, "06:30"))  # not messaged at 21:00


def t_audit_log_is_append_only():
    with conn() as c:
        c.execute("insert into audit_logs (actor_type, action, entity_type, entity_id) "
                  "values ('system','test','x','1')")
        c.commit()
    with raises(INTEGRITY) as c:
        c.execute("update audit_logs set action='tampered'")
    with raises(INTEGRITY) as c:
        c.execute("delete from audit_logs")


# ----------------------------------------------------------------------------
# Runner
# ----------------------------------------------------------------------------

def setup_database():
    with conn("postgres", autocommit=True) as c:
        c.execute(f"drop database if exists {DB} with (force)")
        c.execute(f"create database {DB}")
    with conn(autocommit=True) as c:
        for f in SQL_FILES:
            path = os.path.join(HERE, "..", f)
            with open(path) as fh:
                c.execute(fh.read())
            print(f"  applied {f}")


def main():
    print("Building fresh database...")
    setup_database()
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("t_") and callable(f)]
    if os.environ.get("REVERSE"):
        tests.reverse()          # REVERSE=1 runs the tests backwards to expose hidden dependencies between them
    passed, failed = 0, []
    print(f"\nRunning {len(tests)} tests\n")
    for name, fn in tests:
        try:
            fn()
            passed += 1
            print(f"  PASS  {name}")
        except Exception as e:
            failed.append(name)
            print(f"  FAIL  {name}: {type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}")
            if os.environ.get("VERBOSE"):
                traceback.print_exc()
    print(f"\n{passed} passed, {len(failed)} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()

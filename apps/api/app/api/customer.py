import os
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.encoders import jsonable_encoder
from psycopg import Connection, errors
from psycopg.types.json import Jsonb

from app.db import get_write_connection
from app.schemas.customer import PassPurchaseRequest, PaymentConfirmationRequest
from app.security import Principal, require_customer


router = APIRouter(prefix="/api/me")


def _audit_customer(
    connection: Connection,
    customer_id: UUID,
    action: str,
    entity_type: str,
    entity_id: UUID,
    after: Any,
) -> None:
    connection.execute(
        """
        INSERT INTO audit_logs (
            actor_type, action, entity_type, entity_id, after_data
        )
        VALUES ('customer', %s, %s, %s, %s)
        """,
        (action, entity_type, str(entity_id), Jsonb(jsonable_encoder(after))),
    )


def _customer_id(principal: Principal) -> UUID:
    return UUID(principal.subject_id)


@router.get("")
def get_profile(
    principal: Principal = Depends(require_customer),
    connection: Connection = Depends(get_write_connection),
) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT id, full_name, phone_e164, email, whatsapp_opt_in, created_at
        FROM customers
        WHERE id = %s AND anonymized_at IS NULL
        """,
        (_customer_id(principal),),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    return dict(row)


@router.get("/bookings")
def list_my_bookings(
    principal: Principal = Depends(require_customer),
    connection: Connection = Depends(get_write_connection),
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT b.id, b.booking_ref, b.session_id, b.status, b.payment_mode,
               b.member_pass_id, b.price_paise, b.hold_expires_at,
               b.reschedule_count, s.starts_at, s.ends_at,
               svc.name AS service_name, t.full_name AS trainer_name
        FROM bookings b
        JOIN sessions s ON s.id = b.session_id
        JOIN services svc ON svc.id = s.service_id
        JOIN trainers t ON t.id = s.trainer_id
        WHERE b.customer_id = %s
        ORDER BY s.starts_at DESC, b.id
        """,
        (_customer_id(principal),),
    ).fetchall()
    return [dict(row) for row in rows]


@router.get("/passes")
def list_my_passes(
    principal: Principal = Depends(require_customer),
    connection: Connection = Depends(get_write_connection),
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT mp.id, mp.pass_plan_id, pp.name, mp.sessions_total,
               mp.purchased_at, mp.expires_at, mp.status,
               pb.credits_remaining
        FROM member_passes mp
        JOIN pass_plans pp ON pp.id = mp.pass_plan_id
        JOIN pass_balances pb ON pb.member_pass_id = mp.id
        WHERE mp.customer_id = %s
        ORDER BY mp.expires_at DESC, mp.id
        """,
        (_customer_id(principal),),
    ).fetchall()
    return [dict(row) for row in rows]


@router.get("/payments")
def list_my_payments(
    principal: Principal = Depends(require_customer),
    connection: Connection = Depends(get_write_connection),
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT p.id, p.invoice_id, p.kind, p.amount_paise, p.method,
               p.reference, p.received_at
        FROM payments p
        JOIN invoices i ON i.id = p.invoice_id
        WHERE i.customer_id = %s
        ORDER BY p.received_at DESC, p.id
        """,
        (_customer_id(principal),),
    ).fetchall()
    return [dict(row) for row in rows]


@router.get("/invoices")
def list_my_invoices(
    principal: Principal = Depends(require_customer),
    connection: Connection = Depends(get_write_connection),
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT ib.*
        FROM invoice_balances ib
        JOIN invoices i ON i.id = ib.invoice_id
        WHERE i.customer_id = %s
        ORDER BY i.issue_date DESC, i.id
        """,
        (_customer_id(principal),),
    ).fetchall()
    return [dict(row) for row in rows]


def _invoice_pdf(invoice: dict[str, Any]) -> bytes:
    lines = [
        f"Elevate Studio - Invoice {invoice['invoice_number']}",
        f"Financial year: {invoice['financial_year']}",
        f"Subtotal (paise): {invoice['subtotal_paise']}",
        f"Tax (paise): {invoice['tax_total_paise']}",
        f"Total (paise): {invoice['total_paise']}",
        f"Payment status: {invoice['payment_status']}",
    ]
    content = "BT\n/F1 12 Tf\n50 760 Td\n"
    content += "\n".join(f"({line.replace('(', '[').replace(')', ']')}) Tj\n0 -20 Td" for line in lines)
    content += "\nET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(content.encode('latin-1'))} >>\nstream\n{content}\nendstream".encode("latin-1"),
    ]
    pdf = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, obj in enumerate(objects, 1):
        offsets.append(len(pdf))
        pdf.extend(f"{number} 0 obj\n".encode("ascii"))
        pdf.extend(obj)
        pdf.extend(b"\nendobj\n")
    xref = len(pdf)
    pdf.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("ascii"))
    pdf.extend("".join(f"{offset:010d} 00000 n \n" for offset in offsets).encode("ascii"))
    pdf.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode(
            "ascii"
        )
    )
    return bytes(pdf)


@router.get("/invoices/{invoice_id}")
def get_my_invoice(
    invoice_id: UUID,
    principal: Principal = Depends(require_customer),
    connection: Connection = Depends(get_write_connection),
) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT ib.*
        FROM invoice_balances ib
        JOIN invoices i ON i.id = ib.invoice_id
        WHERE ib.invoice_id = %s AND i.customer_id = %s
        """,
        (invoice_id, _customer_id(principal)),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return dict(row)


@router.get("/invoices/{invoice_id}/pdf")
def download_my_invoice_pdf(
    invoice_id: UUID,
    principal: Principal = Depends(require_customer),
    connection: Connection = Depends(get_write_connection),
) -> Response:
    row = connection.execute(
        """
        SELECT i.invoice_number, i.financial_year, i.subtotal_paise,
               i.tax_total_paise, i.total_paise, ib.payment_status
        FROM invoices i
        JOIN invoice_balances ib ON ib.invoice_id = i.id
        WHERE i.id = %s AND i.customer_id = %s
        """,
        (invoice_id, _customer_id(principal)),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return Response(
        content=_invoice_pdf(dict(row)),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{row["invoice_number"]}.pdf"'},
    )


@router.post("/pass-purchases", status_code=201)
def create_pass_purchase(
    request: PassPurchaseRequest,
    principal: Principal = Depends(require_customer),
    connection: Connection = Depends(get_write_connection),
) -> dict[str, Any]:
    if not request.idempotency_key.strip():
        raise HTTPException(status_code=422, detail="idempotency_key must not be blank")
    customer_id = _customer_id(principal)
    existing = connection.execute(
        """
        SELECT id, customer_id, status, amount_paise, member_pass_id, failure_reason
        FROM payment_intents
        WHERE idempotency_key = %s
        """,
        (request.idempotency_key,),
    ).fetchone()
    if existing is not None:
        if existing["customer_id"] != customer_id:
            raise HTTPException(status_code=409, detail="Payment idempotency key is already in use")
        return dict(existing)
    plan = connection.execute(
        """
        SELECT id, price_paise
        FROM pass_plans
        WHERE id = %s AND is_active
        """,
        (request.pass_plan_id,),
    ).fetchone()
    if plan is None:
        raise HTTPException(status_code=404, detail="Pass plan not found")
    try:
        intent = connection.execute(
            """
            INSERT INTO payment_intents (
                customer_id, pass_plan_id, idempotency_key, amount_paise, provider
            )
            VALUES (%s, %s, %s, %s, 'local_test')
            RETURNING id, status, amount_paise, member_pass_id, failure_reason
            """,
            (
                customer_id,
                request.pass_plan_id,
                request.idempotency_key,
                plan["price_paise"],
            ),
        ).fetchone()
        _audit_customer(
            connection,
            customer_id,
            "payment_intent.create",
            "payment_intent",
            intent["id"],
            intent,
        )
    except errors.UniqueViolation:
        connection.rollback()
        intent = connection.execute(
            """
            SELECT id, customer_id, status, amount_paise, member_pass_id, failure_reason
            FROM payment_intents
            WHERE idempotency_key = %s AND customer_id = %s
            """,
            (request.idempotency_key, customer_id),
        ).fetchone()
        if intent is None:
            raise HTTPException(status_code=409, detail="Payment idempotency key is already in use") from None
    return dict(intent)


@router.post("/pass-purchases/{intent_id}/confirm")
def confirm_pass_purchase(
    intent_id: UUID,
    request: PaymentConfirmationRequest,
    principal: Principal = Depends(require_customer),
    connection: Connection = Depends(get_write_connection),
) -> dict[str, Any]:
    if os.environ.get("ALLOW_LOCAL_TEST_PAYMENTS", "").lower() != "true":
        raise HTTPException(
            status_code=503,
            detail="Local payment confirmation is disabled",
        )
    if request.outcome not in {"succeed", "fail"}:
        raise HTTPException(status_code=422, detail="Outcome must be succeed or fail")
    intent = connection.execute(
        """
        SELECT *
        FROM payment_intents
        WHERE id = %s AND customer_id = %s
        FOR UPDATE
        """,
        (intent_id, _customer_id(principal)),
    ).fetchone()
    if intent is None:
        raise HTTPException(status_code=404, detail="Payment intent not found")
    if intent["status"] == "succeeded":
        return dict(intent)
    if intent["status"] == "failed":
        raise HTTPException(status_code=409, detail="Payment intent has already failed")
    if request.outcome == "fail":
        connection.execute(
            """
            UPDATE payment_intents
               SET status = 'failed', failure_reason = 'local test payment failed'
             WHERE id = %s
            """,
            (intent_id,),
        )
        failed = connection.execute(
            "SELECT * FROM payment_intents WHERE id = %s", (intent_id,)
        ).fetchone()
        _audit_customer(
            connection,
            intent["customer_id"],
            "payment_intent.fail",
            "payment_intent",
            intent_id,
            failed,
        )
        return dict(failed)

    plan = connection.execute(
        """
        SELECT id, session_count, validity_days, price_paise, tax_rate_bps
        FROM pass_plans
        WHERE id = %s AND is_active
        FOR SHARE
        """,
        (intent["pass_plan_id"],),
    ).fetchone()
    if plan is None:
        connection.rollback()
        raise HTTPException(status_code=409, detail="Pass plan is no longer available")
    try:
        member_pass = connection.execute(
            """
            INSERT INTO member_passes (
                customer_id, pass_plan_id, sessions_total, purchased_at, expires_at
            )
            VALUES (
                %s, %s, %s, statement_timestamp(),
                statement_timestamp() + make_interval(days => %s)
            )
            RETURNING id
            """,
            (
                intent["customer_id"],
                plan["id"],
                plan["session_count"],
                plan["validity_days"],
            ),
        ).fetchone()
        tax_enabled = connection.execute(
            """
            SELECT COALESCE((value #>> '{}')::boolean, false) AS enabled
            FROM business_settings
            WHERE key = 'tax_enabled'
            """
        ).fetchone()["enabled"]
        tax = (
            (plan["price_paise"] * plan["tax_rate_bps"] + 5000) // 10000
            if tax_enabled
            else 0
        )
        financial_year = connection.execute(
            "SELECT financial_year_of(current_date) AS financial_year"
        ).fetchone()["financial_year"]
        invoice_number = connection.execute(
            "SELECT * FROM next_invoice_number(%s)",
            (financial_year,),
        ).fetchone()
        invoice = connection.execute(
            """
            INSERT INTO invoices (
                invoice_number, financial_year, seq, member_pass_id,
                customer_id, issue_date, subtotal_paise, tax_total_paise, total_paise
            )
            VALUES (%s, %s, %s, %s, %s, current_date, %s, %s, %s)
            RETURNING id
            """,
            (
                invoice_number["o_number"],
                financial_year,
                invoice_number["o_seq"],
                member_pass["id"],
                intent["customer_id"],
                plan["price_paise"],
                tax,
                plan["price_paise"] + tax,
            ),
        ).fetchone()
        connection.execute(
            """
            INSERT INTO invoice_items (
                invoice_id, description, quantity, unit_price_paise,
                tax_rate_bps, tax_amount_paise, line_total_paise
            )
            VALUES (%s, 'Pass purchase', 1, %s, %s, %s, %s)
            """,
            (
                invoice["id"],
                plan["price_paise"],
                plan["tax_rate_bps"] if tax_enabled else 0,
                tax,
                plan["price_paise"] + tax,
            ),
        )
        connection.execute(
            """
            INSERT INTO payments (invoice_id, kind, amount_paise, method, reference)
            VALUES (%s, 'payment', %s, 'other', %s)
            """,
            (invoice["id"], intent["amount_paise"], f"local-test:{intent_id}"),
        )
        connection.execute(
            """
            INSERT INTO pass_credit_ledger (member_pass_id, delta, reason)
            VALUES (%s, %s, 'purchase')
            """,
            (member_pass["id"], plan["session_count"]),
        )
        completed = connection.execute(
            """
            UPDATE payment_intents
               SET status = 'succeeded', member_pass_id = %s
             WHERE id = %s
            RETURNING *
            """,
            (member_pass["id"], intent_id),
        ).fetchone()
        _audit_customer(
            connection,
            intent["customer_id"],
            "payment_intent.succeed",
            "payment_intent",
            intent_id,
            completed,
        )
        _audit_customer(
            connection,
            intent["customer_id"],
            "pass.activate",
            "member_pass",
            member_pass["id"],
            {"id": member_pass["id"], "invoice_id": invoice["id"]},
        )
        _audit_customer(
            connection,
            intent["customer_id"],
            "invoice.create",
            "invoice",
            invoice["id"],
            {"id": invoice["id"], "invoice_number": invoice_number["o_number"]},
        )
        return {
            "payment_intent": dict(completed),
            "invoice_id": invoice["id"],
            "member_pass_id": member_pass["id"],
        }
    except errors.DatabaseError:
        connection.rollback()
        raise HTTPException(status_code=503, detail="Payment operation failed") from None

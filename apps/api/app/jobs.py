from uuid import UUID

from psycopg import Connection


def schedule_booking_notifications(
    connection: Connection,
    booking_id: UUID,
) -> int:
    booking = connection.execute(
        """
        SELECT b.id, b.customer_id, b.session_id, s.starts_at
        FROM bookings b
        JOIN sessions s ON s.id = b.session_id
        WHERE b.id = %s
        """,
        (booking_id,),
    ).fetchone()
    if booking is None:
        return 0
    inserted = connection.execute(
        """
        INSERT INTO notifications (
            customer_id, booking_id, channel, type, provider,
            scheduled_for, dedupe_key
        )
        VALUES (
            %s, %s, 'email', 'booking_confirmation', 'mock',
            CASE WHEN in_quiet_hours(statement_timestamp())
                 THEN next_allowed_send_time(statement_timestamp())
                 ELSE statement_timestamp() END,
            %s
        )
        ON CONFLICT (dedupe_key) DO NOTHING
        """,
        (
            booking["customer_id"],
            booking_id,
            f"booking-confirmation:{booking_id}",
        ),
    ).rowcount
    offsets = connection.execute(
        """
        SELECT ARRAY(
            SELECT jsonb_array_elements_text(value)::integer
        ) AS offsets
        FROM business_settings
        WHERE key = 'reminder_offsets_minutes'
        """
    ).fetchone()["offsets"]
    for offset in offsets:
        connection.execute(
            """
            INSERT INTO notifications (
                customer_id, booking_id, channel, type, reminder_offset_minutes,
                provider, scheduled_for, dedupe_key
            )
            SELECT %s, %s, 'email', 'reminder', %s, 'mock',
                   reminder_send_time(%s, %s),
                   %s
            ON CONFLICT (dedupe_key) DO NOTHING
            """,
            (
                booking["customer_id"],
                booking_id,
                offset,
                booking["starts_at"],
                offset,
                f"reminder:{booking_id}:{offset}",
            ),
        )
    return inserted


def schedule_event_notification(
    connection: Connection,
    customer_id: UUID,
    booking_id: UUID,
    event_type: str,
) -> int:
    if event_type not in {"cancellation", "reschedule"}:
        raise ValueError("Unsupported booking notification type")
    return connection.execute(
        """
        INSERT INTO notifications (
            customer_id, booking_id, channel, type, provider,
            scheduled_for, dedupe_key
        )
        VALUES (
            %s, %s, 'email', %s, 'mock',
            CASE WHEN in_quiet_hours(statement_timestamp())
                 THEN next_allowed_send_time(statement_timestamp())
                 ELSE statement_timestamp() END,
            %s
        )
        ON CONFLICT (dedupe_key) DO NOTHING
        """,
        (
            customer_id,
            booking_id,
            event_type,
            f"{event_type}:{booking_id}",
        ),
    ).rowcount


def run_mock_notification_worker(
    connection: Connection,
    limit: int = 50,
) -> int:
    claimed = connection.execute(
        "SELECT * FROM claim_due_notifications(%s)",
        (limit,),
    ).fetchall()
    for notification in claimed:
        connection.execute(
            """
            UPDATE notifications
               SET status = 'sent',
                   sent_at = statement_timestamp(),
                   provider_message_id = %s,
                   last_error = NULL
             WHERE id = %s
            """,
            (f"mock:{notification['id']}", notification["id"]),
        )
    return len(claimed)


def mark_notification_failed(
    connection: Connection,
    notification_id: UUID,
    error_message: str,
) -> None:
    connection.execute(
        """
        UPDATE notifications
           SET status = 'failed',
               next_retry_at = statement_timestamp()
                   + make_interval(mins => LEAST(60, power(2, attempts)::integer)),
               last_error = %s
         WHERE id = %s AND status = 'queued'
        """,
        (error_message[:1000], notification_id),
    )

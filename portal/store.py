import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta

from .config import (
    DEFAULT_DATABASE_PATH,
    DEFAULT_DURATION_MINUTES,
    DEFAULT_PATIENT_EMAIL,
    DEFAULT_PATIENT_NAME,
    DEFAULT_PROVIDER_ID,
    DEFAULT_PROVIDER_NAME,
)

logger = logging.getLogger(__name__)
PROVIDER_SCREEN_LEASE_SECONDS = 20
SHARED_MEMORY_DATABASE_URI = "file:patient_portal_shared?mode=memory&cache=shared"


class ConflictError(RuntimeError):
    pass


class ProviderScreenInactiveError(RuntimeError):
    pass


def isoformat(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return value


def parse_iso_datetime(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


class AppointmentStore:
    def __init__(self, database_path=None):
        self.database_path = database_path or os.environ.get("PORTAL_DB_PATH", DEFAULT_DATABASE_PATH)
        self._shared_connection = None
        if self.database_path == ":memory:":
            self._shared_connection = sqlite3.connect(
                SHARED_MEMORY_DATABASE_URI,
                uri=True,
                check_same_thread=False,
            )
            self._shared_connection.row_factory = sqlite3.Row
            self._shared_connection.execute("PRAGMA foreign_keys = ON")
        self.init_database()
        self.seed_data()

    @contextmanager
    def connect(self):
        if self.database_path == ":memory:":
            conn = sqlite3.connect(
                SHARED_MEMORY_DATABASE_URI,
                uri=True,
                check_same_thread=False,
            )
        else:
            conn = sqlite3.connect(self.database_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def init_database(self):
        with self.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS appointments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    patient_name TEXT NOT NULL,
                    patient_email TEXT NOT NULL,
                    provider_id TEXT NOT NULL,
                    provider_name TEXT NOT NULL,
                    appointment_type TEXT NOT NULL,
                    scheduled_for TEXT NOT NULL,
                    end_time TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending','confirmed','cancelled')),
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS appointment_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    appointment_id INTEGER NOT NULL REFERENCES appointments(id) ON DELETE RESTRICT,
                    actor_role TEXT NOT NULL,
                    actor_name TEXT NOT NULL,
                    event_name TEXT NOT NULL,
                    changed_fields TEXT NOT NULL,
                    old_value TEXT,
                    new_value TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            history_foreign_keys = conn.execute("PRAGMA foreign_key_list(appointment_history)").fetchall()
            if not any(key["table"] == "appointments" and key["from"] == "appointment_id" for key in history_foreign_keys):
                conn.execute("ALTER TABLE appointment_history RENAME TO appointment_history_legacy")
                conn.execute(
                    """
                    CREATE TABLE appointment_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        appointment_id INTEGER NOT NULL REFERENCES appointments(id) ON DELETE RESTRICT,
                        actor_role TEXT NOT NULL,
                        actor_name TEXT NOT NULL,
                        event_name TEXT NOT NULL,
                        changed_fields TEXT NOT NULL,
                        old_value TEXT,
                        new_value TEXT,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO appointment_history (
                        id, appointment_id, actor_role, actor_name, event_name,
                        changed_fields, old_value, new_value, created_at
                    )
                    SELECT id, appointment_id, actor_role, actor_name, event_name,
                           changed_fields, old_value, new_value, created_at
                    FROM appointment_history_legacy
                    """
                )
                conn.execute("DROP TABLE appointment_history_legacy")
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS prevent_confirmed_appointment_overlap_insert
                BEFORE INSERT ON appointments
                WHEN NEW.status = 'confirmed'
                BEGIN
                    SELECT CASE WHEN EXISTS (
                        SELECT 1 FROM appointments existing
                        WHERE existing.provider_id = NEW.provider_id
                          AND existing.status = 'confirmed'
                          AND NEW.scheduled_for < existing.end_time
                          AND NEW.end_time > existing.scheduled_for
                    ) THEN RAISE(ABORT, 'This provider already has a confirmed appointment during that time.') END;
                END
                """
            )
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS prevent_confirmed_appointment_overlap_update
                BEFORE UPDATE OF provider_id, status, scheduled_for, end_time ON appointments
                WHEN NEW.status = 'confirmed'
                BEGIN
                    SELECT CASE WHEN EXISTS (
                        SELECT 1 FROM appointments existing
                        WHERE existing.provider_id = NEW.provider_id
                          AND existing.status = 'confirmed'
                          AND existing.id != NEW.id
                          AND NEW.scheduled_for < existing.end_time
                          AND NEW.end_time > existing.scheduled_for
                    ) THEN RAISE(ABORT, 'This provider already has a confirmed appointment during that time.') END;
                END
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS notification_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    appointment_id INTEGER NOT NULL,
                    recipient TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    message TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS provider_screen_leases (
                    provider_id TEXT PRIMARY KEY,
                    token TEXT NOT NULL,
                    last_seen REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS portal_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )

    def seed_data(self):
        with self.connect() as conn:
            seeded = conn.execute(
                "SELECT value FROM portal_metadata WHERE key = 'demo_data_seeded'"
            ).fetchone()
            if seeded:
                return
            count = conn.execute("SELECT COUNT(*) FROM appointments").fetchone()[0]
            if count:
                conn.execute(
                    "INSERT INTO portal_metadata (key, value) VALUES ('demo_data_seeded', '1')"
                )
                return

            now = datetime.now()
            demo_entries = [
                {
                    "patient_name": DEFAULT_PATIENT_NAME,
                    "patient_email": DEFAULT_PATIENT_EMAIL,
                    "provider_id": DEFAULT_PROVIDER_ID,
                    "provider_name": DEFAULT_PROVIDER_NAME,
                    "appointment_type": "Follow-up",
                    "scheduled_for": now + timedelta(days=2, hours=9),
                    "status": "confirmed",
                    "reason": "Annual checkup",
                },
                {
                    "patient_name": DEFAULT_PATIENT_NAME,
                    "patient_email": DEFAULT_PATIENT_EMAIL,
                    "provider_id": DEFAULT_PROVIDER_ID,
                    "provider_name": DEFAULT_PROVIDER_NAME,
                    "appointment_type": "Consultation",
                    "scheduled_for": now + timedelta(days=4, hours=13, minutes=30),
                    "status": "pending",
                    "reason": "Medication review",
                },
                {
                    "patient_name": DEFAULT_PATIENT_NAME,
                    "patient_email": DEFAULT_PATIENT_EMAIL,
                    "provider_id": DEFAULT_PROVIDER_ID,
                    "provider_name": DEFAULT_PROVIDER_NAME,
                    "appointment_type": "Lab review",
                    "scheduled_for": now + timedelta(days=7, hours=15),
                    "status": "confirmed",
                    "reason": "Blood work follow-up",
                },
            ]
            for appointment in demo_entries:
                self.create_appointment(
                    patient_name=appointment["patient_name"],
                    patient_email=appointment["patient_email"],
                    provider_id=appointment["provider_id"],
                    provider_name=appointment["provider_name"],
                    appointment_type=appointment["appointment_type"],
                    scheduled_for=appointment["scheduled_for"],
                    status=appointment["status"],
                    reason=appointment["reason"],
                )
            with self.connect() as conn:
                conn.execute(
                    "INSERT INTO portal_metadata (key, value) VALUES ('demo_data_seeded', '1')"
                )

    def _record_history(self, conn, appointment_id, actor_role, actor_name, event_name, changed_fields, old_value=None, new_value=None):
        conn.execute(
            """
            INSERT INTO appointment_history (
                appointment_id, actor_role, actor_name, event_name, changed_fields, old_value, new_value, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                appointment_id,
                actor_role,
                actor_name,
                event_name,
                changed_fields,
                old_value,
                new_value,
                isoformat(datetime.now()),
            ),
        )

    def _default_appointment_payload(self, appointment):
        return {
            "id": appointment["id"],
            "patient_name": appointment["patient_name"],
            "patient_email": appointment["patient_email"],
            "provider_id": appointment["provider_id"],
            "provider_name": appointment["provider_name"],
            "appointment_type": appointment["appointment_type"],
            "scheduled_for": appointment["scheduled_for"],
            "end_time": appointment["end_time"],
            "status": appointment["status"],
            "reason": appointment["reason"],
            "version": appointment["version"],
            "created_at": appointment["created_at"],
            "updated_at": appointment["updated_at"],
        }

    def _appointment_end(self, scheduled_for):
        return (parse_iso_datetime(scheduled_for) + timedelta(minutes=DEFAULT_DURATION_MINUTES)).isoformat(timespec="seconds")

    def get_appointment_by_id(self, appointment_id):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM appointments WHERE id = ?", (appointment_id,)).fetchone()
            return self._default_appointment_payload(row) if row else None

    def list_appointments(self, role):
        with self.connect() as conn:
            if role == "patient":
                rows = conn.execute(
                    "SELECT * FROM appointments WHERE patient_name = ? ORDER BY scheduled_for ASC, id ASC",
                    (DEFAULT_PATIENT_NAME,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM appointments WHERE provider_id = ? ORDER BY scheduled_for ASC, id ASC",
                    (DEFAULT_PROVIDER_ID,),
                ).fetchall()
        return [self._default_appointment_payload(row) for row in rows]

    def list_history(self, limit=12, appointment_ids=None):
        with self.connect() as conn:
            if appointment_ids is None:
                rows = conn.execute(
                    "SELECT * FROM appointment_history ORDER BY created_at DESC, id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                appointment_ids = tuple(appointment_ids)
                if not appointment_ids:
                    return []
                placeholders = ", ".join("?" for _ in appointment_ids)
                rows = conn.execute(
                    f"SELECT * FROM appointment_history WHERE appointment_id IN ({placeholders}) "
                    "ORDER BY created_at DESC, id DESC",
                    appointment_ids,
                ).fetchall()
        history = [dict(row) for row in rows]
        for item in history:
            for field in ("old_value", "new_value"):
                if item[field] is not None:
                    item[field] = json.loads(item[field])
        return history

    def list_notifications(self, limit=10):
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM notification_log ORDER BY created_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def claim_provider_screen(self, provider_id, token, force=False, lease_seconds=PROVIDER_SCREEN_LEASE_SECONDS):
        now = time.time()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT token, last_seen FROM provider_screen_leases WHERE provider_id = ?",
                (provider_id,),
            ).fetchone()
            if (
                current is not None
                and current["token"] != token
                and current["last_seen"] > now - lease_seconds
                and not force
            ):
                conn.commit()
                return False
            conn.execute(
                """
                INSERT INTO provider_screen_leases (provider_id, token, last_seen)
                VALUES (?, ?, ?)
                ON CONFLICT(provider_id) DO UPDATE SET
                    token = excluded.token,
                    last_seen = excluded.last_seen
                """,
                (provider_id, token, now),
            )
            conn.commit()
            return True

    def heartbeat_provider_screen(self, provider_id, token, lease_seconds=PROVIDER_SCREEN_LEASE_SECONDS):
        now = time.time()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE provider_screen_leases
                SET last_seen = ?
                WHERE provider_id = ? AND token = ? AND last_seen > ?
                """,
                (now, provider_id, token, now - lease_seconds),
            )
            return cursor.rowcount == 1

    def release_provider_screen(self, provider_id, token):
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM provider_screen_leases WHERE provider_id = ? AND token = ?",
                (provider_id, token),
            )

    def _owns_provider_screen(self, conn, provider_id, token, lease_seconds=PROVIDER_SCREEN_LEASE_SECONDS):
        if not token:
            return False
        row = conn.execute(
            "SELECT last_seen FROM provider_screen_leases WHERE provider_id = ? AND token = ?",
            (provider_id, token),
        ).fetchone()
        return row is not None and row["last_seen"] > time.time() - lease_seconds

    def is_provider_screen_owner(self, provider_id, token, lease_seconds=PROVIDER_SCREEN_LEASE_SECONDS):
        with self.connect() as conn:
            return self._owns_provider_screen(conn, provider_id, token, lease_seconds)

    def find_provider_conflicts(self, provider_id, scheduled_for, end_time, exclude_id=None):
        start = isoformat(scheduled_for)
        end = isoformat(end_time)
        with self.connect() as conn:
            rows = self._provider_conflict_rows(conn, provider_id, start, end, exclude_id)
        return [self._default_appointment_payload(row) for row in rows]

    def _provider_conflict_rows(self, conn, provider_id, start, end, exclude_id=None):
        query = """
            SELECT * FROM appointments
            WHERE provider_id = ?
              AND status = 'confirmed'
              AND scheduled_for < ?
              AND end_time > ?
        """
        parameters = [provider_id, end, start]
        if exclude_id is not None:
            query += " AND id != ?"
            parameters.append(exclude_id)
        query += " ORDER BY scheduled_for ASC"
        return conn.execute(query, parameters).fetchall()

    def create_appointment(self, patient_name, provider_id, provider_name, appointment_type, scheduled_for, status, reason, patient_email=DEFAULT_PATIENT_EMAIL):
        if isinstance(scheduled_for, str):
            scheduled_for = parse_iso_datetime(scheduled_for)
        scheduled_for_iso = isoformat(scheduled_for)
        end_time = self._appointment_end(scheduled_for_iso)
        now = isoformat(datetime.now())

        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if status == "confirmed":
                conflict_rows = self._provider_conflict_rows(
                    conn,
                    provider_id,
                    scheduled_for_iso,
                    end_time,
                )
                conflicts = [self._default_appointment_payload(row) for row in conflict_rows]
                if conflicts:
                    raise ConflictError("This provider already has a confirmed appointment during that time.")
            cursor = conn.execute(
                """
                INSERT INTO appointments (
                    patient_name, patient_email, provider_id, provider_name,
                    appointment_type, scheduled_for, end_time, status, reason, created_at, updated_at, version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    patient_name,
                    patient_email,
                    provider_id,
                    provider_name,
                    appointment_type,
                    scheduled_for_iso,
                    end_time,
                    status,
                    reason,
                    now,
                    now,
                ),
            )
            appointment_id = cursor.lastrowid
            self._record_history(
                conn,
                appointment_id,
                actor_role="patient" if status == "pending" else "provider",
                actor_name=patient_name,
                event_name="created",
                changed_fields="patient_name,patient_email,provider_id,provider_name,appointment_type,scheduled_for,end_time,status,reason",
                old_value=None,
                new_value=json.dumps({
                    "patient_name": patient_name,
                    "patient_email": patient_email,
                    "provider_id": provider_id,
                    "provider_name": provider_name,
                    "appointment_type": appointment_type,
                    "scheduled_for": scheduled_for_iso,
                    "end_time": end_time,
                    "status": status,
                    "reason": reason,
                }),
            )
            conn.commit()
            return self.get_appointment_by_id(appointment_id)

    def update_appointment(self, appointment_id, *, expected_version=None, status=None, scheduled_for=None, actor_role="provider", actor_name="System", reason=None, event_name="updated", provider_screen_token=None):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if provider_screen_token is not None and not self._owns_provider_screen(
                conn,
                DEFAULT_PROVIDER_ID,
                provider_screen_token,
            ):
                raise ProviderScreenInactiveError("This provider screen is no longer active. Take over the active screen to make changes.")
            row = conn.execute("SELECT * FROM appointments WHERE id = ?", (appointment_id,)).fetchone()
            if row is None:
                raise ValueError(f"Appointment {appointment_id} does not exist.")
            if expected_version is not None and row["version"] != expected_version:
                raise ConflictError(
                    f"This appointment changed while you were viewing it. Refresh and try again. "
                    f"Expected version {expected_version}, current version {row['version']}."
                )

            old_value = {
                field: row[field]
                for field in (
                    "patient_name",
                    "patient_email",
                    "provider_id",
                    "provider_name",
                    "appointment_type",
                    "scheduled_for",
                    "end_time",
                    "status",
                    "reason",
                )
            }
            old_status = row["status"]
            new_status = status or old_status
            new_time = parse_iso_datetime(scheduled_for) if scheduled_for else parse_iso_datetime(row["scheduled_for"])
            new_end = self._appointment_end(new_time)
            new_value = {
                **old_value,
                "scheduled_for": isoformat(new_time),
                "end_time": new_end,
                "status": new_status,
            }

            if new_status == "cancelled" and old_status != "confirmed":
                raise ValueError("Only confirmed appointments can be cancelled.")

            if new_status == "confirmed":
                conflict_rows = self._provider_conflict_rows(
                    conn,
                    row["provider_id"],
                    isoformat(new_time),
                    new_end,
                    exclude_id=appointment_id,
                )
                conflicts = [self._default_appointment_payload(conflict) for conflict in conflict_rows]
                if conflicts:
                    raise ConflictError("This provider already has a confirmed appointment during that time.")

            new_version = row["version"] + 1
            conn.execute(
                """
                UPDATE appointments
                SET status = ?, scheduled_for = ?, end_time = ?, updated_at = ?, version = ?
                WHERE id = ?
                """,
                (
                    new_status,
                    isoformat(new_time),
                    new_end,
                    isoformat(datetime.now()),
                    new_version,
                    appointment_id,
                ),
            )

            self._record_history(
                conn,
                appointment_id,
                actor_role=actor_role,
                actor_name=actor_name,
                event_name=event_name,
                changed_fields=",".join(
                    field for field in new_value if old_value[field] != new_value[field]
                ) or "no field changes",
                old_value=json.dumps(old_value),
                new_value=json.dumps(new_value),
            )
            conn.commit()

            if new_value != old_value:
                appointment = self.get_appointment_by_id(appointment_id)
                try:
                    self.send_notification_async(appointment, event_name)
                except Exception:
                    logger.exception("Unable to start %s notification for appointment %s", event_name, appointment_id)

                return appointment

            return self.get_appointment_by_id(appointment_id)

    def send_notification_async(self, appointment, event_name="confirmed"):
        if not appointment:
            return

        thread = threading.Thread(
            target=self._send_notification,
            args=(appointment, event_name),
            daemon=True,
        )
        thread.start()

    def _send_notification(self, appointment, event_name="confirmed"):
        appointment_id = appointment["id"]
        scheduled_for = parse_iso_datetime(appointment["scheduled_for"])
        action = {
            "confirmed": "confirmation",
            "rescheduled": "reschedule",
            "cancelled": "cancellation",
        }.get(event_name, "update")
        appointment_details = "\n".join((
            f"Appointment ID: {appointment_id}",
            f"Date: {scheduled_for.strftime('%A, %B %d, %Y')}",
            f"Time: {scheduled_for.strftime('%I:%M %p')}",
            f"Type: {appointment['appointment_type']}",
            f"Reason: {appointment['reason']}",
            f"Status: {appointment['status']}",
        ))
        try:
            message = "\n".join((
                f"Would send appointment {action} email to {appointment['patient_email']} for {appointment['provider_name']}.",
                appointment_details,
            ))
            now = isoformat(datetime.now())
            with self.connect() as conn:
                conn.execute(
                    "INSERT INTO notification_log (appointment_id, recipient, channel, message, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (appointment_id, appointment["patient_email"], "email", message, "queued", now),
                )
                conn.commit()
        except Exception:
            logger.exception("Unable to log %s notification for appointment %s", action, appointment_id)
            with self.connect() as conn:
                conn.execute(
                    "INSERT INTO notification_log (appointment_id, recipient, channel, message, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        appointment_id,
                        appointment["patient_email"],
                        "email",
                        f"Appointment {action} notification failed.\n{appointment_details}",
                        "error",
                        isoformat(datetime.now()),
                    ),
                )
                conn.commit()
from datetime import datetime

from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for
from flask_socketio import emit

from .config import (
    DEFAULT_PATIENT_EMAIL,
    DEFAULT_PATIENT_NAME,
    DEFAULT_PROVIDER_ID,
    DEFAULT_PROVIDER_NAME,
)
from .store import (
    PROVIDER_SCREEN_LEASE_SECONDS,
    ConflictError,
    ProviderScreenInactiveError,
)

portal = Blueprint("portal", __name__)


def get_store():
    return current_app.extensions["appointment_store"]


def emit_appointment_change(message, appointment_id=None, appointment=None):
    payload = {
        "message": message,
        "appointment_id": appointment_id,
        "appointment": appointment,
    }
    current_app.extensions["socketio"].emit("appointment_changed", payload, namespace="/")


@portal.before_app_request
def ensure_role():
    if "role" not in session:
        session["role"] = "patient"


@portal.route("/api/appointments")
def api_appointments():
    role = session.get("role", "patient")
    appointments = get_store().list_appointments(role)
    latest_updated_at = max((appointment["updated_at"] for appointment in appointments), default="")
    return {
        "role": role,
        "appointments": appointments,
        "latest_updated_at": latest_updated_at,
    }


def provider_screen_request_token():
    payload = request.get_json(silent=True) or {}
    return payload.get("token") or request.form.get("provider_screen_token")


@portal.route("/api/provider-screen/claim", methods=["POST"])
def claim_provider_screen():
    if session.get("role") != "provider":
        return {"error": "Provider role required."}, 403
    token = provider_screen_request_token()
    if not token:
        return {"error": "Provider screen token is required."}, 400
    payload = request.get_json(silent=True) or {}
    active = get_store().claim_provider_screen(
        DEFAULT_PROVIDER_ID,
        token,
        force=bool(payload.get("takeover")),
    )
    return {"active": active, "lease_seconds": PROVIDER_SCREEN_LEASE_SECONDS}


@portal.route("/api/provider-screen/heartbeat", methods=["POST"])
def heartbeat_provider_screen():
    if session.get("role") != "provider":
        return {"active": False}, 403
    token = provider_screen_request_token()
    active = bool(token) and get_store().heartbeat_provider_screen(DEFAULT_PROVIDER_ID, token)
    return {"active": active}


@portal.route("/api/provider-screen/release", methods=["POST"])
def release_provider_screen():
    if session.get("role") != "provider":
        return {"released": False}, 403
    token = provider_screen_request_token()
    if token:
        get_store().release_provider_screen(DEFAULT_PROVIDER_ID, token)
    return {"released": bool(token)}


@portal.route("/")
def index():
    role = session.get("role", "patient")
    store = get_store()
    appointments = store.list_appointments(role)
    history = store.list_history(appointment_ids=(item["id"] for item in appointments))
    history_by_appointment = {}
    for item in history:
        history_by_appointment.setdefault(item["appointment_id"], []).append(item)
    notifications = store.list_notifications(limit=50)
    latest_updated_at = max((appointment["updated_at"] for appointment in appointments), default="")
    return render_template(
        "index.html",
        appointments=appointments,
        role=role,
        history=history,
        history_by_appointment=history_by_appointment,
        notifications=notifications,
        patient_name=DEFAULT_PATIENT_NAME,
        provider_name=DEFAULT_PROVIDER_NAME,
        latest_updated_at=latest_updated_at,
    )


@portal.route("/set-role/<role>", methods=["POST"])
def set_role(role):
    if role in {"patient", "provider"}:
        session["role"] = role
        flash(f"Switched to {role} view.")
    return redirect(url_for("portal.index"))


@portal.route("/appointments/request", methods=["POST"])
def request_appointment():
    date_value = request.form.get("appointment_date")
    time_value = request.form.get("appointment_time")
    if not date_value or not time_value:
        flash("Pick both a date and time for the appointment.")
        return redirect(url_for("portal.index"))
    try:
        scheduled = datetime.strptime(f"{date_value} {time_value}", "%Y-%m-%d %H:%M")
        appointment = get_store().create_appointment(
            patient_name=DEFAULT_PATIENT_NAME,
            patient_email=DEFAULT_PATIENT_EMAIL,
            provider_id=DEFAULT_PROVIDER_ID,
            provider_name=DEFAULT_PROVIDER_NAME,
            appointment_type=request.form.get("appointment_type", "Visit"),
            scheduled_for=scheduled,
            status="pending",
            reason=request.form.get("reason", "Requested appointment"),
        )
        emit_appointment_change(
            message="Appointment record changed. The latest booking is now visible.",
            appointment_id=appointment["id"],
            appointment=appointment,
        )
        flash("Appointment request submitted and queued.")
    except ConflictError as exc:
        flash(str(exc))
    except ValueError as exc:
        flash(f"Invalid date or time: {exc}")
    return redirect(url_for("portal.index"))


@portal.route("/appointments/<int:appointment_id>/cancel", methods=["POST"])
def cancel_appointment(appointment_id):
    expected_version = request.form.get("expected_version")
    try:
        appointment = get_store().update_appointment(
            appointment_id,
            expected_version=int(expected_version) if expected_version else None,
            status="cancelled",
            actor_role="patient",
            actor_name=DEFAULT_PATIENT_NAME,
            reason="Patient cancelled appointment",
            event_name="cancelled",
        )
        emit_appointment_change(
            message="Record changed: this appointment was cancelled.",
            appointment_id=appointment_id,
            appointment=appointment,
        )
        flash("Appointment was cancelled.")
    except ConflictError as exc:
        flash(str(exc))
    except ValueError as exc:
        flash(str(exc))
    return redirect(url_for("portal.index"))


@portal.route("/appointments/<int:appointment_id>/confirm", methods=["POST"])
def confirm_appointment(appointment_id):
    expected_version = request.form.get("expected_version")
    provider_screen_token = request.form.get("provider_screen_token", "")
    try:
        appointment = get_store().update_appointment(
            appointment_id,
            expected_version=int(expected_version) if expected_version else None,
            status="confirmed",
            actor_role="provider",
            actor_name=DEFAULT_PROVIDER_NAME,
            reason="Provider confirmed appointment",
            event_name="confirmed",
            provider_screen_token=provider_screen_token,
        )
        emit_appointment_change(
            message="Record changed: this appointment was confirmed.",
            appointment_id=appointment_id,
            appointment=appointment,
        )
        flash("Appointment confirmed successfully.")
    except ConflictError as exc:
        flash(str(exc))
    except ProviderScreenInactiveError as exc:
        flash(str(exc))
    except ValueError as exc:
        flash(str(exc))
    return redirect(url_for("portal.index"))


@portal.route("/appointments/<int:appointment_id>/reschedule", methods=["POST"])
def reschedule_appointment(appointment_id):
    date_value = request.form.get("appointment_date")
    time_value = request.form.get("appointment_time")
    expected_version = request.form.get("expected_version")
    provider_screen_token = request.form.get("provider_screen_token", "")
    if not date_value or not time_value:
        flash("Choose a new date and time before rescheduling.")
        return redirect(url_for("portal.index"))
    try:
        scheduled = datetime.strptime(f"{date_value} {time_value}", "%Y-%m-%d %H:%M")
        appointment = get_store().update_appointment(
            appointment_id,
            expected_version=int(expected_version) if expected_version else None,
            scheduled_for=scheduled,
            actor_role="provider",
            actor_name=DEFAULT_PROVIDER_NAME,
            reason="Provider rescheduled appointment",
            event_name="rescheduled",
            provider_screen_token=provider_screen_token,
        )
        emit_appointment_change(
            message="Record changed: the appointment time was updated.",
            appointment_id=appointment_id,
            appointment=appointment,
        )
        flash("Appointment rescheduled.")
    except ConflictError as exc:
        flash(str(exc))
    except ProviderScreenInactiveError as exc:
        flash(str(exc))
    except ValueError as exc:
        flash(str(exc))
    return redirect(url_for("portal.index"))


def register_socket_handlers(socketio):
    @socketio.on("connect")
    def on_connect():
        emit("connection_ready", {"status": "connected"})
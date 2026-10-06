from portal import create_app
from portal.config import (
    DEFAULT_DATABASE_PATH,
    DEFAULT_DURATION_MINUTES,
    DEFAULT_PATIENT_EMAIL,
    DEFAULT_PATIENT_NAME,
    DEFAULT_PROVIDER_ID,
    DEFAULT_PROVIDER_NAME,
)
from portal.store import AppointmentStore, ConflictError, isoformat, parse_iso_datetime

__all__ = [
    "AppointmentStore",
    "ConflictError",
    "DEFAULT_DATABASE_PATH",
    "DEFAULT_DURATION_MINUTES",
    "DEFAULT_PATIENT_EMAIL",
    "DEFAULT_PATIENT_NAME",
    "DEFAULT_PROVIDER_ID",
    "DEFAULT_PROVIDER_NAME",
    "create_app",
    "isoformat",
    "parse_iso_datetime",
]


if __name__ == "__main__":
    app = create_app()
    app.socketio.run(app, debug=True, host="0.0.0.0", port=5000)

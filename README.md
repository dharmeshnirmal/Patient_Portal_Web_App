# Patient Portal Web App

A lightweight patient portal demo built in Python with Flask and SQLite. It supports a patient and provider role switcher, appointment requests, cancellations, confirmations, conflict prevention, audit history, and notification stubs.

## Setup guide

### Prerequisites

- Python 3.10+ recommended
- Git (optional, for cloning)
- A local terminal

### Windows setup

```powershell
cd C:\path\to\Patient_Portal_Web_App
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

### macOS / Linux setup

```bash
cd /path/to/Patient_Portal_Web_App
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

### Open the app

Visit:

```text
http://localhost:5000
```

The app stores appointment data in a local SQLite database file named `portal.db` and persists it across page refreshes.

## Verified flow

The following core flow has been checked locally:

1. The app loads successfully on the patient view.
2. A patient can create a new pending appointment request.
3. The request shows up in the appointment list with `pending` status.
4. The provider can switch to the provider view.
5. The provider can confirm a pending appointment.
6. The appointment status changes to `confirmed` without blocking the UI.
7. The app records audit trail entries and notification events.
8. The provider cannot schedule overlapping confirmed appointments.

## User guide

### Patient flow

1. Open the app in the browser.
2. By default, you are on the patient view.
3. Fill out the appointment form:
   - date
   - time
   - appointment type
   - reason for visit
4. Click the `Submit request` button.
5. The new appointment appears in the list with `pending` status.
6. A confirmed appointment can be cancelled from the patient view.
7. A pending appointment is not cancelable.

### Provider flow

1. Click the `Provider` role button in the header.
2. The provider list shows all appointments assigned to that provider.
3. For a pending request, click `Confirm` to move it to `confirmed`.
4. Use the `Reschedule` control to change the date and time.
5. Expand `Change history` under an appointment to review its audit timeline.
6. The app prevents overlapping confirmed slots for the same provider.

### Assignment problem handling built in

This app includes support for the common edge cases in the assignment:

- stale view conflicts: the app rejects stale versions using optimistic concurrency checks
- history tracking: each change is stored with the previous and current values
- notification stub: confirmation logs a queued email notification without slowing the main confirm flow
- provider overlap prevention: a confirmed appointment cannot overlap a second confirmed slot for the same provider

## Problem statements and solutions

### Problem 1: stale record updates and live cross-screen visibility

Problem statement:

When two users or two browser screens are viewing the same appointment record, one user may perform an update while the other user still has an older version loaded. In that situation, the system must not silently overwrite newer data. It should detect that the record has changed and reject stale updates. At the same time, when the record is changed on one screen, the other open screens should automatically reflect the new value and show a message that the record was updated.

How it is solved in this application:

1. The appointment table stores a `version` field.
2. Every update is validated using optimistic concurrency: the client sends the expected version number in a hidden form field.
3. The server compares the submitted version with the current database version before saving.
4. If they differ, the update is rejected with a clear conflict message instead of overwriting fresh data.
5. After a valid update, the server emits a real-time `appointment_changed` event using Flask-SocketIO.
6. All open browser tabs connected to the app listen for that event and display a visible banner such as: “Record changed: this appointment was confirmed. Reloading the latest record...”
7. The client then refreshes automatically so the newest appointment values are visible on the other screen without requiring manual refresh.

This satisfies both parts of the problem: safe stale-version protection and automatic live refresh for other users.

### Problem 2: appointment change history and auditability

Problem statement:

If an appointment is on Wednesday but the patient remembers booking Monday, the application must make it possible to determine when the appointment changed, who changed it, and what its values were before and after the change. Confirming, rescheduling, or cancelling must not erase the previous state.

How it is solved in this application:

1. Each appointment has a corresponding set of rows in `appointment_history`, linked through `appointment_id` to the appointment record. The database enforces this relationship with a foreign key, and application startup migrates existing history rows while preserving them.
2. Appointment creation and every successful confirmation, reschedule, or cancellation append an audit event. The appointment update and its history event are written in the same SQLite transaction, so a rejected or failed update does not leave a misleading audit entry.
3. Each event records the action, changed field names, old values, new values, actor name and role, and the change timestamp. The appointment row remains the current state; audit rows preserve the sequence of prior states.
4. On the patient and provider screens, each appointment has an expandable `Change history` section. It displays the action, actor, timestamp, changed fields, and before/after values for that appointment.
5. History is queried for appointments visible to the current role and grouped by appointment, so users can inspect the relevant record's history rather than relying on a detached global list.

The current appointment row, its `version`, and its audit history are complementary parts of the same model: the row stores the latest values, the version protects against stale edits, and the linked append-only history explains how the record reached its current state. In this demo, actor names represent the selected patient/provider role; a production deployment should use authenticated user IDs as the authoritative actor identity.

### Problem 3: non-blocking appointment notifications

Problem statement:

When a provider confirms an appointment, the patient should be notified by email or text. Confirmation must still succeed if notification delivery is slow or unavailable.

How it is solved in this application:

 1. A confirmation, reschedule, or cancellation updates the appointment and commits before notification work begins. No-op updates do not create duplicate notification entries.
 2. The app passes the saved appointment snapshot and action to a background thread, which writes a notification stub to `notification_log`; it does not contact a real email or SMS service.
 3. Each message identifies the action, recipient, appointment ID, date, time, type, reason, and status. The Notifications panel shows separate entries with appointment IDs, status, and timestamps, so repeated changes to one appointment remain distinguishable.
 4. Notification dispatch is isolated from the appointment update result. If starting the worker fails, the app logs the error and keeps the successful appointment change committed.

This lightweight background-thread stub is suitable for the assignment because it demonstrates the separation between the user-facing confirmation and notification work without requiring external services. For real traffic, I would use a durable job queue and transactional outbox, with retries and backoff, idempotency, delivery-status tracking, monitoring, and appropriate email/SMS consent and provider integrations. A process-local daemon thread is not durable across restarts and is not a production queue.

### Problem 4: preventing overlapping confirmed appointments

Problem statement:

A provider cannot attend two appointments at once. Two confirmed appointments for the same provider must never overlap, including when two pending requests are confirmed at nearly the same time.

How it is solved in this application:

The provider view permits only one active write-enabled screen per provider. A database-backed 20-second lease is renewed by the active tab. When another provider screen opens, it asks whether to take over or leave the existing screen active. Taking over invalidates the old screen's token; the old tab becomes read-only on its next heartbeat, and the server rejects its provider updates even before the UI refreshes. If a tab closes without releasing its lease, another screen can claim it after the lease expires. This screen-ownership policy complements, but does not replace, the database overlap invariant above.

Also at at persistence layer following change done to make sure at backend layer.

1. The rule is enforced in the persistence layer, not in the page or button visibility. The appointment store starts a SQLite `BEGIN IMMEDIATE` transaction before reading or changing an appointment.
2. Inside that same transaction and on the same database connection, the store checks whether another confirmed appointment for that provider overlaps the requested interval. It runs this check whenever an appointment will be confirmed, including when an already-confirmed appointment is rescheduled.
3. SQLite triggers on appointment inserts and updates provide a database-level guard as well. Even a write that bypasses the Flask routes or store check is rejected if it would create an overlap.
4. Two simultaneous confirmations are serialized by SQLite's write lock. The first valid confirmation commits. The second then checks against the committed state and receives a conflict instead of creating a second overlapping confirmation.
5. Intervals use the standard overlap condition `new_start < existing_end` and `new_end > existing_start`, so appointments that meet exactly at an endpoint are allowed while appointments sharing any time are rejected.

The store check provides a clear application conflict response, while the database triggers protect the invariant across every write path. Both enforce the same rule at the data boundary; the UI is only a convenience and is not trusted for correctness.



## Notes

This is intentionally a simple but persistent demo. It is meant to be easy to run locally while still demonstrating the logic behind the assignment problems.

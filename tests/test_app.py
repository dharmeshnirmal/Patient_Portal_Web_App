from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import re
import sqlite3
from threading import Barrier

from app import create_app, AppointmentStore, ConflictError


def test_provider_overlap_detection():
    app = create_app(test_config={'TESTING': True, 'DATABASE_PATH': ':memory:'})
    with app.app_context():
        store = AppointmentStore()
        base = datetime(2026, 10, 10, 9, 0)
        store.create_appointment(
            patient_name='Alice',
            provider_id='doc-1',
            provider_name='Dr. Smith',
            appointment_type='Follow-up',
            scheduled_for=base,
            status='confirmed',
            reason='Check-in',
        )
        conflict = store.find_provider_conflicts('doc-1', base + timedelta(minutes=15), base + timedelta(minutes=45), exclude_id=None)
        assert len(conflict) == 1


def test_store_connection_closes_when_context_exits(tmp_path):
    app = create_app(test_config={
        'TESTING': True,
        'DATABASE_PATH': str(tmp_path / 'connection-close.db'),
    })
    store = app.extensions['appointment_store']
    with store.connect() as connection:
        connection.execute('SELECT 1')

    try:
        connection.execute('SELECT 1')
    except sqlite3.ProgrammingError:
        return
    raise AssertionError('Expected database connection to close when its context exits')


def test_cleared_database_does_not_reseed_demo_records_on_restart(tmp_path):
    database_path = str(tmp_path / 'cleared-database.db')
    app = create_app(test_config={'TESTING': True, 'DATABASE_PATH': database_path})
    store = app.extensions['appointment_store']
    with store.connect() as conn:
        conn.execute('DELETE FROM appointment_history')
        conn.execute('DELETE FROM notification_log')
        conn.execute('DELETE FROM appointments')

    restarted_store = AppointmentStore(database_path)

    assert restarted_store.list_appointments('patient') == []
    assert restarted_store.list_history(appointment_ids=[]) == []
    assert restarted_store.list_notifications() == []


def test_rescheduling_confirmed_appointment_into_busy_slot_is_rejected(tmp_path):
    app = create_app(test_config={
        'TESTING': True,
        'DATABASE_PATH': str(tmp_path / 'reschedule-conflict.db'),
    })
    store = app.extensions['appointment_store']
    base = datetime(2026, 10, 10, 9, 0)
    first = store.create_appointment(
        patient_name='Alice',
        provider_id='doc-1',
        provider_name='Dr. Smith',
        appointment_type='Visit',
        scheduled_for=base,
        status='confirmed',
        reason='First visit',
    )
    second = store.create_appointment(
        patient_name='Bob',
        provider_id='doc-1',
        provider_name='Dr. Smith',
        appointment_type='Visit',
        scheduled_for=base + timedelta(hours=1),
        status='confirmed',
        reason='Second visit',
    )

    try:
        store.update_appointment(
            second['id'],
            expected_version=second['version'],
            scheduled_for=base,
        )
    except ConflictError:
        pass
    else:
        raise AssertionError('Expected rescheduling into an occupied slot to be rejected')

    assert store.get_appointment_by_id(first['id'])['scheduled_for'] == base.isoformat(timespec='seconds')
    assert store.get_appointment_by_id(second['id'])['scheduled_for'] == (base + timedelta(hours=1)).isoformat(timespec='seconds')


def test_simultaneous_confirms_allow_only_one_overlapping_appointment(tmp_path, monkeypatch):
    app = create_app(test_config={
        'TESTING': True,
        'DATABASE_PATH': str(tmp_path / 'concurrent-confirms.db'),
    })
    store = app.extensions['appointment_store']
    monkeypatch.setattr(store, 'send_notification_async', lambda _appointment, _event_name: None)
    scheduled_for = datetime(2026, 10, 15, 11, 0)
    appointments = [
        store.create_appointment(
            patient_name=name,
            provider_id='doc-1',
            provider_name='Dr. Smith',
            appointment_type='Visit',
            scheduled_for=scheduled_for,
            status='pending',
            reason='Concurrent confirmation test',
        )
        for name in ('Alice', 'Bob')
    ]
    start_together = Barrier(2)

    def confirm(appointment):
        start_together.wait()
        try:
            store.update_appointment(
                appointment['id'],
                expected_version=appointment['version'],
                status='confirmed',
            )
            return True
        except ConflictError:
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(confirm, appointments))

    assert sorted(results) == [False, True]
    with store.connect() as conn:
        confirmed_count = conn.execute(
            "SELECT COUNT(*) FROM appointments WHERE provider_id = ? AND status = 'confirmed' AND scheduled_for = ?",
            ('doc-1', scheduled_for.isoformat(timespec='seconds')),
        ).fetchone()[0]
    assert confirmed_count == 1


def test_database_rejects_direct_overlapping_confirmed_insert(tmp_path):
    app = create_app(test_config={
        'TESTING': True,
        'DATABASE_PATH': str(tmp_path / 'database-overlap-guard.db'),
    })
    store = app.extensions['appointment_store']
    scheduled_for = datetime(2026, 10, 18, 14, 0)
    store.create_appointment(
        patient_name='Alice',
        provider_id='doc-1',
        provider_name='Dr. Smith',
        appointment_type='Visit',
        scheduled_for=scheduled_for,
        status='confirmed',
        reason='Existing visit',
    )

    with store.connect() as conn:
        try:
            conn.execute(
                """
                INSERT INTO appointments (
                    patient_name, patient_email, provider_id, provider_name,
                    appointment_type, scheduled_for, end_time, status, reason,
                    created_at, updated_at, version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    'Bob', 'bob@example.com', 'doc-1', 'Dr. Smith', 'Visit',
                    scheduled_for.isoformat(timespec='seconds'),
                    (scheduled_for + timedelta(minutes=30)).isoformat(timespec='seconds'),
                    'confirmed', 'Conflicting visit', '2026-10-01T09:00:00',
                    '2026-10-01T09:00:00', 1,
                ),
            )
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError('Expected the database trigger to reject an overlapping insert')

    pending = store.create_appointment(
        patient_name='Bob',
        provider_id='doc-1',
        provider_name='Dr. Smith',
        appointment_type='Visit',
        scheduled_for=scheduled_for,
        status='pending',
        reason='Pending conflicting visit',
    )
    with store.connect() as conn:
        try:
            conn.execute(
                "UPDATE appointments SET status = 'confirmed' WHERE id = ?",
                (pending['id'],),
            )
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError('Expected the database trigger to reject an overlapping update')


def test_stale_version_is_rejected(monkeypatch):
    app = create_app(test_config={'TESTING': True, 'DATABASE_PATH': ':memory:'})
    with app.app_context():
        store = AppointmentStore()
        monkeypatch.setattr(store, 'send_notification_async', lambda _appointment, _event_name: None)
        appt = store.create_appointment(
            patient_name='Alice',
            provider_id='doc-1',
            provider_name='Dr. Smith',
            appointment_type='Follow-up',
            scheduled_for=datetime(2026, 10, 12, 15, 0),
            status='pending',
            reason='Check-in',
        )
        store.update_appointment(appt['id'], status='confirmed', expected_version=appt['version'])
        try:
            store.update_appointment(appt['id'], status='cancelled', expected_version=appt['version'])
        except ConflictError:
            return
        raise AssertionError('Expected stale version conflict to be raised')


def test_socketio_emits_record_change_event():
    app = create_app(test_config={'TESTING': True, 'DATABASE_PATH': ':memory:'})
    client = app.socketio.test_client(app)
    app.socketio.emit('appointment_changed', {'message': 'Record changed'}, namespace='/')
    received = client.get_received('/')
    event = next(item for item in received if item['name'] == 'appointment_changed')
    assert event['args'][0]['message'] == 'Record changed'


def test_patient_sees_provider_update_through_routes(tmp_path):
    app = create_app(test_config={
        'TESTING': True,
        'DATABASE_PATH': str(tmp_path / 'route-test.db'),
    })
    patient = app.test_client()
    provider = app.test_client()
    with patient.session_transaction() as session:
        session['role'] = 'patient'
    with provider.session_transaction() as session:
        session['role'] = 'provider'
    provider_token = 'route-test-provider-screen'
    assert provider.post('/api/provider-screen/claim', json={'token': provider_token}).get_json()['active']

    assert patient.get('/').status_code == 200
    response = patient.post('/appointments/request', data={
        'appointment_date': '2026-12-18',
        'appointment_time': '13:45',
        'appointment_type': 'Follow-up',
        'reason': 'Route refactor regression check',
    })
    assert response.status_code == 302

    patient_appointments = patient.get('/api/appointments').get_json()['appointments']
    appointment = next(item for item in patient_appointments if item['reason'] == 'Route refactor regression check')
    response = provider.post(
        f"/appointments/{appointment['id']}/confirm",
        data={
            'expected_version': str(appointment['version']),
            'provider_screen_token': provider_token,
        },
    )
    assert response.status_code == 302

    updated_appointments = patient.get('/api/appointments').get_json()['appointments']
    updated_appointment = next(item for item in updated_appointments if item['id'] == appointment['id'])
    assert updated_appointment['status'] == 'confirmed'
    scheduled_times = [item['scheduled_for'] for item in updated_appointments]
    assert scheduled_times == sorted(scheduled_times)


def test_appointment_history_shows_before_after_actor_and_time(tmp_path):
    app = create_app(test_config={
        'TESTING': True,
        'DATABASE_PATH': str(tmp_path / 'audit-test.db'),
    })
    patient = app.test_client()
    provider = app.test_client()
    with patient.session_transaction() as session:
        session['role'] = 'patient'
    with provider.session_transaction() as session:
        session['role'] = 'provider'
    provider_token = 'audit-test-provider-screen'
    assert provider.post('/api/provider-screen/claim', json={'token': provider_token}).get_json()['active']

    appointment = next(
        item for item in app.extensions['appointment_store'].list_appointments('patient')
        if item['status'] == 'pending'
    )
    response = provider.post(
        f"/appointments/{appointment['id']}/confirm",
        data={
            'expected_version': str(appointment['version']),
            'provider_screen_token': provider_token,
        },
    )
    assert response.status_code == 302

    page = patient.get('/').get_data(as_text=True)
    history_match = re.search(
        rf'<details class="appointment-history" data-appointment-id="{appointment["id"]}">(.*?)</details>',
        page,
        re.DOTALL,
    )
    assert history_match is not None
    history_html = history_match.group(1)
    assert 'Before' in history_html
    assert 'After' in history_html
    assert 'pending' in history_html
    assert 'confirmed' in history_html
    assert 'Dr. Maya Chen' in history_html
    assert 'datetime=' in history_html

    store = app.extensions['appointment_store']
    with store.connect() as conn:
        foreign_keys = conn.execute('PRAGMA foreign_key_list(appointment_history)').fetchall()
    assert any(row['table'] == 'appointments' and row['from'] == 'appointment_id' for row in foreign_keys)


def test_existing_history_is_migrated_without_losing_events(tmp_path):
    database_path = tmp_path / 'legacy-audit.db'
    with sqlite3.connect(database_path) as conn:
        conn.execute('''
            CREATE TABLE appointments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                patient_name TEXT NOT NULL,
                patient_email TEXT NOT NULL,
                provider_id TEXT NOT NULL,
                provider_name TEXT NOT NULL,
                appointment_type TEXT NOT NULL,
                scheduled_for TEXT NOT NULL,
                end_time TEXT NOT NULL,
                status TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1
            )
        ''')
        conn.execute('''
            CREATE TABLE appointment_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                appointment_id INTEGER NOT NULL,
                actor_role TEXT NOT NULL,
                actor_name TEXT NOT NULL,
                event_name TEXT NOT NULL,
                changed_fields TEXT NOT NULL,
                old_value TEXT,
                new_value TEXT,
                created_at TEXT NOT NULL
            )
        ''')
        conn.execute('''
            INSERT INTO appointments (
                id, patient_name, patient_email, provider_id, provider_name,
                appointment_type, scheduled_for, end_time, status, reason,
                created_at, updated_at, version
            ) VALUES (1, 'Patient', 'patient@example.com', 'provider-1', 'Provider',
                      'Visit', '2026-10-10T09:00:00', '2026-10-10T09:30:00', 'pending',
                      'Review', '2026-10-01T09:00:00', '2026-10-01T09:00:00', 1)
        ''')
        conn.execute('''
            INSERT INTO appointment_history (
                appointment_id, actor_role, actor_name, event_name, changed_fields,
                old_value, new_value, created_at
            ) VALUES (1, 'patient', 'Patient', 'created', 'status,schedule', NULL,
                      '{"status": "pending"}', '2026-10-01T09:00:00')
        ''')

    app = create_app(test_config={
        'TESTING': True,
        'DATABASE_PATH': str(database_path),
    })
    store = app.extensions['appointment_store']
    history = store.list_history(appointment_ids=[1])
    assert len(history) == 1
    assert history[0]['event_name'] == 'created'
    assert history[0]['new_value'] == {'status': 'pending'}
    with store.connect() as conn:
        foreign_keys = conn.execute('PRAGMA foreign_key_list(appointment_history)').fetchall()
        assert conn.execute('PRAGMA foreign_keys').fetchone()[0] == 1
    assert any(row['table'] == 'appointments' and row['from'] == 'appointment_id' for row in foreign_keys)


def test_notification_shows_appointment_details(tmp_path):
    app = create_app(test_config={
        'TESTING': True,
        'DATABASE_PATH': str(tmp_path / 'notification-test.db'),
    })
    store = app.extensions['appointment_store']
    appointment = next(
        item for item in store.list_appointments('patient')
        if item['status'] == 'pending'
    )
    store._send_notification(appointment)

    notification = store.list_notifications(limit=1)[0]
    scheduled_for = datetime.fromisoformat(appointment['scheduled_for'])
    assert f"Date: {scheduled_for.strftime('%A, %B %d, %Y')}" in notification['message']
    assert f"Time: {scheduled_for.strftime('%I:%M %p')}" in notification['message']
    assert f"Type: {appointment['appointment_type']}" in notification['message']
    assert f"Reason: {appointment['reason']}" in notification['message']

    page = app.test_client().get('/').get_data(as_text=True)
    assert 'notification-message' in page
    assert appointment['reason'] in page


def test_each_meaningful_update_adds_notification_for_same_appointment(tmp_path, monkeypatch):
    app = create_app(test_config={
        'TESTING': True,
        'DATABASE_PATH': str(tmp_path / 'repeated-notifications.db'),
    })
    store = app.extensions['appointment_store']
    monkeypatch.setattr(
        store,
        'send_notification_async',
        lambda appointment, event_name: store._send_notification(appointment, event_name),
    )
    appointment = store.create_appointment(
        patient_name='Alice',
        provider_id='doc-1',
        provider_name='Dr. Smith',
        appointment_type='Review',
        scheduled_for=datetime(2026, 12, 20, 14, 25),
        status='pending',
        reason='Repeated notification test',
    )

    confirmed = store.update_appointment(
        appointment['id'],
        expected_version=appointment['version'],
        status='confirmed',
        event_name='confirmed',
    )
    store.update_appointment(
        appointment['id'],
        expected_version=confirmed['version'],
        scheduled_for=datetime(2026, 12, 21, 15, 30),
        event_name='rescheduled',
    )

    notifications = [
        item for item in store.list_notifications(limit=20)
        if item['appointment_id'] == appointment['id']
    ]
    assert len(notifications) == 2
    assert any('confirmation' in item['message'].lower() for item in notifications)
    assert any('reschedule' in item['message'].lower() for item in notifications)
    page = app.test_client().get('/').get_data(as_text=True)
    assert page.count(f"Appointment #{appointment['id']}") >= 2


def test_confirmation_succeeds_if_notification_dispatch_fails(tmp_path, monkeypatch):
    app = create_app(test_config={
        'TESTING': True,
        'DATABASE_PATH': str(tmp_path / 'notification-failure-test.db'),
    })
    store = app.extensions['appointment_store']
    appointment = next(
        item for item in store.list_appointments('patient')
        if item['status'] == 'pending'
    )

    def fail_to_dispatch(_appointment, _event_name):
        raise RuntimeError('Notification worker is unavailable')

    monkeypatch.setattr(store, 'send_notification_async', fail_to_dispatch)
    confirmed = store.update_appointment(
        appointment['id'],
        expected_version=appointment['version'],
        status='confirmed',
    )

    assert confirmed['status'] == 'confirmed'


def test_second_provider_screen_can_take_over_and_old_screen_cannot_write(tmp_path):
    app = create_app(test_config={
        'TESTING': True,
        'DATABASE_PATH': str(tmp_path / 'provider-screen-takeover.db'),
    })
    first_screen = app.test_client()
    second_screen = app.test_client()
    for client in (first_screen, second_screen):
        with client.session_transaction() as session:
            session['role'] = 'provider'

    initial_provider_page = first_screen.get('/').get_data(as_text=True)
    assert 'provider-screen-dialog' in initial_provider_page
    assert 'disabled>Confirm' in initial_provider_page

    first_token = 'provider-screen-first'
    second_token = 'provider-screen-second'
    assert first_screen.post('/api/provider-screen/claim', json={'token': first_token}).get_json()['active']
    assert not second_screen.post('/api/provider-screen/claim', json={'token': second_token}).get_json()['active']
    assert second_screen.post(
        '/api/provider-screen/claim',
        json={'token': second_token, 'takeover': True},
    ).get_json()['active']
    assert not first_screen.post(
        '/api/provider-screen/heartbeat',
        json={'token': first_token},
    ).get_json()['active']

    store = app.extensions['appointment_store']
    appointment = next(
        item for item in store.list_appointments('provider')
        if item['status'] == 'pending'
    )
    stale_response = first_screen.post(
        f"/appointments/{appointment['id']}/confirm",
        data={
            'expected_version': str(appointment['version']),
            'provider_screen_token': first_token,
        },
    )
    assert stale_response.status_code == 302
    assert store.get_appointment_by_id(appointment['id'])['status'] == 'pending'

    active_response = second_screen.post(
        f"/appointments/{appointment['id']}/confirm",
        data={
            'expected_version': str(appointment['version']),
            'provider_screen_token': second_token,
        },
    )
    assert active_response.status_code == 302
    assert store.get_appointment_by_id(appointment['id'])['status'] == 'confirmed'

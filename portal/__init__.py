import os

from flask import Flask
from flask_socketio import SocketIO

from .config import DEFAULT_DATABASE_PATH
from .routes import portal, register_socket_handlers
from .store import AppointmentStore


def create_app(test_config=None):
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    app = Flask(
        __name__,
        template_folder=os.path.join(project_root, "templates"),
        static_folder=os.path.join(project_root, "static"),
    )
    app.config.from_mapping(
        SECRET_KEY="patient-portal-demo",
        DATABASE_PATH=DEFAULT_DATABASE_PATH,
    )
    if test_config:
        app.config.update(test_config)

    database_path = app.config["DATABASE_PATH"]
    if database_path != ":memory:":
        os.makedirs(os.path.dirname(database_path) or ".", exist_ok=True)

    os.environ["PORTAL_DB_PATH"] = database_path
    store = AppointmentStore(database_path)
    socketio = SocketIO(app, cors_allowed_origins="*")
    app.extensions["appointment_store"] = store
    app.extensions["socketio"] = socketio
    app.register_blueprint(portal)
    register_socket_handlers(socketio)
    app.socketio = socketio
    return app


__all__ = ["create_app"]
import os

from flask import Flask, jsonify, render_template
from werkzeug.exceptions import RequestEntityTooLarge

from app.catalog import SPEC
from app.config import MAX_FILE, ROOT
from app.errors import DomainIssue, error_response
from app.reconcile.receipts import ReconciliationError
from app.reconcile.store import ReceiptStore
from app.review.store import Store
from app.routes import api


def create_app(database=None):
    database = database or os.getenv("WORKBENCH_DB", str(ROOT / "instance" / "workbench.sqlite3"))
    app = Flask(__name__)
    app.config["DATABASE_PATH"] = database
    app.config["MAX_CONTENT_LENGTH"] = MAX_FILE + 64 * 1024
    app.extensions["runs"] = Store(database)
    app.extensions["receipts"] = ReceiptStore(database)
    app.register_error_handler(DomainIssue, error_response)
    app.register_error_handler(ReconciliationError, lambda exc: error_response(
        DomainIssue(exc.code, exc.status, str(exc))
    ))
    app.register_error_handler(RequestEntityTooLarge, lambda _: error_response(DomainIssue("FILE_TOO_LARGE", 413)))
    app.register_blueprint(api)

    @app.get("/")
    def home():
        return render_template("index.html", p=SPEC)

    @app.get("/healthz")
    def health():
        return jsonify(status="ok")

    return app


app = create_app()

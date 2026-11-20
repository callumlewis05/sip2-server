"""In-memory stand-in for the ``/api/sip2`` endpoints CDS-ILS will expose.

Used by the integration tests (started in a thread) and by docker-compose for
local development (``python -m tests.fake_ils --port 5001``).  It implements
the contract documented in ``cds_sip2/rest/client.py`` and the README.

Patrons exist here only so that checkout can be refused for an unknown or
blocked patron; the SIP2 server has no patron endpoints yet.
"""

import argparse
import copy
import datetime as dt
import threading

from flask import Blueprint, Flask, abort, jsonify, request
from werkzeug.serving import make_server

TOKEN = "test-token"
LOAN_DAYS = 28

PATRONS = {
    "P001": {"name": "Ada Lovelace", "blocks": []},
    "P002": {"name": "Blocked Patron",
             "blocks": ["charge_privileges_denied", "excessive_outstanding_fines"]},
}

ITEMS = {
    "CM-B000456412": {"title": "Écoute", "media_type": "book",
                      "status": "available", "location": "Main reading room",
                      "on_loan_to": None, "due_date": None, "hold_queue_length": 0},
    "CM-1234567890": {"title": "MySQL",
                      "status": "on_loan", "location": "Main reading room",
                      "on_loan_to": "P001", "due_date": None, "hold_queue_length": 0},
    "CM-P00101093": {"title": "Windows 95 Answers!",
                      "status": "on_loan", "location": "Main reading room",
                      "on_loan_to": "P001", "due_date": None, "hold_queue_length": 0},
}


def fresh_state():
    state = {
        "patrons": copy.deepcopy(PATRONS),
        "items": copy.deepcopy(ITEMS),
        "transactions": {},
    }
    state["items"]["CM-1234567890"]["due_date"] = (dt.date.today() + dt.timedelta(days=7)).isoformat()
    return state


def create_app(token=TOKEN, state=None):
    app = Flask("fake_ils")
    app.config["STATE"] = state or fresh_state()
    app.config["TOKEN"] = token
    app.register_blueprint(_blueprint(), url_prefix="/api/sip2")
    return app


def _blueprint():
    bp = Blueprint("sip2", __name__)

    def st():
        from flask import current_app

        return current_app.config["STATE"]

    def reject(status, code, message, user_message=None, **extra):
        body = {"code": code, "message": message, "user_message": user_message or message}
        body.update(extra)
        response = jsonify(body)
        response.status_code = status
        abort(response)

    def today():
        return dt.date.today()

    def item_view(barcode, item):
        return {
            "barcode": barcode, "title": item["title"], "status": item["status"],
            "location": item["location"], "current_location": None,
            "due_date": item["due_date"], "hold_queue_length": item["hold_queue_length"],
            "owner": "CERN Library", "media_type": item.get("media_type"),
            "messages": [f"Seen: {item['seen']}"] if item.get("seen") else [],
        }

    def replay(txid):
        if txid and txid in st()["transactions"]:
            return jsonify(st()["transactions"][txid])
        return None

    def remember(txid, result):
        if txid:
            st()["transactions"][txid] = result
        return jsonify(result)

    @bp.before_request
    def check_token():
        from flask import current_app

        expected = f"Bearer {current_app.config['TOKEN']}"
        if request.headers.get("Authorization") != expected:
            reject(401, "service_auth", "invalid service token")

    @bp.get("/ping")
    def ping():
        return jsonify({"ok": True})

    @bp.get("/items/<barcode>")
    def get_item(barcode):
        item = st()["items"].get(barcode)
        if item is None:
            reject(404, "item_not_found", "unknown item")
        return jsonify(item_view(barcode, item))

    @bp.post("/checkout")
    def do_checkout():
        data = request.get_json(force=True, silent=True) or {}
        replayed = replay(data.get("transaction_id"))
        if replayed is not None:
            return replayed
        item = st()["items"].get(data.get("item_barcode"))
        if item is None:
            reject(404, "item_not_found", "unknown item")
        patron = st()["patrons"].get(data.get("patron_barcode"))
        if patron is None:
            reject(409, "patron_not_found", "unknown patron",
                   "Card not recognised.", title=item["title"])
        if "charge_privileges_denied" in patron["blocks"]:
            reject(409, "patron_blocked", "patron is blocked",
                   "Your account is blocked. Please ask at the library desk.", title=item["title"])
        if item["status"] != "available":
            reject(409, "item_not_available", f"item status is {item['status']}",
                   "This item is not available for loan.", title=item["title"])
        due = (today() + dt.timedelta(days=LOAN_DAYS)).isoformat()
        item.update(status="on_loan", on_loan_to=data["patron_barcode"], due_date=due)
        return remember(data.get("transaction_id"), {
            "title": item["title"], "due_date": due, "is_renewal": False,
            "transaction_id": data.get("transaction_id"),
        })

    @bp.post("/checkin")
    def do_checkin():
        data = request.get_json(force=True, silent=True) or {}
        replayed = replay(data.get("transaction_id"))
        if replayed is not None:
            return replayed
        item = st()["items"].get(data.get("item_barcode"))
        if item is None:
            reject(404, "item_not_found", "unknown item")
        if item["status"] != "on_loan":
            reject(409, "item_not_on_loan", f"item status is {item['status']}",
                   "This item is not on loan.", title=item["title"])
        item.update(status="available", on_loan_to=None, due_date=None)
        return remember(data.get("transaction_id"), {
            "title": item["title"], "permanent_location": item["location"],
            "alert": item["hold_queue_length"] > 0,
            "transaction_id": data.get("transaction_id"),
        })

    @bp.post("/items/<barcode>/status")
    def update_item_status(barcode):
        data = request.get_json(force=True, silent=True) or {}
        replayed = replay(data.get("transaction_id"))
        if replayed is not None:
            return replayed
        item = st()["items"].get(barcode)
        if item is None:
            reject(404, "item_not_found", "unknown item")
        item["seen"] = data.get("properties") or "yes"
        return remember(data.get("transaction_id"), item_view(barcode, item))

    return bp


class ThreadedServer:
    """Run the fake ILS on a background thread (port 0 = pick a free one)."""

    def __init__(self, app=None, host="127.0.0.1", port=0):
        self.app = app or create_app()
        self._server = make_server(host, port, self.app, threaded=True)
        self.host = host
        self.port = self._server.server_port
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self):
        return f"http://{self.host}:{self.port}/api/sip2"

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._server.shutdown()
        self._thread.join(timeout=5)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Fake CDS-ILS /api/sip2 for local development")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5001)
    parser.add_argument("--token", default=TOKEN)
    args = parser.parse_args(argv)
    create_app(token=args.token).run(host=args.host, port=args.port)


if __name__ == "__main__":
    main()

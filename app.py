from flask import Flask, request, jsonify, make_response
from flask_cors import CORS
import requests
from datetime import datetime
import time

app = Flask(__name__)

# CORS wide-open (simple)
CORS(app, resources={r"/*": {"origins": "*"}})

SAHULAT_PAYOUT_URL = "https://server.sahulatpay.com/disbursement/tele"
SAHULAT_PAYIN_URL = "https://server.sahulatpay.com/transactions/tele"

SECRET_KEYS = {
    "password",
    "integritySalt",
    "integrity_salt",
    "secret",
    "salt",
    "apiKey",
    "api_key",
}

def sanitize(obj):
    if isinstance(obj, list):
        return [sanitize(x) for x in obj]
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            out[k] = "***" if k in SECRET_KEYS else sanitize(v)
        return out
    return obj

def extract_first_transaction(original, txn_type):
    if txn_type == "payout":
        txns = (original.get("data") or {}).get("transactions") or []
    else:
        txns = original.get("transactions") or []
    if isinstance(txns, list) and len(txns) > 0:
        return txns[0]
    return None

def normalize_status(status):
    if not status:
        return "UNKNOWN"
    s = str(status).strip().lower()
    if s in ("success", "completed"):
        return "COMPLETED"
    if s in ("failed", "reversed"):
        return "FAILED"
    if s in ("pending", "inprogress", "processing"):
        return "PENDING"
    return str(status).upper()

def pick_any(d, keys, default=None):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default

def call_sahulat(order_id: str, txn_type: str):
    base_url = SAHULAT_PAYOUT_URL if txn_type == "payout" else SAHULAT_PAYIN_URL

    r = requests.get(
        base_url,
        params={"merchantTransactionId": order_id},
        timeout=15
    )

    try:
        original = r.json()
    except Exception:
        original = {"raw": r.text}

    original_dict = original if isinstance(original, dict) else {}

    txn = extract_first_transaction(original_dict, txn_type)
    txn = txn if isinstance(txn, dict) else {}

    raw_status = txn.get("status") if txn else None
    status = normalize_status(raw_status)

    txn_id = pick_any(txn, ["transactionId", "txnId", "id"], default="N/A")
    txn_date = pick_any(txn, ["createdAt", "created_at", "date_time", "date", "timestamp"], default="N/A")
    processed_at = pick_any(txn, ["updatedAt", "updated_at"], default=None) or txn_date

    return {
        "http_ok": r.ok,
        "status_code": r.status_code,
        "order_id": order_id,
        "type": txn_type,
        "summary": {
            "status": status,
            "raw_status": (raw_status if raw_status is not None else "N/A"),
            "txn_id": txn_id,
            "processed_at": processed_at,
            "date": txn_date,
        },
        "original_sanitized": sanitize(original) if isinstance(original, (dict, list)) else original
    }

# global CORS headers (extra safety)
@app.after_request
def add_cors_headers(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp

@app.route("/status", methods=["GET", "OPTIONS"])
def status_proxy():
    if request.method == "OPTIONS":
        return make_response("", 204)

    order_id = request.args.get("id", "").strip()
    txn_type = request.args.get("type", "payout").strip().lower()

    if not order_id:
        return jsonify({"ok": False, "error": "id is required"}), 400
    if txn_type not in ("payout", "payin"):
        return jsonify({"ok": False, "error": "type must be payout or payin"}), 400

    try:
        out = call_sahulat(order_id, txn_type)

        result = {
            "ok": out["http_ok"],
            "status_code": out["status_code"],
            "order_id": out["order_id"],
            "type": out["type"],
            "summary": {
                "status": out["summary"]["status"],
                "txn_id": out["summary"]["txn_id"],
                "date": out["summary"]["date"],
                "processed_at": out["summary"]["processed_at"],
            },
            "data": out["original_sanitized"]
        }
        return jsonify(result), out["status_code"]

    except requests.exceptions.Timeout:
        return jsonify({"ok": False, "error": "timeout"}), 504
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ✅ NEW: GET-based bulk endpoint (no preflight for browser)
@app.route("/bulk-status-get", methods=["GET", "OPTIONS"])
def bulk_status_get():
    if request.method == "OPTIONS":
        return make_response("", 204)

    raw_ids = request.args.get("ids", "").strip()
    txn_type = request.args.get("type", "payout").strip().lower()

    if txn_type not in ("payout", "payin"):
        return jsonify({"ok": False, "error": "type must be payout or payin"}), 400

    # ids comma-separated string -> list
    ids = [x.strip() for x in raw_ids.split(",") if x.strip()]

    if not ids:
        return jsonify({"ok": False, "error": "no ids provided"}), 400

    if len(ids) > 5000:
        return jsonify({"ok": False, "error": "Too many ids. Max 5000 per request."}), 400

    results = []
    started = time.time()

    for order_id in ids:
        try:
            out = call_sahulat(order_id, txn_type)
            status = out["summary"]["status"]
            raw_status = out["summary"]["raw_status"]
            txn_id = out["summary"]["txn_id"]
            processed_at = out["summary"]["processed_at"]

            note = ""
            if not out["http_ok"]:
                note = "Upstream not ok"
            if status == "UNKNOWN":
                note = note or "Unknown status"
            if out["status_code"] != 200:
                note = note or f"HTTP {out['status_code']}"

            results.append({
                "order_id": order_id,
                "type": txn_type,
                "status": status,
                "raw_status": raw_status,
                "txn_id": txn_id,
                "processed_at": processed_at,
                "status_code": out["status_code"],
                "note": note
            })
        except requests.exceptions.Timeout:
            results.append({
                "order_id": order_id,
                "type": txn_type,
                "status": "TIMEOUT",
                "raw_status": "TIMEOUT",
                "txn_id": "",
                "processed_at": "",
                "status_code": 504,
                "note": "timeout"
            })
        except Exception as e:
            results.append({
                "order_id": order_id,
                "type": txn_type,
                "status": "ERROR",
                "raw_status": "ERROR",
                "txn_id": "",
                "processed_at": "",
                "status_code": 500,
                "note": str(e)
            })

    elapsed_ms = int((time.time() - started) * 1000)

    return jsonify({
        "ok": True,
        "type": txn_type,
        "count": len(results),
        "elapsed_ms": elapsed_ms,
        "results": results
    }), 200


@app.get("/")
def health():
    return jsonify({"ok": True, "service": "sahulatpay-status-proxy"}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)

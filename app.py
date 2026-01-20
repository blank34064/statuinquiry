from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
from datetime import datetime

app = Flask(__name__)
CORS(app)

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
    """Recursively mask sensitive keys in dict/list."""
    if isinstance(obj, list):
        return [sanitize(x) for x in obj]
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in SECRET_KEYS:
                out[k] = "***"
            else:
                out[k] = sanitize(v)
        return out
    return obj


def extract_first_transaction(original, txn_type):
    """
    Return first transaction dict based on payin/payout structure.
    - payout: original['data']['transactions']
    - payin: original['transactions']
    """
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


@app.get("/")
def health():
    return jsonify({"ok": True, "service": "sahulatpay-status-proxy"}), 200


@app.get("/status")
def status_proxy():
    """
    Proxy endpoint used by HTML:
    /status?id=<merchantTransactionId>&type=payout|payin
    """
    order_id = request.args.get("id", "").strip()
    txn_type = request.args.get("type", "payout").strip().lower()

    if not order_id:
        return jsonify({"ok": False, "error": "id is required"}), 400

    if txn_type not in ("payout", "payin"):
        return jsonify({"ok": False, "error": "type must be payout or payin"}), 400

    base_url = SAHULAT_PAYOUT_URL if txn_type == "payout" else SAHULAT_PAYIN_URL

    try:
        r = requests.get(
            base_url,
            params={"merchantTransactionId": order_id},
            timeout=15,
        )

        try:
            original = r.json()
        except Exception:
            original = {"raw": r.text}

        txn = extract_first_transaction(
            original if isinstance(original, dict) else {}, txn_type
        )
        txn = txn if isinstance(txn, dict) else {}

        raw_status = txn.get("status") if txn else None
        status = normalize_status(raw_status)

        txn_id = pick_any(txn, ["transactionId", "txnId", "id"], default="N/A")
        txn_date = pick_any(
            txn, ["createdAt", "created_at", "date_time", "date", "timestamp"], default="N/A"
        )

        amount = pick_any(txn, ["amount", "totalAmount", "txnAmount", "balance"], default=None)
        currency = pick_any(txn, ["currency", "ccy"], default="PKR")

        # example merchant field from your screenshot
        merchant = None
        if isinstance(txn.get("jazzCashMerchant"), dict):
            merchant = txn["jazzCashMerchant"].get("merchant_of")

        merchant = merchant or pick_any(txn, ["merchantName"], default=None)

        result = {
            "ok": r.ok,
            "status_code": r.status_code,
            "order_id": order_id,
            "type": txn_type,
            # Summary block (UI ke liye easy)
            "summary": {
                "status": status,  # COMPLETED / FAILED / PENDING / ...
                "txn_id": txn_id,  # provider txn id
                "date": txn_date,
                "amount": amount,
                "currency": currency,
                "merchant": merchant,
            },
            # Full response but sanitized
            "data": sanitize(original)
            if isinstance(original, (dict, list))
            else original,
        }

        return jsonify(result), r.status_code

    except requests.exceptions.Timeout:
        return jsonify({"ok": False, "error": "timeout"}), 504
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# 🔥 NEW: Bulk status endpoint
@app.post("/bulk-status")
def bulk_status():
    """
    Bulk status checker.

    Request (JSON body):
    {
      "type": "payout" | "payin",
      "ids": ["359884596", "87703010204162", ...]
    }

    Response:
    {
      "ok": true,
      "type": "payout",
      "count": 3,
      "results": [
        {
          "order_id": "359884596",
          "type": "payout",
          "status": "COMPLETED",
          "raw_status": "Completed",
          "txn_id": "202601201768897307262781",
          "processed_at": "2026-01-20T08:21:46.665Z",
          "status_code": 200,
          "note": ""
        },
        ...
      ]
    }
    """
    data = request.get_json(force=True, silent=True) or {}
    txn_type = (data.get("type") or "payout").strip().lower()
    ids = data.get("ids") or []

    if txn_type not in ("payout", "payin"):
      return jsonify({"ok": False, "error": "type must be payout or payin"}), 400

    if not isinstance(ids, list) or not ids:
      return jsonify({"ok": False, "error": "ids must be a non-empty list"}), 400

    base_url = SAHULAT_PAYOUT_URL if txn_type == "payout" else SAHULAT_PAYIN_URL
    results = []

    for raw_id in ids:
        order_id = str(raw_id).strip()
        if not order_id:
            continue

        try:
            r = requests.get(
                base_url,
                params={"merchantTransactionId": order_id},
                timeout=15,
            )
            status_code = r.status_code

            try:
                original = r.json()
            except Exception:
                original = {}

            txn = extract_first_transaction(
                original if isinstance(original, dict) else {}, txn_type
            )

            if not txn:
                # koi transaction mila hi nahi
                results.append(
                    {
                        "order_id": order_id,
                        "type": txn_type,
                        "status": "NOT_IN_BO",
                        "raw_status": None,
                        "txn_id": "N/A",
                        "processed_at": "N/A",
                        "status_code": status_code,
                        "note": "NOT_IN_BO",
                    }
                )
                continue

            raw_status = txn.get("status")
            status = normalize_status(raw_status)

            txn_id = pick_any(
                txn,
                ["transactionId", "txnId", "id"],
                default="N/A",
            )
            txn_date = pick_any(
                txn,
                ["createdAt", "created_at", "date_time", "date", "timestamp"],
                default="N/A",
            )

            results.append(
                {
                    "order_id": order_id,
                    "type": txn_type,
                    "status": status,
                    "raw_status": raw_status,
                    "txn_id": txn_id,
                    "processed_at": txn_date,
                    "status_code": status_code,
                    "note": "",
                }
            )

        except requests.exceptions.Timeout:
            results.append(
                {
                    "order_id": order_id,
                    "type": txn_type,
                    "status": "TIMEOUT",
                    "raw_status": None,
                    "txn_id": "N/A",
                    "processed_at": "N/A",
                    "status_code": 504,
                    "note": "TIMEOUT",
                }
            )
        except Exception as e:
            results.append(
                {
                    "order_id": order_id,
                    "type": txn_type,
                    "status": "ERROR",
                    "raw_status": None,
                    "txn_id": "N/A",
                    "processed_at": "N/A",
                    "status_code": 500,
                    "note": str(e),
                }
            )

    return jsonify(
        {
            "ok": True,
            "type": txn_type,
            "count": len(results),
            "results": results,
        }
    ), 200


if __name__ == "__main__":
    # run: python app.py
    app.run(host="0.0.0.0", port=5000, debug=True)

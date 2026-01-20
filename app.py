import os
from flask import Flask, request, jsonify
from flask_cors import CORS
import requests

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
    if not isinstance(original, dict):
        return None

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
    if not isinstance(d, dict):
        return default
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


@app.get("/")
def health():
    # Railway health check ke liye simple OK route
    return jsonify({"ok": True, "service": "sahulatpay-status-proxy"}), 200


@app.get("/status")
def status_proxy():
    """
    Proxy endpoint used by HTML:
    /status?id=<merchantTransactionId>&type=payout|payin
    """
    order_id = (request.args.get("id") or "").strip()
    txn_type = (request.args.get("type") or "payout").strip().lower()

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

        txn = extract_first_transaction(original, txn_type)
        txn = txn or {}

        raw_status = txn.get("status")
        status = normalize_status(raw_status)

        txn_id = pick_any(txn, ["transactionId", "txnId", "id"], default="N/A")
        txn_date = pick_any(
            txn,
            ["createdAt", "created_at", "date_time", "date", "timestamp"],
            default="N/A",
        )

        amount = pick_any(
            txn,
            ["amount", "totalAmount", "txnAmount", "balance"],
            default=None,
        )
        currency = pick_any(txn, ["currency", "ccy"], default="PKR")

        merchant = None
        jcm = txn.get("jazzCashMerchant")
        if isinstance(jcm, dict):
            merchant = jcm.get("merchant_of")
        merchant = merchant or pick_any(txn, ["merchantName"], default=None)

        result = {
            "ok": r.ok,
            "status_code": r.status_code,
            "order_id": order_id,
            "type": txn_type,
            "summary": {
                "status": status,   # COMPLETED / FAILED / PENDING / ...
                "txn_id": txn_id,   # provider txn id
                "date": txn_date,
                "amount": amount,
                "currency": currency,
                "merchant": merchant,
            },
            # full but sanitized
            "data": sanitize(original)
            if isinstance(original, (dict, list))
            else original,
        }

        # Even if SahulatPay 500/400 de, hum JSON bhej rahe hain
        return jsonify(result), r.status_code or 200

    except requests.exceptions.Timeout:
        return jsonify({"ok": False, "error": "timeout"}), 504
    except Exception as e:
        # yahan exception bhi JSON me wrap ho raha hai, process crash nahi karega
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    # Railway apna PORT env deta hai, usko use karo
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)

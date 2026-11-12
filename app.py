import os
import traceback

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

load_dotenv()

import ai  # noqa: E402
import recon  # noqa: E402

app = Flask(__name__)
CACHE = {"recon": None, "log": []}


def _err(e, code=500):
    traceback.print_exc()
    return jsonify({"error": str(e) or f"{type(e).__name__} (no message from Braintree)"}), code


@app.get("/")
def index():
    return render_template("index.html", ai_provider=ai.provider())


@app.post("/api/seed")
def api_seed():
    try:
        out = recon.seed()
        CACHE["log"] = [{"msg": f"Seeded {out['customers']} Braintree sandbox customers and a ledger of {out['ledger_rows']} rows."}]
        CACHE["recon"] = recon.reconcile()
        return jsonify({**out, **CACHE["recon"], "log": CACHE["log"]})
    except Exception as e:
        return _err(e)


@app.get("/api/recon")
def api_recon():
    try:
        if not recon.load_state()["customers"]:
            return jsonify({"summary": None, "rows": [], "log": []})
        CACHE["recon"] = recon.reconcile()
        return jsonify({**CACHE["recon"], "log": CACHE["log"]})
    except Exception as e:
        return _err(e)


def _row(rid):
    if not CACHE["recon"]:
        CACHE["recon"] = recon.reconcile()
    return next(r for r in CACHE["recon"]["rows"] if r["id"] == rid)


@app.post("/api/explain/<int:rid>")
def api_explain(rid):
    try:
        return jsonify(ai.explain(_row(rid)))
    except Exception as e:
        return _err(e)


@app.post("/api/fix/<int:rid>")
def api_fix(rid):
    try:
        row = _row(rid)
        msg = recon.apply_fix(row)
        CACHE["log"].insert(0, {"msg": msg})
        CACHE["recon"] = recon.reconcile()
        return jsonify({"result": msg, **CACHE["recon"], "log": CACHE["log"]})
    except Exception as e:
        return _err(e)


@app.post("/api/ask")
def api_ask():
    try:
        q = (request.json or {}).get("question", "").strip()
        if not q:
            return _err("Empty question", 400)
        if not CACHE["recon"]:
            CACHE["recon"] = recon.reconcile()
        return jsonify(ai.ask(q, CACHE["recon"]))
    except Exception as e:
        return _err(e)


if __name__ == "__main__":
    app.run(debug=True, port=int(os.getenv("PORT", 5000)))

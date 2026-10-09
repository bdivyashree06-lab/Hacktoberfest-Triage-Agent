"""Tiny Flask server: static UI + triage/approval APIs.

Run:  export GITHUB_TOKEN=... DEMO_REPO=owner/triage-demo && python app.py
Then: open http://localhost:8000
"""

from __future__ import annotations

import os

from flask import Flask, jsonify, request, send_from_directory

import agent
import llm
import tools

app = Flask(__name__, static_folder="static")


@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "model": llm.MODEL, "backend": llm.BASE_URL})


@app.get("/api/issues")
def issues():
    """Seeded issues for the demo dropdown / CLI."""
    return jsonify(tools.list_issues())


@app.get("/api/pending")
def pending():
    return jsonify({"pending": tools.pending_snapshot()})


@app.post("/api/triage")
def triage():
    payload = request.get_json(silent=True) or {}
    try:
        number = int(payload.get("issue", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "body must be {\"issue\": <number>}"}), 400
    if number < 1:
        return jsonify({"error": "issue number must be >= 1"}), 400

    result = agent.run(number)
    result["pending_actions"] = tools.pending_snapshot()
    return jsonify(result)  # needs_human results are still HTTP 200 — they are
    # a designed outcome (see README "Failure handling"), not a server error.


@app.post("/api/approve")
def approve():
    """Human-in-the-loop gate: the model itself can never reach this endpoint."""
    payload = request.get_json(silent=True) or {}
    action_id = str(payload.get("action_id", ""))
    decision = payload.get("decision")
    if not action_id or decision not in ("approve", "deny"):
        return jsonify({"error": "body must be {\"action_id\": ..., \"decision\": \"approve\"|\"deny\"}"}), 400
    result = tools.approve(action_id) if decision == "approve" else tools.deny(action_id)
    return jsonify(result), (200 if result.get("applied") or result.get("denied") else 409)


if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    app.run(host=host, port=port, threaded=True)

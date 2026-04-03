from flask import Flask, jsonify


app = Flask(__name__)


@app.get("/")
def health() -> tuple[dict[str, str], int]:
    return jsonify({"status": "ok", "service": "imgadvisor-test"}), 200


@app.get("/ready")
def ready() -> tuple[dict[str, str], int]:
    return jsonify({"ready": "true"}), 200

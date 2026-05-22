from __future__ import annotations

from flask import Flask, jsonify, request

from .runner import list_jobs, run_job


def create_app() -> Flask:
    app = Flask(__name__)

    @app.get("/health")
    def health():
        return jsonify({"status": "ok"})

    @app.get("/api/v1/jobs")
    def jobs():
        return jsonify({"jobs": list_jobs()})

    @app.post("/api/v1/jobs/<job_name>/run")
    def run(job_name: str):
        payload = request.get_json(silent=True) or {}
        options = payload.get("options", {})
        timeout_raw = payload.get("timeout_seconds", 1200)

        try:
            timeout = int(timeout_raw)
        except (TypeError, ValueError):
            return jsonify({"error": "timeout_seconds must be a positive integer"}), 400

        if timeout <= 0:
            return jsonify({"error": "timeout_seconds must be a positive integer"}), 400

        try:
            result = run_job(job_name=job_name, options=options, timeout_seconds=timeout)
        except ValueError as exc:
            if "Unknown job:" in str(exc):
                return jsonify({"error": "Unknown job"}), 404
            return jsonify({"error": "Invalid job execution request"}), 400
        except FileNotFoundError:
            return jsonify({"error": "Job script not found"}), 404
        except Exception as exc:  # pragma: no cover
            app.logger.exception("Unexpected job execution failure: %s", exc)
            return jsonify({"error": "Internal server error"}), 500

        status_code = 200 if result["success"] else 500
        return jsonify(result), status_code

    return app

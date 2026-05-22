from __future__ import annotations

from flask import Flask, jsonify, request

from .runner import (
    InvalidJobRequestError,
    JobScriptNotFoundError,
    JobTimeoutError,
    UnknownJobError,
    list_jobs,
    run_job,
)


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
            return jsonify({"error": "timeout_seconds must be an integer"}), 400

        try:
            result = run_job(job_name=job_name, options=options, timeout_seconds=timeout)
        except UnknownJobError:
            return jsonify({"error": "Unknown job"}), 404
        except InvalidJobRequestError:
            return jsonify({"error": "timeout_seconds must be positive"}), 400
        except JobScriptNotFoundError:
            return jsonify({"error": f"Job job not found for job '{job_name}'"}), 404
        except JobTimeoutError:
            return jsonify({"error": "Job execution timeout exceeded"}), 504
        except Exception as exc:  # pragma: no cover
            app.logger.exception("Unexpected job execution failure: %s", exc)
            return jsonify({"error": "Internal server error"}), 500

        status_code = 200 if result["success"] else 500
        return jsonify(result), status_code

    return app
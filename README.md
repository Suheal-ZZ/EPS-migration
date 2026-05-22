# EPS Migration Microservice (Flask)

This project now exposes the migration scripts through a Flask microservice.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
python3 app.py
```

## API

- `GET /health` → service health check
- `GET /api/v1/jobs` → list available migration/validation jobs
- `POST /api/v1/jobs/<job_name>/run` → run a job

Example request body:

```json
{
  "options": {
    "dry_run": true,
    "month": "2024-04"
  },
  "timeout_seconds": 1200
}
```

`options` maps to supported script CLI flags (`--dry-run`, `--month`, etc.).

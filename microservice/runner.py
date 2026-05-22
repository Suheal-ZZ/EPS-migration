from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .script_registry import ALLOWED_ARGS, SCRIPT_REGISTRY

REPO_ROOT = Path(__file__).resolve().parents[1]


class UnknownJobError(ValueError):
    """Raised when a requested job is not registered."""


class InvalidJobRequestError(ValueError):
    """Raised when request options are invalid for safe execution."""


class JobScriptNotFoundError(FileNotFoundError):
    """Raised when a mapped script file cannot be found."""


def list_jobs() -> list[str]:
    """Return all supported migration/validation job names."""
    return sorted(SCRIPT_REGISTRY.keys())


def _build_cli_args(raw_options: dict) -> list[str]:
    """Convert a validated options dictionary into whitelisted CLI arguments."""
    cli_args: list[str] = []
    for key, value in raw_options.items():
        expected_type = ALLOWED_ARGS.get(key)
        if expected_type is None:
            continue

        flag = f"--{key.replace('_', '-')}"
        if expected_type is bool:
            if bool(value):
                cli_args.append(flag)
            continue

        if value is None:
            continue
        cli_args.extend([flag, str(value)])

    return cli_args


def run_job(job_name: str, options: dict | None = None, timeout_seconds: int = 1200) -> dict:
    """Execute a registered migration script and return command output metadata."""
    if job_name not in SCRIPT_REGISTRY:
        raise UnknownJobError(job_name)
    if timeout_seconds <= 0:
        raise InvalidJobRequestError("timeout_seconds must be a positive integer")

    script_path = REPO_ROOT / SCRIPT_REGISTRY[job_name]
    if not script_path.exists():
        raise JobScriptNotFoundError(str(script_path))

    safe_options = options or {}
    command = [sys.executable, str(script_path), *_build_cli_args(safe_options)]

    env = os.environ.copy()
    process = subprocess.run(
        command,
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )

    return {
        "job": job_name,
        "exit_code": process.returncode,
        "stdout": process.stdout,
        "stderr": process.stderr,
        "success": process.returncode == 0,
    }

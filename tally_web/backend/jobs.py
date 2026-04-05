"""
jobs.py — Thread-safe in-memory job store.
Each job tracks: status, log lines, output files, and error message.
"""

from __future__ import annotations

import threading
import uuid
from typing import Any

# Job status constants
PENDING = "pending"
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"

_lock = threading.Lock()
_jobs: dict[str, dict[str, Any]] = {}


def create_job() -> str:
    """Create a new job entry and return its unique ID."""
    job_id = str(uuid.uuid4())
    with _lock:
        _jobs[job_id] = {
            "id": job_id,
            "status": PENDING,
            "log_lines": [],
            "output_files": [],
            "error": None,
        }
    return job_id


def get_job(job_id: str) -> dict[str, Any] | None:
    """Return a snapshot of the job dict, or None if not found."""
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return None
        # Return a shallow copy to avoid race conditions on the caller side
        return {
            "id": job["id"],
            "status": job["status"],
            "log_lines": list(job["log_lines"]),
            "output_files": list(job["output_files"]),
            "error": job["error"],
        }


def append_log(job_id: str, line: str) -> None:
    """Append a log line to an existing job."""
    with _lock:
        if job_id in _jobs:
            _jobs[job_id]["log_lines"].append(line)


def set_status(job_id: str, status: str) -> None:
    """Update the status of an existing job."""
    with _lock:
        if job_id in _jobs:
            _jobs[job_id]["status"] = status


def set_output_files(job_id: str, files: list[str]) -> None:
    """Set the list of output file paths for a completed job."""
    with _lock:
        if job_id in _jobs:
            _jobs[job_id]["output_files"] = files


def set_error(job_id: str, error: str) -> None:
    """Record an error message on a failed job."""
    with _lock:
        if job_id in _jobs:
            _jobs[job_id]["error"] = error


def list_jobs() -> list[dict[str, Any]]:
    """Return a list of all job snapshots (for debugging)."""
    with _lock:
        return [
            {
                "id": j["id"],
                "status": j["status"],
                "output_files": list(j["output_files"]),
                "error": j["error"],
            }
            for j in _jobs.values()
        ]

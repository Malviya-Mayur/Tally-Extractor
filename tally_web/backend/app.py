"""
app.py — FastAPI application for the Tally Pipeline Web Interface.

Endpoints:
  POST /api/extract              Start an extraction job
  GET  /api/status/<job_id>      Poll job status + log lines + output files
  GET  /api/logs/<job_id>        SSE stream of live log lines
  GET  /api/download/<job_id>/<filename>  Download a generated CSV
  GET  /api/config               Get server-side defaults
  POST /api/config               Update server-side defaults

Run with:
  cd tally_web
  uvicorn backend.app:app --host 127.0.0.1 --port 8080 --reload
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator
from sse_starlette.sse import EventSourceResponse

from . import jobs
from .pipeline_runner import start_extraction_job

# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("web_pipeline.log"),
    ],
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Config loading
# ─────────────────────────────────────────────────────────────────────────────
_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"
_DEFAULT_CONFIG: dict[str, Any] = {
    "tally": {"host": "localhost", "port": 9000, "default_from": "20250401", "default_to": "20260331"},
    "output": {"directory": "./tally_out", "timestamp": True},
    "server": {"bind": "127.0.0.1", "port": 8080, "auth_enabled": False},
    "pipeline": {"retries": 3, "timeout": 60},
}


def _load_config() -> dict[str, Any]:
    if _CONFIG_PATH.exists():
        with _CONFIG_PATH.open("r", encoding="utf-8") as f:
            loaded = yaml.safe_load(f) or {}
        # Deep merge with defaults
        cfg = dict(_DEFAULT_CONFIG)
        for section, defaults in _DEFAULT_CONFIG.items():
            if section in loaded:
                cfg[section] = {**defaults, **loaded[section]}
        return cfg
    return dict(_DEFAULT_CONFIG)


def _save_config(cfg: dict[str, Any]) -> None:
    with _CONFIG_PATH.open("w", encoding="utf-8") as f:
        yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)


_config: dict[str, Any] = _load_config()

# ─────────────────────────────────────────────────────────────────────────────
# FastAPI app
# ─────────────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Tally Pipeline Web Interface",
    description="Browser-based wrapper for Tally_Pipeline.py",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8080", "http://127.0.0.1:8080"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve frontend static files
_FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
if _FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_FRONTEND_DIR)), name="static")


@app.get("/", include_in_schema=False)
async def root():
    index = _FRONTEND_DIR / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return {"message": "Tally Pipeline API is running. Frontend not found."}


# ─────────────────────────────────────────────────────────────────────────────
# Request / Response models
# ─────────────────────────────────────────────────────────────────────────────

class ExportStarSchema(BaseModel):
    dimensions: list[str] = []
    facts: list[str] = []


class ExtractRequest(BaseModel):
    from_date: str
    to_date: str
    port: int = 9000
    out_dir: str = "./tally_out"
    retries: int = 3
    timeout: int = 60
    export_star_schema: ExportStarSchema = ExportStarSchema()

    @field_validator("from_date", "to_date")
    @classmethod
    def validate_date(cls, v: str) -> str:
        v = v.replace("-", "")  # allow YYYY-MM-DD or YYYYMMDD
        if len(v) != 8 or not v.isdigit():
            raise ValueError("Date must be in YYYYMMDD or YYYY-MM-DD format")
        return v


class ConfigUpdate(BaseModel):
    tally_port: int | None = None
    tally_host: str | None = None
    default_from: str | None = None
    default_to: str | None = None
    output_directory: str | None = None
    retries: int | None = None
    timeout: int | None = None


# ─────────────────────────────────────────────────────────────────────────────
# API Endpoints
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/api/extract")
async def extract(req: ExtractRequest):
    """Start a new extraction job and return its job_id."""
    job_id = jobs.create_job()
    params = {
        "from_date": req.from_date,
        "to_date": req.to_date,
        "port": req.port,
        "out_dir": req.out_dir,
        "retries": req.retries,
        "timeout": req.timeout,
        "export_dims": req.export_star_schema.dimensions,
        "export_facts": req.export_star_schema.facts,
    }
    logger.info("Starting extraction job %s | params: %s", job_id, params)
    start_extraction_job(job_id, params)
    return {"job_id": job_id, "message": "Extraction started"}


@app.get("/api/status/{job_id}")
async def status(job_id: str):
    """Return job status, log lines so far, and list of output files."""
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.get("/api/logs/{job_id}")
async def log_stream(job_id: str):
    """
    SSE endpoint that streams live log lines to the browser.
    The client connects once and receives lines as they are appended.
    Sends a final 'done' event when the job completes or fails.
    """
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    async def event_generator():
        sent = 0
        while True:
            current = jobs.get_job(job_id)
            if current is None:
                break

            all_lines = current["log_lines"]
            new_lines = all_lines[sent:]
            for line in new_lines:
                yield {"event": "log", "data": line}
                sent += 1

            if current["status"] in (jobs.COMPLETED, jobs.FAILED):
                yield {
                    "event": "done",
                    "data": current["status"],
                }
                break

            await asyncio.sleep(0.3)

    return EventSourceResponse(event_generator())


@app.get("/api/download/{job_id}/{filename}")
async def download(job_id: str, filename: str):
    """Serve a generated CSV file for download."""
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] != jobs.COMPLETED:
        raise HTTPException(status_code=409, detail="Job not yet completed")

    # Find the matching file in output_files list
    matched: str | None = None
    for filepath in job["output_files"]:
        if Path(filepath).name == filename:
            matched = filepath
            break

    if matched is None:
        raise HTTPException(status_code=404, detail="File not found in this job's output")

    file_path = Path(matched)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Output file missing from disk")

    return FileResponse(
        path=str(file_path),
        filename=filename,
        media_type="text/csv",
    )


@app.get("/api/config")
async def get_config():
    """Return the current server-side defaults."""
    cfg = _load_config()
    return {
        "tally_host": cfg["tally"]["host"],
        "tally_port": cfg["tally"]["port"],
        "default_from": cfg["tally"]["default_from"],
        "default_to": cfg["tally"]["default_to"],
        "output_directory": cfg["output"]["directory"],
        "retries": cfg["pipeline"]["retries"],
        "timeout": cfg["pipeline"]["timeout"],
    }


@app.post("/api/config")
async def update_config(update: ConfigUpdate):
    """Update server-side defaults and persist to config.yaml."""
    global _config
    cfg = _load_config()

    if update.tally_host is not None:
        cfg["tally"]["host"] = update.tally_host
    if update.tally_port is not None:
        cfg["tally"]["port"] = update.tally_port
    if update.default_from is not None:
        cfg["tally"]["default_from"] = update.default_from
    if update.default_to is not None:
        cfg["tally"]["default_to"] = update.default_to
    if update.output_directory is not None:
        cfg["output"]["directory"] = update.output_directory
    if update.retries is not None:
        cfg["pipeline"]["retries"] = update.retries
    if update.timeout is not None:
        cfg["pipeline"]["timeout"] = update.timeout

    _save_config(cfg)
    _config = cfg
    return {"message": "Configuration updated", "config": cfg}


@app.get("/api/browse-folder")
async def browse_folder():
    """
    Open a native OS folder-picker dialog on the server and return the
    selected absolute path.  Works on any machine with a display (Linux/Windows/macOS).
    Falls back to a 404-like error if tkinter or a display is unavailable.
    """
    try:
        import tkinter as tk
        from tkinter import filedialog

        # Create a hidden root window so the dialog appears on top
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)

        folder = filedialog.askdirectory(
            title="Select Output Directory",
            parent=root,
        )
        root.destroy()

        if not folder:
            return {"path": None, "cancelled": True}
        return {"path": str(Path(folder).resolve()), "cancelled": False}

    except Exception as exc:  # noqa: BLE001
        logger.warning("browse_folder failed: %s", exc)
        raise HTTPException(
            status_code=503,
            detail=f"Folder picker unavailable on this server: {exc}"
        )

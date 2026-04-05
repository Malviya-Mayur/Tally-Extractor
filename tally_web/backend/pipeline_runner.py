"""
pipeline_runner.py — Background thread executor for the Tally Pipeline.

Imports functions from Tally_Pipeline.py (one level up) and runs them
in a dedicated thread so the FastAPI server is never blocked.
"""

from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path
from typing import Any, Callable

# ── Make the parent directory importable so we can reach Tally_Pipeline ──────
_BASE = Path(__file__).resolve().parent.parent.parent  # …/Tally Extractor/
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))

import Tally_Pipeline as tp  # noqa: E402  (after sys.path fix)

from . import jobs  # noqa: E402

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

class _JobLogHandler(logging.Handler):
    """A logging.Handler that forwards records to the in-memory job log."""

    def __init__(self, job_id: str, extra_callback: Callable[[str], None] | None = None):
        super().__init__()
        self.job_id = job_id
        self.extra_callback = extra_callback

    def emit(self, record: logging.LogRecord) -> None:
        line = self.format(record)
        jobs.append_log(self.job_id, line)
        if self.extra_callback:
            self.extra_callback(line)


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def run_extraction(job_id: str, params: dict[str, Any]) -> None:
    """
    Execute the full Tally Pipeline for a given job_id using params dict.

    params keys:
      from_date       str  YYYYMMDD
      to_date         str  YYYYMMDD
      port            int  Tally HTTP port (default 9000)
      out_dir         str  Output directory path
      retries         int  Max HTTP retry attempts
      timeout         int  HTTP timeout in seconds
      export_dims     list[str]  Dimension table names to export (e.g. ["LEDGER"])
      export_facts    list[str]  Fact table names: "voucher", "ledger_entry", "inventory_line"
    """
    jobs.set_status(job_id, jobs.RUNNING)

    # Set up a dedicated log handler that writes to the job store
    handler = _JobLogHandler(job_id)
    handler.setFormatter(
        logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    )
    tp_logger = logging.getLogger("__main__")  # Tally_Pipeline uses root logger
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)
    tp_logger.addHandler(handler)

    def log(msg: str) -> None:
        jobs.append_log(job_id, msg)

    try:
        from_date: str = params["from_date"]
        to_date: str = params["to_date"]
        port: int = int(params.get("port", 9000))
        out_dir = Path(params.get("out_dir", "tally_out"))
        retries: int = int(params.get("retries", 3))
        timeout: int = int(params.get("timeout", 60))
        export_dims: list[str] = params.get("export_dims", [])
        export_facts: list[str] = params.get("export_facts", [])

        # ── Step 1: Fetch XML bytes from Tally ───────────────────────────────
        log(f"Fetching XML from Tally at port {port} for {from_date} → {to_date} …")
        raw_bytes = tp.fetch_tally_xml_bytes(
            from_date, to_date,
            port=port,
            max_retries=retries,
            timeout=timeout,
        )
        if raw_bytes is None:
            raise RuntimeError(
                "Failed to fetch data from Tally. "
                "Verify Tally is running and the TDL report 'APIRawVouchers' is loaded."
            )

        log(f"Received {len(raw_bytes):,} bytes. Parsing XML …")

        # ── Step 2: Parse XML ─────────────────────────────────────────────────
        import xml.etree.ElementTree as ET
        try:
            root = tp.parse_xml_from_bytes(raw_bytes)
        except ET.ParseError as exc:
            raise RuntimeError(f"Malformed XML from Tally: {exc}") from exc

        result = tp.parse_tally_star_schema(root)
        log(
            f"Parsed — vouchers: {len(result.fact_voucher)} | "
            f"ledger lines: {len(result.fact_ledger_entry)} | "
            f"inventory lines: {len(result.fact_inventory_line)}"
        )

        # ── Step 3: Build flat fact table ─────────────────────────────────────
        log("Building flat fact table …")
        flat_rows = tp.build_flat_fact_rows(root, result)

        # ── Step 4: Compose output filename ───────────────────────────────────
        import calendar
        import re
        from datetime import datetime as _dt

        company = re.sub(r'[/*?:"<>|]', "_", tp._extract_company_name(root)).strip()
        min_d, max_d = tp._voucher_date_range(result.fact_voucher)
        period = tp._format_period(min_d, max_d)
        timestamp = _dt.now().strftime("%Y%m%d_%H%M%S")
        dump_name = f"{company}_transaction_dump_{period}_{timestamp}.csv"

        out_dir.mkdir(parents=True, exist_ok=True)
        dump_path = out_dir / dump_name
        tp.export_flat_fact(dump_path, flat_rows)
        log(f"Transaction dump saved → {dump_path.resolve()}")

        output_files = [str(dump_path.resolve())]

        # ── Step 5: Optional star-schema exports ──────────────────────────────
        if export_dims:
            for dim in export_dims:
                dim_key = dim.upper()
                if dim_key in result.dimensions:
                    p = out_dir / f"dim_{dim.lower()}.csv"
                    tp.write_csv_rows(p, result.dimensions[dim_key])
                    output_files.append(str(p.resolve()))
                    log(f"Dimension table written → {p.name}")

        if "voucher" in export_facts:
            p = out_dir / "fact_voucher.csv"
            tp.write_csv_rows(p, result.fact_voucher)
            output_files.append(str(p.resolve()))
            log(f"fact_voucher.csv written → {p.name}")

        if "ledger_entry" in export_facts:
            p = out_dir / "fact_ledger_entry.csv"
            tp.write_csv_rows(p, result.fact_ledger_entry)
            output_files.append(str(p.resolve()))
            log(f"fact_ledger_entry.csv written → {p.name}")

        if "inventory_line" in export_facts:
            p = out_dir / "fact_inventory_line.csv"
            tp.write_csv_rows(p, result.fact_inventory_line)
            output_files.append(str(p.resolve()))
            log(f"fact_inventory_line.csv written → {p.name}")

        log(
            f"✅ Extraction complete! "
            f"{len(flat_rows):,} rows | {len(output_files)} file(s) written."
        )
        jobs.set_output_files(job_id, output_files)
        jobs.set_status(job_id, jobs.COMPLETED)

    except Exception as exc:  # noqa: BLE001
        err_msg = str(exc)
        logger.exception("Extraction job %s failed", job_id)
        jobs.append_log(job_id, f"❌ ERROR: {err_msg}")
        jobs.set_error(job_id, err_msg)
        jobs.set_status(job_id, jobs.FAILED)

    finally:
        root_logger.removeHandler(handler)
        tp_logger.removeHandler(handler)


def start_extraction_job(job_id: str, params: dict[str, Any]) -> None:
    """Spawn a background thread to run the extraction and return immediately."""
    t = threading.Thread(
        target=run_extraction,
        args=(job_id, params),
        daemon=True,
        name=f"tally-job-{job_id[:8]}",
    )
    t.start()

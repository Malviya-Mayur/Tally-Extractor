"""
pipeline_runner.py — Background thread executor for the Tally Pipeline.

Imports functions from Tally_Pipeline.py (one level up) and runs them
in a dedicated thread so the FastAPI server is never blocked.

Supports two source modes:
  1. Live Tally API  — fetches XML directly from the running Tally instance.
  2. XML file upload — reads a previously-saved XML file (params["xml_file"]).

Output is always a single .xlsx workbook with two sheets:
  • "Data"           — flat transaction dump rows
  • "Extraction Log" — timestamped log lines for the job
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


def _write_xlsx(xlsx_path: Path, flat_rows: list[dict], log_lines: list[str]) -> None:
    """Write a two-sheet Excel workbook: 'Data' and 'Extraction Log'."""
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    wb = openpyxl.Workbook()

    # ── Sheet 1: Data ─────────────────────────────────────────────────────────
    ws_data = wb.active
    ws_data.title = "Data"

    header_fill = PatternFill("solid", fgColor="1E3A5F")
    header_font = Font(bold=True, color="FFFFFF")

    if flat_rows:
        headers = list(flat_rows[0].keys())
        for col_idx, header in enumerate(headers, 1):
            cell = ws_data.cell(row=1, column=col_idx, value=header)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center")

        for row_idx, row in enumerate(flat_rows, 2):
            for col_idx, key in enumerate(headers, 1):
                ws_data.cell(row=row_idx, column=col_idx, value=row.get(key))

        # Auto-fit column widths (cap at 50)
        for col_idx, header in enumerate(headers, 1):
            max_len = max(
                len(str(header)),
                *(len(str(row.get(header, "") or "")) for row in flat_rows[:200]),
            )
            ws_data.column_dimensions[get_column_letter(col_idx)].width = min(max_len + 2, 50)
    else:
        ws_data.cell(row=1, column=1, value="No data rows were produced.")

    # ── Sheet 2: Extraction Log ───────────────────────────────────────────────
    ws_log = wb.create_sheet("Extraction Log")
    log_header_fill = PatternFill("solid", fgColor="2D2D2D")
    log_header_font = Font(bold=True, color="FFFFFF")

    hdr = ws_log.cell(row=1, column=1, value="Extraction Log")
    hdr.fill = log_header_fill
    hdr.font = log_header_font
    ws_log.column_dimensions["A"].width = 120

    for row_idx, line in enumerate(log_lines, 2):
        ws_log.cell(row=row_idx, column=1, value=line)

    wb.save(xlsx_path)


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def run_extraction(job_id: str, params: dict[str, Any]) -> None:
    """
    Execute the full Tally Pipeline for a given job_id using params dict.

    params keys:
      from_date       str  YYYYMMDD  (required for live mode)
      to_date         str  YYYYMMDD  (required for live mode)
      port            int  Tally HTTP port (default 9000, live mode only)
      out_dir         str  Output directory path
      retries         int  Max HTTP retry attempts (live mode only)
      timeout         int  HTTP timeout in seconds (live mode only)
      export_dims     list[str]  Dimension table names to export (e.g. ["LEDGER"])
      export_facts    list[str]  Fact table names: "voucher", "ledger_entry", "inventory_line"
      xml_file        str  (optional) Path to an already-extracted XML file. When
                           provided, the live Tally API call is skipped entirely.
    """
    jobs.set_status(job_id, jobs.RUNNING)

    out_dir = Path(params.get("out_dir", "tally_out"))
    out_dir.mkdir(parents=True, exist_ok=True)

    # In-memory log collector for the Excel sheet
    collected_log_lines: list[str] = []

    # Set up a dedicated log handler that writes to the job store
    handler = _JobLogHandler(job_id)
    handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))

    tp_logger = logging.getLogger("__main__")  # Tally_Pipeline uses root logger
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)
    tp_logger.addHandler(handler)

    def log(msg: str) -> None:
        jobs.append_log(job_id, msg)
        collected_log_lines.append(msg)

    try:
        xml_file: str | None = params.get("xml_file")
        export_dims: list[str] = params.get("export_dims", [])
        export_facts: list[str] = params.get("export_facts", [])

        # ── Step 1: Obtain XML bytes ──────────────────────────────────────────
        import xml.etree.ElementTree as ET

        if xml_file:
            # --- XML Upload Mode ---
            xml_path = Path(xml_file)
            log(f"Reading XML from uploaded file: {xml_path.name} …")
            raw_bytes = xml_path.read_bytes()
            log(f"Read {len(raw_bytes):,} bytes from file.")
        else:
            # --- Live Tally API Mode ---
            from_date: str = params["from_date"]
            to_date: str = params["to_date"]
            port: int = int(params.get("port", 9000))
            retries: int = int(params.get("retries", 3))
            timeout: int = int(params.get("timeout", 60))

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
            log(f"Received {len(raw_bytes):,} bytes.")

        # ── Step 2: Parse XML ─────────────────────────────────────────────────
        log("Parsing XML …")
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

        # ── Step 4: Optional star-schema exports (CSV) ────────────────────────
        extra_output_files: list[str] = []

        if export_dims:
            for dim in export_dims:
                dim_key = dim.upper()
                if dim_key in result.dimensions:
                    p = out_dir / f"dim_{dim.lower()}.csv"
                    tp.write_csv_rows(p, result.dimensions[dim_key])
                    extra_output_files.append(str(p.resolve()))
                    log(f"Dimension table written → {p.name}")

        if "voucher" in export_facts:
            p = out_dir / "fact_voucher.csv"
            tp.write_csv_rows(p, result.fact_voucher)
            extra_output_files.append(str(p.resolve()))
            log(f"fact_voucher.csv written → {p.name}")

        if "ledger_entry" in export_facts:
            p = out_dir / "fact_ledger_entry.csv"
            tp.write_csv_rows(p, result.fact_ledger_entry)
            extra_output_files.append(str(p.resolve()))
            log(f"fact_ledger_entry.csv written → {p.name}")

        if "inventory_line" in export_facts:
            p = out_dir / "fact_inventory_line.csv"
            tp.write_csv_rows(p, result.fact_inventory_line)
            extra_output_files.append(str(p.resolve()))
            log(f"fact_inventory_line.csv written → {p.name}")

        # ── Step 5: Compose output filename ───────────────────────────────────
        import re
        from datetime import datetime as _dt

        company = re.sub(r'[/*?:"<>|]', "_", tp._extract_company_name(root)).strip()
        min_d, max_d = tp._voucher_date_range(result.fact_voucher)
        period = tp._format_period(min_d, max_d)
        timestamp = _dt.now().strftime("%Y%m%d_%H%M%S")
        xlsx_name = f"{company}_extraction_{period}_{timestamp}.xlsx"

        # ── Step 6: Write Excel workbook ──────────────────────────────────────
        log(f"Writing Excel workbook ({len(flat_rows):,} data rows) …")

        # Collect all log lines generated so far before writing
        final_log_lines = list(jobs.get_job(job_id)["log_lines"]) + collected_log_lines
        # Deduplicate while preserving order
        seen: set[str] = set()
        deduped_log: list[str] = []
        for line in final_log_lines:
            if line not in seen:
                seen.add(line)
                deduped_log.append(line)

        xlsx_path = out_dir / xlsx_name
        _write_xlsx(xlsx_path, flat_rows, deduped_log)
        log(f"✅ Excel workbook saved → {xlsx_path.resolve()}")

        output_files = [str(xlsx_path.resolve())] + extra_output_files

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
        # Clean up uploaded temp XML file if used
        xml_file_path = params.get("xml_file")
        if xml_file_path:
            try:
                Path(xml_file_path).unlink(missing_ok=True)
            except Exception:
                pass


def start_extraction_job(job_id: str, params: dict[str, Any]) -> None:
    """Spawn a background thread to run the extraction and return immediately."""
    t = threading.Thread(
        target=run_extraction,
        args=(job_id, params),
        daemon=True,
        name=f"tally-job-{job_id[:8]}",
    )
    t.start()

# AGENTS.md — Tally Extractor Developer & Agent Reference

This document provides a concise and clear reference for AI agents and developers working on this project. It outlines the codebase layout, pipeline capabilities, execution flows, and integration steps.

---

## 1. Project Map & Architecture

**Tally Extractor** is an offline-first utility that extracts ledger, voucher, and inventory records from Tally Prime/ERP, processing them into structured Excel (`.xlsx`) and CSV outputs.

```
Tally-Extractor/
├── Agent/                     # Project guidelines, PRDs, and stack specifications
│   ├── AGENTS.md              # [This File] Main developer and AI agent reference
│   ├── Product Requirements Document.md
│   └── Technical Stack for Tally.md
├── tally_web/                 # FastAPI Web Server + Vanilla Frontend
│   ├── backend/
│   │   ├── app.py             # REST API routing (extract, status, logs SSE, config)
│   │   ├── jobs.py            # Thread-safe in-memory job registry
│   │   └── pipeline_runner.py # Background thread executor (bridges Web App and V2 Core)
│   ├── frontend/              # Glassmorphic Dark-Mode UI (HTML, CSS, Vanilla JS)
│   │   ├── index.html
│   │   ├── style.css
│   │   └── script.js
│   ├── config.yaml            # Server defaults and timeouts configuration
│   ├── start.sh / start.bat   # Cross-platform application startup scripts
│   └── requirements.txt       # Python web application dependencies
├── Tally_Pipeline.py          # Legacy V1 pipeline (In-memory, single fetch)
└── Tally_Pipeline_V2.py       # Production V2 pipeline (Chunked monthly requests, SQLite buffers)
```

---

## 2. Core Execution Flows

### 2.1. Live Extraction Pipeline (V2)
1. **Divide Range**: The date range is split into monthly chunks via `_month_chunks()` to keep Tally's XML payloads small and prevent RAM overload.
2. **Fetch**: Each month is fetched from Tally's local server (default: port `9000`) using `fetch_tally_xml_bytes()` with exponential backoff retries.
3. **Parse & Index**:
   - `collect_all_masters()` parses the XML tree in a single pass.
   - Pre-indexes ledger masters (`opening_balance`, `gstin`, `pan`, `msme_enterprise_category`, `udyam_registration_number`, etc.) and element pointers (`le_elem`, `inv_elem`) to avoid O(N²) parent lookups.
4. **Buffer to SQLite**: The script inserts the parsed rows directly into a temporary SQLite database on disk (`tally_pipeline_v2.db`) rather than keeping large collections in memory.
5. **Streaming Export**: `export_db_to_csv()` streams rows from SQLite to a temporary CSV.
6. **Excel Conversion**: `pipeline_runner.py` uses `openpyxl` to read the temporary CSV and compiles a premium dual-sheet `.xlsx` workbook containing:
   - Sheet 1: **"Data"** (fully formatted fact rows).
   - Sheet 2: **"Extraction Log"** (complete session logs).
7. **Clean up**: Deletes all temporary SQLite and CSV files.

### 2.2. Offline XML Upload Pipeline
1. The user uploads an XML dump through `POST /api/upload-xml`.
2. The web server saves it to `/tmp` and issues a token.
3. Upon triggering extraction, the runner skips the HTTP request steps, loads the file directly, and runs the standard parse-index-buffer-export cycle.

---

## 3. Web Service API Reference

The FastAPI server binds to `127.0.0.1:8888` (or port specified in `config.yaml` / `app.py`).

| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `POST` | `/api/extract` | Begins background thread extraction. Returns `{job_id, message}`. |
| `POST` | `/api/upload-xml` | Uploads a local Tally XML file. Returns an `xml_token` (temp filepath). |
| `GET` | `/api/status/{job_id}`| Returns current status (`pending`, `running`, `completed`, `failed`), output paths, and log lines. |
| `GET` | `/api/logs/{job_id}` | EventSource connection returning live logs (SSE). |
| `GET` | `/api/download/{job_id}/{filename}` | Serves the generated spreadsheet. |
| `GET` | `/api/config` | Retrieves default configurations. |
| `POST` | `/api/config` | Updates default configurations in `config.yaml`. |
| `GET` | `/api/browse-folder` | Triggers a server-side native OS directory selection dialog (via tkinter). |

---

## 4. Key Functions to Know

- **`Tally_Pipeline_V2.py`**:
  - `fetch_tally_xml_bytes(from_date, to_date, port, max_retries, timeout)`: Connects to Tally HTTP server.
  - `collect_all_masters(root)`: Gathers currency, group, ledger, stock, godown, unit, and voucher types.
  - `process_chunk_to_db(root, masters, db_conn)`: Resolves flattening and logs rows into SQLite.
  - `export_db_to_csv(db_path, csv_path)`: Streams data out to a file.
- **`tally_web/backend/pipeline_runner.py`**:
  - `run_extraction(job_id, params)`: Instantiates a thread, captures logs, calls V2 parser functions, builds Excel file.
- **`tally_web/backend/jobs.py`**:
  - `append_log(job_id, line)` / `set_status(job_id, status)`: Thread-safe in-memory job operations.

---

## 5. Setup & Development Gotchas

- **Tally Configuration**: The custom report `APIRawVouchers` must be defined and loaded into Tally Prime. The TDL configuration is located in `API_Extractor.txt`.
- **Port Mapping**: Tally's port (normally `9000`) must match the value configured in the Web UI.
- **Python Version**: Recommend Python 3.10+ due to type hint unions (`|`).
- **Dependencies**: Ensure `openpyxl` (for Excel exporting), `requests` (for Tally queries), and `pyyaml` (for configuration parsing) are installed. Installing `lxml` is optional but highly recommended to accelerate XML parsing by 3-5x.

# Product Requirements Document (PRD)
## Tally Pipeline Web Interface – Interactive Tally Data Extraction

**Version:** 1.0  
**Date:** 2026-04-05  
**Status:** Draft  

---

## 1. Executive Summary

The **Tally Pipeline Web Interface** is a browser‑based application that wraps the existing Python‑based `Tally_Pipeline.py` script. It allows non‑technical users to interactively extract transaction data from Tally Prime, preview results, and download CSV exports without using a command line. The system will run on a local network machine (same as Tally) and expose a simple web UI that communicates with the backend via REST API. All processing remains on the same host; no data is sent to external servers.

---

## 2. Goals & Objectives

- **Goal 1:** Provide a user‑friendly interface for Tally data extraction (date range, optional parameters, progress feedback).  
- **Goal 2:** Eliminate command‑line dependency – users can trigger extraction and download files via a web browser.  
- **Goal 3:** Keep all existing functionality of `Tally_Pipeline.py` (fetch, parse, star‑schema, flat fact dump, timestamped output).  
- **Goal 4:** Maintain security – no external cloud, only localhost access, optional authentication.  
- **Goal 5:** Support both interactive and batch (API) usage patterns.

---

## 3. Scope

### 3.1 In Scope

- Web UI built with HTML/CSS/JavaScript (or a lightweight framework like Vue/React).  
- Backend service (Python + Flask/FastAPI) that calls the existing `Tally_Pipeline.py` logic (or refactors it into a reusable module).  
- Real‑time logging/progress streaming from the extraction process to the frontend.  
- Download of generated CSV files (transaction dump and optional star‑schema files).  
- Configuration persistence (last used dates, output directory, port) using browser local storage or server‑side config file.  
- Error handling and user‑friendly error messages.  
- Support for the same command‑line arguments via UI controls (date range, port, output directory, retries, timeout, no‑prompt).  

### 3.2 Out of Scope

- Multi‑user or cloud hosting – the service is intended for single‑user on a local machine.  
- Direct editing of Tally data – extraction only.  
- Support for Tally versions older than Tally Prime (API may vary).  
- Mobile responsive design (desktop first, but basic responsiveness is nice to have).  

---

## 4. User Personas

| Persona | Description | Needs |
|---------|-------------|-------|
| **Accountant** | Non‑technical finance user | Simple date picker, one‑click extract, automatic download of Excel/CSV. |
| **System Integrator** | Technical person who needs automation | API endpoint to trigger extraction, JSON response, ability to pass parameters. |
| **Manager** | Wants to review periodic reports | Schedule extraction (future enhancement) or run on demand with consistent naming. |

---

## 5. Functional Requirements

### 5.1 Extraction Workflow

| ID | Requirement | Priority |
|----|-------------|----------|
| FR‑01 | User can enter **start date** and **end date** (YYYY-MM-DD picker, internally converted to YYYYMMDD). | P0 |
| FR‑02 | User can optionally override **Tally HTTP port** (default 9000). | P1 |
| FR‑03 | User can specify **output directory** (absolute or relative path) where CSV files will be saved. | P1 |
| FR‑04 | User can set **retries** (1‑5) and **timeout** (seconds). | P2 |
| FR‑05 | User can choose to **export additional star‑schema tables** (dimensions, fact_voucher, fact_ledger_entry, fact_inventory_line) via checkboxes. | P0 |
| FR‑06 | A **“Start Extraction”** button triggers the backend process. | P0 |
| FR‑07 | During extraction, the UI shows a **live log** (stdout/stderr streaming) so user sees progress. | P0 |
| FR‑08 | After successful extraction, the UI provides **download links** for each generated CSV file. | P0 |
| FR‑09 | The backend saves the transaction dump with a **timestamped filename** (e.g., `Company_transaction_dump_Mar-26_20250406_143052.csv`). | P0 |
| FR‑10 | If extraction fails, the UI displays the error message and a “Retry” button. | P0 |

### 5.2 Configuration & Persistence

| ID | Requirement | Priority |
|----|-------------|----------|
| FR‑11 | The UI remembers last used **date range**, **port**, **output directory** (via browser localStorage). | P1 |
| FR‑12 | Option to **reset to defaults**. | P2 |
| FR‑13 | A settings panel to change default Tally connection parameters (host, port). | P2 |

### 5.3 API Endpoints (for headless / integration)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/extract` | POST | Accepts JSON: `{from_date, to_date, port, out_dir, retries, timeout, export_star_schema: {dimensions: [...], facts: [...]}}`. Returns `{job_id, message}`. |
| `/api/status/<job_id>` | GET | Returns job status (`pending`, `running`, `completed`, `failed`) and log lines. |
| `/api/download/<job_id>/<filename>` | GET | Downloads a generated CSV file. |
| `/api/config` | GET/POST | Get or set server‑side defaults. |

**Note:** For simplicity, the first version can use a synchronous endpoint with streaming response (Server‑Sent Events or WebSocket for logs). Alternatively, implement async job queue with background thread.

---

## 6. Non‑Functional Requirements

| ID | Requirement | Target |
|----|-------------|--------|
| NFR‑01 | **Performance** – Extraction time same as command‑line version (overhead < 5%). | |
| NFR‑02 | **Security** – The web server binds only to `127.0.0.1` (localhost) by default. Optional basic authentication can be enabled via environment variable. | |
| NFR‑03 | **Reliability** – Process must handle Tally timeouts, malformed XML, and disk write errors gracefully. | |
| NFR‑04 | **Usability** – UI should be intuitive, with clear progress indicator (spinner + log stream). | |
| NFR‑05 | **Portability** – Backend should run on Windows (main target) and Linux/macOS (optional). | |
| NFR‑06 | **Logging** – Server logs to file (`web_pipeline.log`) and also streams to frontend. | |

---

## 7. Technical Architecture

### 7.1 High‑Level Diagram

```
[Browser]  <--HTTP/WS-->  [Flask/FastAPI Server]  --calls-->  [Tally_Pipeline Core (Python module)]
                                                              |
                                                              +--> [Tally HTTP API] (port 9000)
                                                              |
                                                              +--> [File System] (CSV output)
```

### 7.2 Technology Stack

| Layer | Choice | Reason |
|-------|--------|--------|
| Frontend | HTML5, Tailwind CSS, vanilla JavaScript (or Vue.js) | Lightweight, no build step required. |
| Backend | Python 3.10+ with **FastAPI** (or Flask) | FastAPI provides async streaming, OpenAPI docs, easy integration. |
| Process Execution | `subprocess` or refactor `Tally_Pipeline` into callable functions | Refactoring into a library is cleaner; but initially wrap the script with `subprocess.Popen` to capture stdout/stderr. |
| Log Streaming | Server‑Sent Events (SSE) or WebSocket | SSE is simpler for one‑way log streaming. |
| Concurrency | Background thread per job (with job queue) | Avoid blocking the server. |

### 7.3 Data Flow

1. User submits form → Frontend POSTs to `/api/extract`.
2. Server creates a job ID, spawns a background thread that executes `Tally_Pipeline` with appropriate arguments (or calls refactored functions).
3. Server streams logs via SSE endpoint `/api/logs/<job_id>`.
4. Upon completion, server writes job metadata (list of output files) to memory/file.
5. Frontend polls `/api/status/<job_id>` or receives final event, then displays download links.

### 7.4 File Structure

```
tally_web/
├── backend/
│   ├── app.py                 # FastAPI/Flask application
│   ├── pipeline_runner.py     # Wrapper to call Tally_Pipeline logic
│   ├── jobs.py                # Job store (in-memory dict)
│   └── templates/             # (if using server-side templates)
├── frontend/
│   ├── index.html
│   ├── style.css
│   └── script.js
├── Tally_Pipeline.py          # Original script (may be refactored)
└── requirements.txt
```

---

## 8. User Interface Mock‑up (Text Description)

```
+---------------------------------------------------+
|  TALLY PIPELINE WEB EXTRACTOR                     |
+---------------------------------------------------+
|  Date Range:                                      |
|    [Start Date: 2026-04-01]  [End Date: 2026-03-31] |
|                                                    |
|  Tally Settings:                                   |
|    Port: [9000]          Output Dir: [./tally_out] |
|    Retries: [3]          Timeout: [60] sec         |
|                                                    |
|  Additional Exports:                               |
|    ☐ Dimension tables (dim_ledger, dim_group...)   |
|    ☐ fact_voucher.csv                              |
|    ☐ fact_ledger_entry.csv                         |
|    ☐ fact_inventory_line.csv                       |
|                                                    |
|  [ START EXTRACTION ]                              |
|                                                    |
+---------------------------------------------------+
|  EXTRACTION LOG:                                   |
|  [2026-04-05 14:30:22] Fetching data from Tally... |
|  [2026-04-05 14:30:25] Received 1.2 MB             |
|  [2026-04-05 14:30:26] Parsing XML...              |
|  [2026-04-05 14:30:28] Building flat fact table... |
|  [2026-04-05 14:30:29] CSV saved to ./tally_out/...|
|                                                    |
|  ✅ Extraction complete!                           |
|  Download files:                                   |
|    • MyCo_transaction_dump_Apr-25_20260405_143029.csv |
|    • dim_ledger.csv (optional)                     |
+---------------------------------------------------+
```

---

## 9. Error Handling

| Scenario | UI Response |
|----------|-------------|
| Tally not running or port unreachable | Show error message with checklist (is Tally open? is HTTP server enabled?). |
| Invalid date range | Highlight date fields, show tooltip. |
| Disk full or permission denied | Show error and suggest changing output directory. |
| XML parse error | Log raw snippet, suggest checking TDL report. |
| Timeout during extraction | Allow user to increase timeout value and retry. |

---

## 10. Implementation Phases

### Phase 1 – Core Web Wrapper (MVP)
- Refactor `Tally_Pipeline.py` into a callable module (functions `extract_to_csv(from_date, to_date, out_dir, ...)`).  
- Build a simple Flask/FastAPI server with a single synchronous endpoint.  
- Create minimal HTML form with date picker and start button.  
- Show plain text log after extraction finishes (no streaming).  
- Provide download link for the main transaction dump.  

### Phase 2 – Enhanced UX
- Add live log streaming via SSE.  
- Implement background job queue (so UI doesn’t freeze).  
- Add configuration persistence (localStorage).  
- Add checkboxes for star‑schema exports.  

### Phase 3 – Production Readiness
- Basic authentication (optional).  
- Support for multiple simultaneous extractions (job queue with limits).  
- API documentation (Swagger UI).  
- Windows executable wrapper (optional, for non‑Python users).  

---

## 11. Success Metrics

- **User satisfaction:** Ability to complete extraction without touching command line.  
- **Reliability:** 95%+ success rate under normal Tally operation.  
- **Performance:** Extraction time within 10% of original script.  
- **Adoption:** Internal finance team uses it for monthly reporting.

---

## 12. Open Questions / Risks

| Risk | Mitigation |
|------|-------------|
| Tally’s TDL report `APIRawVouchers` may not be loaded. | Provide a one‑time setup guide in UI (or automate loading via Tally’s file import). |
| Long‑running extraction could cause HTTP timeouts. | Use async job pattern with polling; increase server timeout. |
| Concurrent extractions may overload Tally. | Limit to one job at a time (queue). |
| Browser may block downloads if pop‑up is prevented. | Use direct link with `download` attribute; instruct user to allow. |

---

## 13. Appendix

### A. Example API Request

**POST /api/extract**  
```json
{
  "from_date": "20250401",
  "to_date": "20260331",
  "port": 9000,
  "out_dir": "C:\\tally_exports",
  "retries": 3,
  "timeout": 120,
  "export_star_schema": {
    "dimensions": ["LEDGER", "GROUP"],
    "facts": ["voucher", "ledger_entry"]
  }
}
```

**Response:**
```json
{
  "job_id": "abc-123",
  "message": "Extraction started"
}
```

### B. Configuration File (server‑side)

Default `config.yaml`:
```yaml
tally:
  host: localhost
  port: 9000
  default_from: 20250401
  default_to: 20260331
output:
  directory: ./tally_out
  timestamp: true
server:
  bind: 127.0.0.1
  port: 8080
  auth_enabled: false
```

---

## 14. Approval

| Role | Name | Date | Signature |
|------|------|------|------------|
| Product Owner | | | |
| Tech Lead | | | |
| QA Lead | | | |

---

**End of PRD**

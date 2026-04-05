# Technical Stack for Tally Pipeline Web Interface

| **Language / Module** | **Where Used** | **Purpose** |
|----------------------|----------------|-------------|
| **Python 3.10+** | Backend (FastAPI server, job runner) | Main backend language; runs the extraction logic, manages jobs, streams logs. |
| **FastAPI** | Backend (`app.py`) | REST API framework for endpoints (`/api/extract`, `/api/status`, `/api/download`); provides automatic OpenAPI docs. |
| **Uvicorn** | Backend server (start script) | ASGI server to run FastAPI in production. |
| **sse-starlette** | Backend (`/api/logs` endpoint) | Server‑Sent Events (SSE) for live log streaming to frontend. |
| **python-multipart** | Backend (form data handling) | Parse form data if using HTML forms instead of JSON. |
| **Pydantic** | Backend (request/response models) | Validate incoming JSON payloads (dates, port, options). |
| **threading / asyncio** | Backend job manager | Run extraction in background thread without blocking API responses. |
| **uuid** | Backend job store | Generate unique `job_id` for each extraction request. |
| **datetime** | Backend & frontend | Timestamp for filenames and job creation times. |
| **HTML5** | Frontend (`index.html`) | Structure of the web page (form, log container, download links). |
| **CSS3** | Frontend (`style.css`) | Styling (layout, colors, spinners, responsive design). |
| **Tailwind CSS** | Frontend (optional, via CDN) | Utility-first CSS framework for rapid UI development. |
| **JavaScript (ES6)** | Frontend (`script.js`) | DOM manipulation, form submission, fetch API calls, SSE event handling, localStorage persistence. |
| **Fetch API** | Frontend (JavaScript) | HTTP requests to backend endpoints (POST extract, GET status, GET download). |
| **EventSource API** | Frontend (JavaScript) | Native browser API to receive Server‑Sent Events (live logs). |
| **localStorage** | Frontend (JavaScript) | Persist user preferences (last used dates, port, output directory) across sessions. |
| **Blob / URL.createObjectURL** | Frontend (JavaScript) | Handle CSV file downloads dynamically. |

---

## Optional / Alternative Choices

| **Alternative** | **Instead of** | **Reason** |
|----------------|----------------|-------------|
| **Flask + Flask-SSE** | FastAPI + sse-starlette | Simpler for developers familiar with Flask; but FastAPI provides better async performance. |
| **React / Vue.js** | Vanilla JS | For more complex UI state management; adds build step overhead. |
| **SQLite** | In‑memory job dict | Persist job history across server restarts (future enhancement). |
| **WebSockets (Socket.IO)** | SSE | Bi‑directional communication; more complex than SSE for one‑way logs. |
| **Django + Channels** | FastAPI | Heavy‑weight; overkill for this use case. |

---

## Summary Diagram

```
Browser (HTML/CSS/JS)
   │
   │ Fetch API / EventSource
   ▼
FastAPI (Python)
   ├── /api/extract  → creates job, starts background thread
   ├── /api/logs     → SSE stream
   ├── /api/status   → polling endpoint
   └── /api/download → serves CSV files
   │
   │ calls
   ▼
Tally_Pipeline.py (refactored functions)
   │
   ├── fetch_tally_xml_bytes()
   ├── parse_tally_star_schema()
   └── build_flat_fact_rows()
   │
   ▼
Tally Prime (HTTP port 9000) + CSV files on disk
```

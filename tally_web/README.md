# Tally Pipeline Web Interface

A browser-based wrapper for `Tally_Pipeline.py` that lets non-technical users extract Tally Prime transaction data without using command line.

---

## Quick Start

### Linux / macOS

```bash
cd tally_web
bash start.sh
```

### Windows

Double-click `start.bat` in the `tally_web/` folder, or run it from Command Prompt.

Then open your browser at **http://127.0.0.1:8080**

---

## Manual Start

```bash
cd tally_web
pip install -r requirements.txt
python -m uvicorn backend.app:app --host 127.0.0.1 --port 8080 --reload
```

---

## Prerequisites

1. **Python 3.10+** installed
2. **Tally Prime** running on the same machine
3. **TDL report `APIRawVouchers` loaded** in Tally (one-time setup):
   - In Tally Prime → Gateway of Tally → F12 Configuration → Load TDL Files
   - Point it to your `.tdl` file that defines the `APIRawVouchers` report
4. **Tally HTTP Server enabled**:
   - In Tally Prime → F12 → Configure → Enable TallyPrime Server → Yes
   - Default port: **9000**

---

## Features

| Feature | Details |
|---------|---------|
| Date range picker | Select from/to dates visually |
| Live log streaming | Server-Sent Events stream logs in real-time |
| Star-schema exports | Optional dimension and fact table CSVs |
| Download links | Click to download generated CSV files |
| Settings persistence | Form values saved in browser localStorage |
| Settings modal | Configure Tally host/port defaults |

---

## Project Structure

```
tally_web/
├── backend/
│   ├── __init__.py
│   ├── app.py              # FastAPI application (all REST endpoints)
│   ├── jobs.py             # Thread-safe in-memory job store
│   └── pipeline_runner.py  # Background thread executor
├── frontend/
│   ├── index.html          # Main UI page
│   ├── style.css           # Premium dark-mode styles
│   └── script.js           # Frontend logic (SSE, fetch, localStorage)
├── config.yaml             # Server-side defaults
├── requirements.txt        # Python dependencies
├── start.sh                # Linux/macOS launcher
└── start.bat               # Windows launcher
```

---

## API Reference

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/extract` | POST | Start an extraction job |
| `/api/status/<job_id>` | GET | Get job status + logs + output files |
| `/api/logs/<job_id>` | GET (SSE) | Live log stream |
| `/api/download/<job_id>/<filename>` | GET | Download a CSV file |
| `/api/config` | GET | Read server defaults |
| `/api/config` | POST | Update server defaults |

Interactive API docs (Swagger UI): **http://127.0.0.1:8080/docs**

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `Connection refused` on Tally port | Ensure Tally Prime is running and HTTP Server is enabled |
| `APIRawVouchers` not found | Load the TDL file in Tally → F12 → TDL Config |
| Very small XML response | Confirm a company is open in Tally and the period has transactions |
| Permission denied on output dir | Change output directory to a path you have write access to |

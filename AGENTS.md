# AGENTS.md — Tally Extractor

## Project Overview

**Tally Extractor** is a single-file Python pipeline (`Tally_Pipeline.py`) that fetches accounting data from Tally Prime via its TDL HTTP API, parses the XML response in memory, and exports it as CSV files in a star-schema and flat fact-table format.

## Architecture

The script is organized into 5 sections:

| Section | Purpose |
|---------|---------|
| 1 | XML helpers & star-schema parsing |
| 2 | Tally HTTP API fetch with retry logic |
| 3 | Flat fact table builder (transaction dump) |
| 4 | Output filename helpers (company name, date range, timestamp) |
| 5 | CLI entry point (`main()`) with argparse |

## Key Data Flow

```
Tally Prime (HTTP/XML) → raw bytes → XML parse → StarSchemaResult → Flat Fact Rows → CSV
```

### Outputs

- **Transaction dump** (always exported): `{company}_transaction_dump_{period}_{timestamp}.csv` — 77 columns of enriched ledger-level transaction data
- **Star-schema CSVs** (optional, user-selected): `dim_{name}.csv`, `fact_voucher.csv`, `fact_ledger_entry.csv`, `fact_inventory_line.csv`

## Running the Pipeline

```bash
# Interactive mode (prompts for dates)
python Tally_Pipeline.py

# Non-interactive / batch mode
python Tally_Pipeline.py --from 20250401 --to 20260331 --out ./out --no-prompt
```

### CLI Arguments

| Flag | Default | Description |
|------|---------|-------------|
| `--from` | (prompted) | Start date YYYYMMDD |
| `--to` | (prompted) | End date YYYYMMDD |
| `--port` | 9000 | Tally HTTP port |
| `--out` | `./tally_out` | Output directory |
| `--prefix` | `""` | Filename prefix for star-schema CSVs |
| `--retries` | 3 | Max HTTP retry attempts |
| `--timeout` | 60 | HTTP timeout in seconds |
| `--no-prompt` | false | Skip interactive prompts |

## Dependencies

- **stdlib**: `xml.etree.ElementTree`, `csv`, `argparse`, `logging`, `decimal`, `dataclasses`, `datetime`, `calendar`, `re`, `json`, `time`, `pathlib`, `typing`
- **third-party**: `requests`

## Key Concepts

### Star Schema Dimensions

The parser extracts these dimension tables from Tally XML: `CURRENCY`, `GROUP`, `LEDGER`, `STOCKGROUP`, `STOCKITEM`, `UNIT`, `GODOWN`, `VOUCHERTYPE`, `TAXUNIT`, `COMPANY`.

### Voucher Entry Modes

The pipeline handles two Tally voucher modes:
- **Item Invoice** — inventory entries at voucher level with embedded accounting allocations
- **Standard (As Voucher)** — ledger entries with nested inventory allocations

### Flat Fact Columns (77 total)

The transaction dump enriches each ledger line with: voucher metadata, party ledger info, GST/PAN/MSME details, e-way bill/e-invoice data, inventory details, bill allocations, batch details, and address information.

## Important Functions

| Function | Line | Purpose |
|----------|------|---------|
| `fetch_tally_xml_bytes` | ~471 | HTTP POST to Tally with exponential backoff retry |
| `parse_xml_from_bytes` | ~421 | Decode UTF-16/UTF-8, sanitize, parse XML |
| `parse_tally_star_schema` | ~380 | Extract dimensions + fact tables from XML |
| `build_flat_fact_rows` | ~1186 | Enrich ledger lines with master data into flat rows |
| `export_star_schema` | ~452 | Write dimension/fact CSVs |
| `export_flat_fact` | ~1366 | Write transaction dump CSV |
| `main` | ~1435 | CLI entry point orchestrating the full pipeline |

## Logging

Logs are written to both stdout and `tally_pipeline.log` at INFO level.

## Conventions

- No intermediate XML files are written to disk — all processing is in-memory
- Dates from Tally are in YYYYMMDD format (8-digit strings)
- Amounts use `Decimal` for precision; Tally uses negative-for-credit convention
- XML namespace prefixes are stripped via `strip_ns()`
- List elements (`.LIST` suffix) are handled specially to avoid flattening nested structures

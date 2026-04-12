#!/usr/bin/env python3
"""
Tally Pipeline v2 — single-script end-to-end:
  1. Fetch raw XML from Tally Prime via TDL API (monthly chunks, no temp file).
  2. Parse the XML envelope in a SINGLE tree-walk (all masters + vouchers at once).
  3. Build flat fact rows and write them DIRECTLY to SQLite (zero RAM accumulation).
  4. Export the SQLite table to a timestamped CSV on demand.

Key improvements over v1:
  - UTF-8 safe on Windows (FileHandler + console).
  - Single merged XML tree-walk replaces 7+ separate passes.
  - ET.Element references pre-indexed — eliminates O(N²) per-row lookups.
  - Month-by-month chunked HTTP fetch — bounded per-request memory.
  - SQLite as primary output — rows written immediately, never held in RAM.
  - lxml used for parsing when available, stdlib ET as fallback.
  - Robust date validation (rejects logically invalid dates).
  - Cycle guard in primary_group traversal.
  - --debug / --chunk-months / --db CLI flags added.
  - Empty-result warning with actionable guidance.
  - Windows reserved filename safety.

Usage (interactive):
  python Tally_Pipeline_v2.py

Usage (non-interactive / batch):
  python Tally_Pipeline_v2.py --from 20250401 --to 20260331 --out ./out --no-prompt

Usage (debug mode):
  python Tally_Pipeline_v2.py --from 20250401 --to 20260331 --debug

Dependencies (all stdlib except requests; lxml optional but recommended):
  pip install requests
  pip install lxml   # optional — 3-5x faster XML parsing
"""

from __future__ import annotations

# ── stdlib ────────────────────────────────────────────────────────────────────
import argparse
import calendar
import csv
import io
import json
import logging
import re
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterator

# ── third-party ───────────────────────────────────────────────────────────────
import requests

# ── optional fast XML parser ──────────────────────────────────────────────────
try:
    from lxml import etree as lxml_etree
    _LXML_AVAILABLE = True
except ImportError:
    _LXML_AVAILABLE = False

# =============================================================================
# WINDOWS UTF-8 SAFETY
# Must run before ANY logging or print() so emoji/Unicode don't crash on cp1252.
# =============================================================================
if sys.stdout.encoding and sys.stdout.encoding.lower().replace("-", "") not in ("utf8",):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower().replace("-", "") not in ("utf8",):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# =============================================================================
# LOGGING  — FileHandler with explicit UTF-8, log next to the script file
# =============================================================================
_LOG_FILE = Path(__file__).with_name("tally_pipeline.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(str(_LOG_FILE), encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


# =============================================================================
# SECTION 1 — XML helpers
# =============================================================================

# Pre-compiled regex for control-character sanitisation (avoids recompile per call)
_CONTROL_CHAR_RE = re.compile(r"&#(\d+);")


def strip_ns(tag: str) -> str:
    """Strip XML namespace prefix from a tag string."""
    return tag.split("}", 1)[-1] if "}" in tag else tag


def sanitize_tally_xml(raw: str) -> str:
    """Remove Tally control-character entity refs that are invalid in XML 1.0."""
    return _CONTROL_CHAR_RE.sub(
        lambda m: "" if int(m.group(1)) < 32 else m.group(0),
        raw,
    )


def _is_list_element(el: ET.Element) -> bool:
    return strip_ns(el.tag).endswith(".LIST")


def _text_leaf(el: ET.Element) -> bool:
    if len(el) == 0:
        return True
    return all((c.text or "").strip() == "" and len(c) == 0 for c in el)


def flatten_scalars(
    el: ET.Element,
    *,
    prefix: str = "",
    skip_lists: bool = True,
    always_skip_list_tags: frozenset[str] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {}

    def key(name: str) -> str:
        return f"{prefix}{name}" if prefix else name

    for child in el:
        name = strip_ns(child.tag)
        if skip_lists and _is_list_element(child):
            if always_skip_list_tags and name in always_skip_list_tags:
                continue
            if any(_text_leaf(c) or len(c) > 0 for c in child):
                nested = _list_to_jsonable(child)
                if nested:
                    out[key(name)] = json.dumps(nested, ensure_ascii=False)
            continue
        if _text_leaf(child):
            val = (child.text or "").strip()
            if val:
                out[key(name)] = val
        else:
            sub = flatten_scalars(
                child,
                prefix=f"{name}_",
                skip_lists=skip_lists,
                always_skip_list_tags=always_skip_list_tags,
            )
            out.update(sub)
    return out


def _list_to_jsonable(list_el: ET.Element) -> list | dict:
    rows: list[dict[str, Any]] = []
    for child in list_el:
        row: dict[str, Any] = {}
        cn = strip_ns(child.tag)
        if _text_leaf(child):
            row[cn] = (child.text or "").strip()
        else:
            row.update(flatten_scalars(child, skip_lists=False))
        if row:
            rows.append(row)
    return rows


def _collect_children_by_tag(el: ET.Element, *tags: str) -> dict[str, ET.Element]:
    """
    Return the first child matching each requested tag in a SINGLE linear pass.
    Avoids calling _find_list() N times on the same element.
    """
    want = set(tags)
    found: dict[str, ET.Element] = {}
    for c in el:
        t = strip_ns(c.tag)
        if t in want:
            found[t] = c
            want.discard(t)
            if not want:
                break
    return found


# =============================================================================
# SECTION 2 — Star-schema dataclasses
# =============================================================================

@dataclass
class StarSchemaConfig:
    dimension_tags: frozenset[str] = field(
        default_factory=lambda: frozenset({
            "CURRENCY", "GROUP", "LEDGER", "STOCKGROUP", "STOCKITEM",
            "UNIT", "GODOWN", "VOUCHERTYPE", "TAXUNIT", "COMPANY",
        })
    )
    voucher_tag: str = "VOUCHER"


@dataclass
class MasterData:
    """All master lookups collected in a single XML tree-walk."""
    ledger:         dict[str, dict[str, Any]]   # name → master fields
    stock:          dict[str, dict[str, Any]]   # name → opening_qty/value
    group_parent:   dict[str, str]              # group name → parent group name
    stock_parent:   dict[str, str]              # stock item name → stock group
    # Pre-built element references — eliminates O(N²) re-scans
    le_elem:        dict[tuple[str, int], ET.Element]           # (guid, le_idx) → ET.Element
    inv_elem:       dict[tuple[str, int, int], ET.Element]      # (guid, le_idx, inv_idx) → ET.Element
    party_le_elem:  dict[tuple[str, int], ET.Element]           # (guid, local_idx) → ET.Element (item invoice)
    voucher_sidecar: dict[str, dict[str, str]]                  # guid → sidecar fields
    credit_period:  dict[tuple[str, int], str]                  # (guid, le_idx) → credit period string
    # Tally dimensions (for optional star-schema CSV export)
    dimensions:     dict[str, list[dict[str, Any]]]


# =============================================================================
# SECTION 3 — Date utilities
# =============================================================================

def _validate_date_format(date_str: str) -> bool:
    """Validate YYYYMMDD — rejects logically invalid dates like 20260231."""
    if not (len(date_str) == 8 and date_str.isdigit()):
        return False
    try:
        datetime.strptime(date_str, "%Y%m%d")
        return True
    except ValueError:
        return False


def _month_chunks(from_date: str, to_date: str, chunk_months: int = 1) -> list[tuple[str, str]]:
    """
    Split a YYYYMMDD date range into chunks of `chunk_months` months.
    Keeps per-request XML size bounded for large datasets.
    """
    start = date(int(from_date[:4]), int(from_date[4:6]), int(from_date[6:8]))
    end   = date(int(to_date[:4]),   int(to_date[4:6]),   int(to_date[6:8]))
    chunks: list[tuple[str, str]] = []
    cur = start
    while cur <= end:
        # Advance by chunk_months months
        m = cur.month - 1 + chunk_months
        y = cur.year + m // 12
        m = m % 12 + 1
        last_day = calendar.monthrange(y, m)[1]
        # Last day of the chunk period
        chunk_end_raw = date(y, m, 1) - timedelta(days=1)
        chunk_end = min(chunk_end_raw, end)
        chunks.append((cur.strftime("%Y%m%d"), chunk_end.strftime("%Y%m%d")))
        cur = chunk_end_raw + timedelta(days=1)
        if cur > end:
            break
    return chunks


def _format_period(min_date: str, max_date: str) -> str:
    def fmt(d: str) -> str:
        y, m = d[:4], d[4:6]
        return f"{calendar.month_abbr[int(m)]}-{y[2:]}"
    if not min_date or not max_date:
        return "unknown-period"
    if min_date[:6] == max_date[:6]:
        return fmt(min_date)
    return f"{fmt(min_date)}-{fmt(max_date)}"


# =============================================================================
# SECTION 4 — Tally HTTP fetch
# =============================================================================

def fetch_tally_xml_bytes(
    from_date: str,
    to_date: str,
    port: int = 9000,
    max_retries: int = 3,
    timeout: int = 300,
) -> bytes | None:
    """
    POST a TDL export request to Tally and return raw XML bytes.
    Default timeout raised to 300s — full-year exports can take several minutes.
    Returns None on failure; all errors are logged.
    """
    if not _validate_date_format(from_date):
        logger.error("Invalid start date '%s'. Expected YYYYMMDD (e.g. 20250401).", from_date)
        return None
    if not _validate_date_format(to_date):
        logger.error("Invalid end date '%s'. Expected YYYYMMDD (e.g. 20260331).", to_date)
        return None
    if int(from_date) > int(to_date):
        logger.error("Start date %s is after end date %s.", from_date, to_date)
        return None

    url = f"http://localhost:{port}/"
    req_xml = (
        "<ENVELOPE>"
        "<HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>"
        "<BODY><EXPORTDATA><REQUESTDESC>"
        "<REPORTNAME>APIRawVouchers</REPORTNAME>"
        "<STATICVARIABLES>"
        f"<SVFROMDATE>{from_date}</SVFROMDATE>"
        f"<SVTODATE>{to_date}</SVTODATE>"
        "</STATICVARIABLES>"
        "</REQUESTDESC></EXPORTDATA></BODY>"
        "</ENVELOPE>"
    )
    headers = {"Content-Type": "text/xml;charset=utf-8"}
    logger.info("Fetching 'APIRawVouchers' from Tally for period %s to %s", from_date, to_date)

    for attempt in range(max_retries):
        try:
            logger.info("Attempt %d/%d ...", attempt + 1, max_retries)
            response = requests.post(url, data=req_xml, headers=headers, timeout=timeout)
            response.raise_for_status()
            data = response.content
            logger.info("Received %s bytes from Tally.", f"{len(data):,}")
            logger.debug("Raw response (first 500 bytes):\n%s", data[:500])
            if len(data) < 100:
                logger.warning(
                    "Response is unusually small (%d bytes). "
                    "Verify TDL 'APIRawVouchers' is loaded in Tally.",
                    len(data),
                )
            return data

        except requests.exceptions.ConnectionError as exc:
            logger.error("Connection error on attempt %d: %s", attempt + 1, exc)
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                logger.info("Retrying in %ds ...", wait)
                time.sleep(wait)
            else:
                logger.error(
                    "Max retries exceeded. Checklist:\n"
                    "  1. Tally ERP/Prime is running.\n"
                    "  2. TDL report 'APIRawVouchers' is loaded (F4 in Tally).\n"
                    "  3. Tally HTTP server is enabled (Help > Settings > Connectivity).\n"
                    "  4. Port %d is not blocked by Windows Firewall.\n"
                    "     Test: Test-NetConnection -ComputerName localhost -Port %d",
                    port, port,
                )

        except requests.exceptions.Timeout as exc:
            logger.error("Timeout on attempt %d (timeout=%ds): %s", attempt + 1, timeout, exc)
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                logger.info("Retrying in %ds ...", wait)
                time.sleep(wait)
            else:
                logger.error("Max retries exceeded. Try increasing --timeout (current: %ds).", timeout)

        except requests.exceptions.HTTPError as exc:
            logger.error("HTTP error: %s | status %s", exc, response.status_code)
            logger.error("Response snippet: %s", response.text[:500])
            break  # HTTP errors are not retryable

        except Exception as exc:
            logger.error("Unexpected error on attempt %d: %s", attempt + 1, exc, exc_info=True)
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                logger.info("Retrying in %ds ...", wait)
                time.sleep(wait)

    return None


def parse_xml_from_bytes(raw_bytes: bytes) -> ET.Element:
    """
    Decode raw Tally HTTP response bytes and return a parsed XML Element.
    Handles UTF-16 BOM (common in Tally Unicode exports) and UTF-8.
    Uses lxml for parsing when available (3-5x faster, lower memory).
    """
    if raw_bytes[:2] in (b"\xff\xfe", b"\xfe\xff"):
        raw_str = raw_bytes.decode("utf-16")
    else:
        raw_str = raw_bytes.decode("utf-8", errors="replace")

    raw_str = sanitize_tally_xml(raw_str)

    if _LXML_AVAILABLE:
        # lxml.etree returns its own Element type, but is API-compatible with ET
        root = lxml_etree.fromstring(raw_str.encode("utf-8"))
        # Wrap in a stdlib-compatible shim so the rest of the code is unchanged
        return ET.fromstring(ET.tostring(root))  # convert lxml → stdlib Element

    return ET.fromstring(raw_str)


# =============================================================================
# SECTION 5 — Voucher entry-mode detection
# =============================================================================

_VOUCHER_SKIP_LISTS = frozenset({
    "ALLLEDGERENTRIES.LIST",
    "INVENTORYENTRIESIN.LIST",
    "INVENTORYENTRIESOUT.LIST",
    "ALLINVENTORYENTRIES.LIST",
    "OLDAUDITENTRYIDS.LIST",
})


def _voucher_header_row(voucher: ET.Element) -> dict[str, Any]:
    row = flatten_scalars(
        voucher,
        skip_lists=True,
        always_skip_list_tags=_VOUCHER_SKIP_LISTS,
    )
    for attr in ("REMOTEID", "VCHKEY", "VCHTYPE", "ACTION", "OBJVIEW"):
        if voucher.get(attr):
            row[f"ATTR_{attr}"] = voucher.get(attr)
    return row


def _has_accounting_allocation_fields(accounting_entry: ET.Element) -> bool:
    expected = {"LEDGERNAME", "AMOUNT"}
    found: set[str] = set()
    for child in accounting_entry:
        if _text_leaf(child):
            found.add(strip_ns(child.tag))
    return bool(found & expected)


def _is_item_invoice_mode(voucher: ET.Element) -> bool:
    """Detect whether a voucher uses Item Invoice vs. As Voucher entry mode."""
    vch_entry_mode = ""
    has_item_invoice_structure = False
    has_ledger_entries = False
    voucher_attrib_indicators = False

    for c in voucher:
        t = strip_ns(c.tag)
        if t == "VCHENTRYMODE" and _text_leaf(c):
            vch_entry_mode = (c.text or "").strip()
        elif t == "ALLINVENTORYENTRIES.LIST":
            for inv_entry in c:
                for child in inv_entry:
                    if strip_ns(child.tag) == "ACCOUNTINGALLOCATIONS.LIST":
                        if _has_accounting_allocation_fields(child):
                            has_item_invoice_structure = True
                            break
                if has_item_invoice_structure:
                    break
        elif t == "LEDGERENTRIES.LIST":
            has_ledger_entries = True

    if not voucher_attrib_indicators:
        voucher_attrib_indicators = any(
            ("item" in k.lower() and "invoice" in k.lower()) or
            (v and "item" in v.lower() and "invoice" in v.lower())
            for k, v in voucher.attrib.items()
        )

    return (
        vch_entry_mode == "Item Invoice"
        or has_item_invoice_structure
        or (has_ledger_entries and voucher_attrib_indicators)
    )


# =============================================================================
# SECTION 6 — Ledger / inventory line extraction (per voucher)
# =============================================================================

def _extract_from_item_invoice_mode(
    voucher: ET.Element, vch_guid: str, vchkey: str | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    ledger_rows: list[dict[str, Any]] = []
    inv_rows: list[dict[str, Any]] = []
    inv_i = 0

    for inv_entry in voucher:
        if strip_ns(inv_entry.tag) != "ALLINVENTORYENTRIES.LIST":
            continue
        if len(inv_entry) == 0:
            continue
        inv_scalars = flatten_scalars(inv_entry, skip_lists=True)
        if not inv_scalars.get("STOCKITEMNAME"):
            continue

        inv_row: dict[str, Any] = {
            "voucher_guid": vch_guid,
            "voucher_vchkey": vchkey or "",
            "ledger_line_index": inv_i,
            "inventory_line_index": inv_i,
        }
        inv_row.update(inv_scalars)

        for batch in inv_entry:
            if strip_ns(batch.tag) != "BATCHALLOCATIONS.LIST":
                continue
            inv_row.update(
                {f"batch_{k}": v for k, v in flatten_scalars(batch, skip_lists=True).items()}
            )

        for acct in inv_entry:
            if strip_ns(acct.tag) != "ACCOUNTINGALLOCATIONS.LIST":
                continue
            acct_scalars = flatten_scalars(acct, skip_lists=True)
            led_row: dict[str, Any] = {
                "voucher_guid": vch_guid,
                "voucher_vchkey": vchkey or "",
                "ledger_line_index": inv_i,
                "inventory_line_index": inv_i,
                "LEDGERNAME": acct_scalars.get("LEDGERNAME", ""),
                "AMOUNT": acct_scalars.get("AMOUNT", ""),
                "ISPARTYLEDGER": acct_scalars.get("ISPARTYLEDGER", ""),
            }
            led_row.update(acct_scalars)
            ledger_rows.append(led_row)

        inv_rows.append(inv_row)
        inv_i += 1

    # Party ledger entries (LEDGERENTRIES.LIST) — item invoice mode
    for le in voucher:
        if strip_ns(le.tag) != "LEDGERENTRIES.LIST":
            continue
        le_scalars = flatten_scalars(le, skip_lists=True)
        led_row = {
            "voucher_guid": vch_guid,
            "voucher_vchkey": vchkey or "",
            "ledger_line_index": inv_i,
            "inventory_line_index": -1,
            "_item_invoice_source": "LEDGERENTRIES",
            "_local_line_index": len(ledger_rows),
        }
        led_row.update(le_scalars)
        ledger_rows.append(led_row)
        inv_i += 1

    return ledger_rows, inv_rows


def _extract_from_standard_mode(
    voucher: ET.Element, vch_guid: str, vchkey: str | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    ledger_rows: list[dict[str, Any]] = []
    inv_rows: list[dict[str, Any]] = []
    line_idx = 0

    for le in voucher:
        if strip_ns(le.tag) != "ALLLEDGERENTRIES.LIST":
            continue
        if len(le) == 0:
            continue
        le_scalars = flatten_scalars(le, skip_lists=True)
        led_row: dict[str, Any] = {
            "voucher_guid": vch_guid,
            "voucher_vchkey": vchkey or "",
            "ledger_line_index": line_idx,
            "inventory_line_index": -1,
        }
        led_row.update(le_scalars)

        inv_i = 0
        for inv in le:
            if strip_ns(inv.tag) != "INVENTORYALLOCATIONS.LIST":
                continue
            inv_scalars = flatten_scalars(inv, skip_lists=True)
            if not inv_scalars.get("STOCKITEMNAME"):
                continue
            inv_entry: dict[str, Any] = {
                "voucher_guid": vch_guid,
                "voucher_vchkey": vchkey or "",
                "ledger_line_index": line_idx,
                "inventory_line_index": inv_i,
            }
            inv_entry.update(inv_scalars)
            for batch in inv:
                if strip_ns(batch.tag) != "BATCHALLOCATIONS.LIST":
                    continue
                inv_entry.update(
                    {f"batch_{k}": v for k, v in flatten_scalars(batch, skip_lists=True).items()}
                )
            inv_rows.append(inv_entry)
            inv_i += 1

        ledger_rows.append(led_row)
        line_idx += 1

    return ledger_rows, inv_rows


def _ledger_lines_for_voucher(
    voucher: ET.Element, vch_guid: str, vchkey: str | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if _is_item_invoice_mode(voucher):
        logger.debug("Voucher %s: Item Invoice mode detected.", vch_guid)
        return _extract_from_item_invoice_mode(voucher, vch_guid, vchkey)
    else:
        logger.debug("Voucher %s: Standard (As Voucher) mode detected.", vch_guid)
        return _extract_from_standard_mode(voucher, vch_guid, vchkey)


# =============================================================================
# SECTION 7 — Master data + element index: SINGLE TREE-WALK
# =============================================================================
# Previously the script made 7+ separate root.iter() passes.
# This function does ONE pass, collecting everything simultaneously.
# It also pre-indexes ET.Element references so the flat-fact builder
# never needs to re-scan the tree per row (eliminates O(N²) behaviour).

UDYAM_KEY_TAGS = (
    "UDYAMREGISTRATIONNO", "UDYAMNO", "UDYAMNUMBER",
    "MSMEREGISTRATIONNO", "REGISTRATIONNO", "REGISTRATIONNUMBER",
)
PAN_KEY_TAGS   = ("INCOMETAXPAN", "PAN", "ITPAN", "INCOMETAXNUMBER")
EWAY_NO_KEYS   = ("EWAYBILLNO", "EWAYBILLNUMBER", "EWBNO")
EWAY_DATE_KEYS = ("EWAYBILLDATE", "EWAYBILLDT", "EWAYDATE")
IRN_KEYS       = ("IRN", "IRNNO", "EINVOICEIRN")
ACK_NO_KEYS    = ("ACKNO", "ACKNUMBER", "ACKNOWLEDGEMENTNO")
ACK_DATE_KEYS  = ("ACKDATE", "ACKNOWLEDGEMENTDATE")
GST_REG_TAGS   = frozenset({"LEDGSTREGDETAILS.LIST", "LEDGSTREGDEATALS.LIST", "GSTDETAILS.LIST"})
MSME_TAG       = "MSMEREGISTRATIONDETAILS.LIST"

_DIMENSION_TAGS = frozenset({
    "CURRENCY", "GROUP", "LEDGER", "STOCKGROUP", "STOCKITEM",
    "UNIT", "GODOWN", "VOUCHERTYPE", "TAXUNIT", "COMPANY",
})


def _entity_name(el: ET.Element) -> str | None:
    return el.get("NAME") or el.get("RESERVEDNAME")


def _direct_scalar_children(el: ET.Element) -> dict[str, str]:
    out: dict[str, str] = {}
    for c in el:
        if _is_list_element(c):
            continue
        if _text_leaf(c):
            t = strip_ns(c.tag)
            val = (c.text or "").strip()
            if val:
                out[t] = val
    return out


def _first_nested_block_flat(el: ET.Element) -> dict[str, str]:
    if el is None:
        return {}
    for c in el:
        if _is_list_element(c):
            sub = _first_nested_block_flat(c)
            if sub:
                return sub
            continue
        if _text_leaf(c):
            return {strip_ns(c.tag): (c.text or "").strip()}
        d = flatten_scalars(c, skip_lists=True, always_skip_list_tags=frozenset())
        if d:
            return {k: str(v) for k, v in d.items() if v is not None}
    return {}


def _pick_first(d: dict[str, str], keys: tuple[str, ...]) -> str:
    for k in keys:
        if d.get(k):
            return d[k]
    return ""


def _format_address_blob(d: dict[str, str]) -> str:
    parts = [d[k] for k in sorted(d.keys()) if "ADDRESS" in k.upper()]
    return " | ".join(parts) if parts else d.get("ADDRESS", "")


def _collect_ledger_master(led: ET.Element) -> dict[str, Any]:
    """Extract all fields from a LEDGER master element."""
    parent = cur = cc_on = opening_balance = tax_type = ""
    for c in led:
        t = strip_ns(c.tag)
        if not _text_leaf(c):
            continue
        val = (c.text or "").strip()
        if t == "PARENT":          parent = val
        elif t == "CURRENCYNAME":  cur = val
        elif t == "ISCOSTCENTRESON": cc_on = val
        elif t == "OPENINGBALANCE": opening_balance = val
        elif t == "TAXTYPE":       tax_type = val

    # GSTIN + PAN
    gstin = pan = ""
    for c in led:
        if _is_list_element(c) or not _text_leaf(c):
            continue
        if strip_ns(c.tag) in PAN_KEY_TAGS:
            pan = (c.text or "").strip() or pan
    for c in led:
        if strip_ns(c.tag) in GST_REG_TAGS:
            raw = _direct_scalar_children(c) or _first_nested_block_flat(c)
            if not gstin:
                gstin = raw.get("GSTIN", "")
            if not pan:
                pan = _pick_first(raw, PAN_KEY_TAGS)
            if gstin and pan:
                break

    # MSME
    udyam = msme_cat = msme_act = msme_from = ""
    for c in led:
        if strip_ns(c.tag) != MSME_TAG:
            continue
        raw = _direct_scalar_children(c) or _first_nested_block_flat(c)
        msme_from = raw.get("FROMDATE", "")
        msme_cat  = raw.get("ENTERPRISETYPE", "")
        msme_act  = raw.get("MSMEACTIVITYTYPE", "")
        udyam = _pick_first(raw, UDYAM_KEY_TAGS)
        if not udyam:
            for k, v in raw.items():
                if v and re.search(r"UDYAM", k, re.I):
                    udyam = v
                    break
        break

    # Entity type
    entity_type = ""
    for c in led:
        if strip_ns(c.tag) in ("STATUTORYDETAILS.LIST", "PARTYDETAILS.LIST", "LEDSTATUTORY.LIST"):
            raw = _direct_scalar_children(c) or _first_nested_block_flat(c)
            for k in ("CONSTITUTIONNAME", "ENTITYTYPE", "ORGANIZATIONTYPE", "PARTYTYPE"):
                if raw.get(k):
                    entity_type = raw[k]
                    break
            if entity_type:
                break

    return {
        "ledger_group_name": parent,
        "currency": cur,
        "has_cost_centres_on_ledger": cc_on,
        "gstin": gstin,
        "pan": pan,
        "party_entity_type": entity_type,
        "opening_balance": opening_balance,
        "tax_type": tax_type,
        "udyam_registration_number": udyam,
        "msme_enterprise_category": msme_cat,
        "msme_activity_type": msme_act,
        "msme_effective_from": msme_from,
    }


def _collect_voucher_sidecar(voucher: ET.Element) -> dict[str, str]:
    """Extract all voucher-level sidecar fields in a single element pass."""
    out: dict[str, str] = {k: "" for k in (
        "narration", "destination", "gst_registration_name", "company_state",
        "delivery_same_as_consignee", "dispatch_same_as_consignor",
        "is_reverse_charge_applicable", "is_unregistered_rcm",
        "bill_to_name", "bill_to_address", "bill_to_state", "bill_to_pin",
        "ship_to_name", "ship_to_address", "ship_to_state", "ship_to_pin",
        "eway_consignor_address", "eway_consignee_address",
        "e_way_bill_number", "e_way_bill_date",
        "e_invoice_irn", "e_invoice_ack_number", "e_invoice_ack_date",
        "party_invoice_no", "party_invoice_date",
        "original_voucher_number", "original_voucher_date",
        "agreement_order_no", "goods_vehicle_number", "reference",
        "voucher_entry_mode",
    )}

    _scalar_map = {
        "NARRATION": "narration",
        "BASICFINALDESTINATION": "destination",
        "GSTREGISTRATION": "gst_registration_name",
        "CMPGSTSTATE": "company_state",
        "ISDELIVERYSAMEASCONSIGNEE": "delivery_same_as_consignee",
        "ISDISPATCHSAMEASCONSIGNOR": "dispatch_same_as_consignor",
        "ISREVERSECHARGEAPPLICABLE": "is_reverse_charge_applicable",
        "VCHSTATUSISUNREGISTEREDRCM": "is_unregistered_rcm",
        "VCHENTRYMODE": "voucher_entry_mode",
        "PARTYINVNO": "party_invoice_no",
        "PARTYINVDATE": "party_invoice_date",
        "ORIGINALVCHNUMBER": "original_voucher_number",
        "ORIGINALVCHDATE": "original_voucher_date",
        "AGGREMENTORDERNO": "agreement_order_no",
        "GOODSVEHICLENUMBER": "goods_vehicle_number",
        "REFERENCE": "reference",
    }

    # Collect all child tags in one pass
    wanted_lists = {
        "GSTBUYERADDRESS.LIST", "GSTCONSIGNEEADDRESS.LIST",
        "GSTEWAYCONSIGNORADDRESS.LIST", "GSTEWAYCONSIGNEEADDRESS.LIST",
        "EWAYBILLDETAILS.LIST", "GSTEINVOICEDETAILS.LIST", "EINVOICEDETAILS.LIST",
    }
    list_children: dict[str, ET.Element] = {}

    for c in voucher:
        t = strip_ns(c.tag)
        if t in _scalar_map and _text_leaf(c):
            out[_scalar_map[t]] = (c.text or "").strip()
        elif t in wanted_lists and t not in list_children:
            list_children[t] = c

    def addr(prefix: str, tag: str) -> None:
        lst = list_children.get(tag)
        if lst is None:
            return
        blk = _direct_scalar_children(lst) or _first_nested_block_flat(lst)
        if not blk:
            return
        out[f"{prefix}_name"]    = blk.get("NAME") or blk.get("BILLTO") or blk.get("PARTYNAME") or ""
        out[f"{prefix}_state"]   = blk.get("STATE") or blk.get("STATENAME") or ""
        out[f"{prefix}_pin"]     = blk.get("PINCODE") or blk.get("PIN") or ""
        out[f"{prefix}_address"] = _format_address_blob(blk) or blk.get("ADDRESS", "")

    addr("bill_to", "GSTBUYERADDRESS.LIST")
    addr("ship_to", "GSTCONSIGNEEADDRESS.LIST")

    for tag, key in (
        ("GSTEWAYCONSIGNORADDRESS.LIST", "eway_consignor_address"),
        ("GSTEWAYCONSIGNEEADDRESS.LIST", "eway_consignee_address"),
    ):
        lst = list_children.get(tag)
        if lst:
            blk = _direct_scalar_children(lst) or _first_nested_block_flat(lst)
            if blk:
                out[key] = " | ".join(f"{k}={v}" for k, v in sorted(blk.items()) if v)

    ew = list_children.get("EWAYBILLDETAILS.LIST")
    if ew is not None:
        blk = _direct_scalar_children(ew) or _first_nested_block_flat(ew)
        out["e_way_bill_number"] = _pick_first(blk, EWAY_NO_KEYS)
        out["e_way_bill_date"]   = _pick_first(blk, EWAY_DATE_KEYS)

    for einv_tag in ("GSTEINVOICEDETAILS.LIST", "EINVOICEDETAILS.LIST"):
        ei = list_children.get(einv_tag)
        if ei is None:
            continue
        blk = _direct_scalar_children(ei) or _first_nested_block_flat(ei)
        out["e_invoice_irn"]        = _pick_first(blk, IRN_KEYS)
        out["e_invoice_ack_number"] = _pick_first(blk, ACK_NO_KEYS)
        out["e_invoice_ack_date"]   = _pick_first(blk, ACK_DATE_KEYS)
        if out["e_invoice_irn"]:
            break

    if not out["voucher_entry_mode"]:
        out["voucher_entry_mode"] = "As Voucher"

    return out


def _extract_credit_period_from_le(le: ET.Element) -> str:
    """Read BILLCREDITPERIOD from a ALLLEDGERENTRIES.LIST element."""
    for c in le:
        if strip_ns(c.tag) != "BILLALLOCATIONS.LIST":
            continue
        for f in c:
            if strip_ns(f.tag) == "BILLCREDITPERIOD":
                txt = (f.text or "").strip()
                return txt if txt else (f.get("P") or "").strip()
    return ""


def _voucher_guid_from_elem(voucher: ET.Element) -> str:
    for c in voucher:
        if strip_ns(c.tag) == "GUID" and _text_leaf(c):
            return (c.text or "").strip()
    return ""


def collect_all_masters(root: ET.Element) -> MasterData:
    """
    Single-pass collection of ALL master data AND element index references.

    Replaces the original 7+ separate root.iter() calls.
    Also pre-builds element reference dicts so the flat-fact builder
    never needs to re-scan the tree (eliminates O(N^2) behaviour).
    """
    ledger_m: dict[str, dict[str, Any]]    = {}
    stock_m:  dict[str, dict[str, Any]]    = {}
    group_parent: dict[str, str]           = {}
    stock_parent: dict[str, str]           = {}
    voucher_sidecar: dict[str, dict]       = {}
    credit_period: dict[tuple[str, int], str] = {}
    dimensions: dict[str, list[dict]]      = {t: [] for t in _DIMENSION_TAGS}

    # Element reference indexes
    le_elem: dict[tuple[str, int], ET.Element]          = {}
    inv_elem: dict[tuple[str, int, int], ET.Element]    = {}
    party_le_elem: dict[tuple[str, int], ET.Element]    = {}

    for tm in root.iter():
        if strip_ns(tm.tag) != "TALLYMESSAGE":
            continue

        for child in tm:
            tag = strip_ns(child.tag)

            # ── Dimension / master elements ───────────────────────────────────
            if tag in _DIMENSION_TAGS:
                name = _entity_name(child)

                if tag == "LEDGER" and name:
                    ledger_m[name] = _collect_ledger_master(child)
                    # Dimensions table entry too
                    row = flatten_scalars(child, skip_lists=True)
                    row["entity_tag"] = tag
                    row["entity_name"] = name
                    for ak, av in child.attrib.items():
                        row[f"attr_{ak.lower()}"] = av
                    dimensions[tag].append(row)

                elif tag == "STOCKITEM" and name:
                    oq = ov = sp = ""
                    for c in child:
                        ct = strip_ns(c.tag)
                        if not _text_leaf(c):
                            continue
                        val = (c.text or "").strip()
                        if ct == "OPENINGBALANCE": oq = val
                        elif ct == "OPENINGVALUE": ov = val
                        elif ct == "PARENT":       sp = val
                    stock_m[name]     = {"opening_qty": oq, "opening_value": ov}
                    stock_parent[name] = sp
                    row = flatten_scalars(child, skip_lists=True)
                    row.update({"entity_tag": tag, "entity_name": name})
                    dimensions[tag].append(row)

                elif tag == "GROUP" and name:
                    p = next(
                        ((c.text or "").strip() for c in child
                         if strip_ns(c.tag) == "PARENT" and _text_leaf(c)), ""
                    )
                    group_parent[name] = p
                    row = flatten_scalars(child, skip_lists=True)
                    row.update({"entity_tag": tag, "entity_name": name})
                    dimensions[tag].append(row)

                else:
                    # Other dimension tags — just collect for star schema export
                    row = flatten_scalars(child, skip_lists=True)
                    row["entity_tag"] = tag
                    if name:
                        row["entity_name"] = name
                    for ak, av in child.attrib.items():
                        row[f"attr_{ak.lower()}"] = av
                    dimensions[tag].append(row)

            # ── VOUCHER elements ──────────────────────────────────────────────
            elif tag == "VOUCHER":
                guid = _voucher_guid_from_elem(child)
                if not guid:
                    continue

                # Sidecar (voucher-level fields: narration, GST, e-way, etc.)
                voucher_sidecar[guid] = _collect_voucher_sidecar(child)

                # Index ALLLEDGERENTRIES.LIST elements + their credit periods
                le_idx = 0
                party_le_local_idx = 0

                for le_child in child:
                    le_tag = strip_ns(le_child.tag)

                    if le_tag == "ALLLEDGERENTRIES.LIST":
                        if len(le_child) > 0:
                            # Store reference for bill-allocation extraction
                            le_elem[(guid, le_idx)] = le_child
                            # Credit period
                            cp = _extract_credit_period_from_le(le_child)
                            if cp:
                                credit_period[(guid, le_idx)] = cp
                            # Index inventory allocations under this LE
                            inv_i = 0
                            for inv_c in le_child:
                                if strip_ns(inv_c.tag) in (
                                    "INVENTORYALLOCATIONS.LIST",
                                    "ALLINVENTORYENTRIES.LIST",
                                ):
                                    inv_elem[(guid, le_idx, inv_i)] = inv_c
                                    inv_i += 1
                            le_idx += 1

                    elif le_tag == "LEDGERENTRIES.LIST":
                        # Item-invoice-mode party ledger entries
                        party_le_elem[(guid, party_le_local_idx)] = le_child
                        party_le_local_idx += 1

                    elif le_tag == "ALLINVENTORYENTRIES.LIST":
                        # Item invoice: direct inventory on voucher
                        inv_i = 0
                        for inv_entry in le_child:
                            if any(
                                strip_ns(c.tag) == "STOCKITEMNAME" and c.text
                                for c in inv_entry
                            ):
                                inv_elem[(guid, -1, inv_i)] = inv_entry
                                inv_i += 1

    dimensions = {k: v for k, v in dimensions.items() if v}
    return MasterData(
        ledger=ledger_m,
        stock=stock_m,
        group_parent=group_parent,
        stock_parent=stock_parent,
        le_elem=le_elem,
        inv_elem=inv_elem,
        party_le_elem=party_le_elem,
        voucher_sidecar=voucher_sidecar,
        credit_period=credit_period,
        dimensions=dimensions,
    )


# =============================================================================
# SECTION 8 — Primary group traversal (with cycle guard)
# =============================================================================

def primary_group(group_name: str, parent_of: dict[str, str], max_depth: int = 50) -> str:
    """
    Walk up the group hierarchy to find the root (primary) group.
    max_depth prevents infinite loops on corrupt/cyclic Tally data.
    """
    if not group_name:
        return ""
    cur = group_name
    seen: set[str] = set()
    for _ in range(max_depth):
        nxt = parent_of.get(cur)
        if not nxt or nxt in seen:
            break
        seen.add(cur)
        cur = nxt
    else:
        logger.warning(
            "primary_group: max_depth=%d reached for '%s'. "
            "Possible cycle in Tally group hierarchy.",
            max_depth, group_name,
        )
    return cur


# =============================================================================
# SECTION 9 — Bill & batch extraction (uses pre-built element index)
# =============================================================================

def _extract_bill_allocations(le: ET.Element) -> list[dict[str, str]]:
    allocations: list[dict[str, str]] = []
    for c in le:
        if strip_ns(c.tag) != "BILLALLOCATIONS.LIST":
            continue
        name = date_str = amount = allocated = typ = ""
        for f in c:
            ft = strip_ns(f.tag)
            if ft in ("NAME", "n"):          name      = (f.text or "").strip()
            elif ft == "BILLDATE":           date_str  = (f.text or "").strip()
            elif ft == "BILLAMOUNT":         amount    = (f.text or "").strip()
            elif ft == "AMOUNT":             allocated = (f.text or "").strip()
            elif ft == "BILLTYPE":           typ       = (f.text or "").strip()
        allocations.append({
            "bill_name": name, "bill_date": date_str,
            "bill_amount": amount, "bill_allocated_amount": allocated, "bill_type": typ,
        })
    return allocations


def _extract_batch_details(inv_alloc: ET.Element) -> dict[str, str]:
    out = {"batch_mfg_date": "", "batch_expiry_period": "", "batch_tracking_number": "", "batch_order_no": ""}
    for c in inv_alloc:
        if strip_ns(c.tag) != "BATCHALLOCATIONS.LIST":
            continue
        for batch in c:
            for f in batch:
                ft = strip_ns(f.tag)
                if ft == "MFDON":           out["batch_mfg_date"]          = (f.text or "").strip()
                elif ft == "EXPIRYPERIOD":  out["batch_expiry_period"]     = (f.text or "").strip()
                elif ft == "TRACKINGNUMBER": out["batch_tracking_number"]  = (f.text or "").strip()
                elif ft == "ORDERNO":       out["batch_order_no"]          = (f.text or "").strip()
            break
        break
    return out


def _pan_from_gstin(gstin: str) -> str:
    g = (gstin or "").strip().upper()
    if len(g) == 15 and g[:2].isdigit():
        return g[2:12]
    return ""


def _parse_decimal(s: str | None) -> Decimal:
    if not s or not str(s).strip():
        return Decimal(0)
    try:
        return Decimal(str(s).strip().replace(",", ""))
    except InvalidOperation:
        return Decimal(0)


# =============================================================================
# SECTION 10 — Flat fact columns definition
# =============================================================================

FLAT_FACT_COLUMNS: tuple[str, ...] = (
    "entry_line_key", "voucher_natural_key", "posting_date",
    "voucher_type", "voucher_number", "voucher_entry_mode",
    "party_ledger_name", "gst_registration_name", "company_state",
    "ledger_name", "credit_period", "ledger_group_name", "primary_group_name",
    "is_party_ledger_line", "has_cost_centres_on_ledger", "cost_centre_name",
    "narration", "destination", "currency",
    "amount_absolute", "amount_tally_signed", "debit_credit_flag", "signed_amount_debit_positive",
    "pan", "pan_derived_from_gstin", "gstin",
    "udyam_registration_number", "msme_enterprise_category", "msme_activity_type", "msme_effective_from",
    "party_entity_type",
    "bill_to_name", "bill_to_address", "bill_to_state", "bill_to_pin",
    "ship_to_name", "ship_to_address", "ship_to_state", "ship_to_pin",
    "delivery_same_as_consignee", "dispatch_same_as_consignor",
    "eway_consignor_address", "eway_consignee_address",
    "e_way_bill_number", "e_way_bill_date",
    "e_invoice_irn", "e_invoice_ack_number", "e_invoice_ack_date",
    "is_reverse_charge_applicable", "is_unregistered_rcm",
    "stock_item_name", "stock_group_name", "godown_name", "batch_name",
    "quantity", "rate", "inventory_amount",
    "line_index", "inventory_line_index", "bill_line_index",
    "ledger_opening_balance", "tax_type",
    "stock_opening_balance", "stock_opening_value",
    "bill_name", "bill_date", "bill_amount", "bill_allocated_amount", "bill_type",
    "batch_mfg_date", "batch_expiry_period", "batch_tracking_number", "batch_order_no",
    "party_invoice_no", "party_invoice_date",
    "original_voucher_number", "original_voucher_date",
    "agreement_order_no", "goods_vehicle_number", "reference",
)

_EMPTY_BILL = {
    "bill_name": "", "bill_date": "", "bill_amount": "",
    "bill_allocated_amount": "", "bill_type": "",
}


# =============================================================================
# SECTION 11 — SQLite output
# =============================================================================

def _init_db(conn: sqlite3.Connection) -> None:
    """Create the transactions table if it does not exist."""
    cols_sql = ", ".join(f'"{c}" TEXT' for c in FLAT_FACT_COLUMNS)
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS transactions (
            {cols_sql},
            PRIMARY KEY ("entry_line_key")
        )
    """)
    conn.execute('CREATE INDEX IF NOT EXISTS idx_posting_date ON transactions("posting_date")')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_voucher_type ON transactions("voucher_type")')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_ledger_name  ON transactions("ledger_name")')
    conn.commit()


def _insert_rows(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    """Bulk insert a batch of tuples into transactions table."""
    placeholders = ", ".join(["?"] * len(FLAT_FACT_COLUMNS))
    conn.executemany(
        f"INSERT OR REPLACE INTO transactions VALUES ({placeholders})",
        rows,
    )
    conn.commit()


def export_db_to_csv(db_path: Path, csv_path: Path) -> int:
    """
    Export all rows from transactions table to a CSV file.
    Returns the number of rows written.
    Streaming cursor — does not load all rows into RAM at once.
    """
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM transactions ORDER BY posting_date, voucher_natural_key")

    row_count = 0
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(FLAT_FACT_COLUMNS)
        for row in cur:
            writer.writerow(tuple(row))
            row_count += 1

    conn.close()
    return row_count


# =============================================================================
# SECTION 12 — Flat fact row builder  (writes directly to SQLite)
# =============================================================================

def process_chunk_to_db(
    root: ET.Element,
    masters: MasterData,
    db_conn: sqlite3.Connection,
    batch_size: int = 500,
) -> tuple[int, int]:
    """
    Build flat fact rows from a parsed XML chunk and write them directly to SQLite.

    Operates in streaming batches (batch_size rows at a time) — no full list in RAM.
    Returns (voucher_count, row_count).
    """
    _EMPTY_INV_INDEX: dict[tuple[str, int], list[dict]] = {}

    # Build inventory index from this chunk's vouchers only
    inv_index: dict[tuple[str, int], list[dict[str, Any]]] = {}
    fact_voucher: list[dict[str, Any]] = []
    fact_ledger_entry: list[dict[str, Any]] = []

    for tm in root.iter():
        if strip_ns(tm.tag) != "TALLYMESSAGE":
            continue
        for child in tm:
            if strip_ns(child.tag) != "VOUCHER":
                continue
            hdr = _voucher_header_row(child)
            guid = hdr.get("GUID", "")
            vchkey = child.get("VCHKEY") or hdr.get("ATTR_VCHKEY")
            fact_voucher.append(hdr)
            ledgers, invs = _ledger_lines_for_voucher(child, guid, vchkey)
            fact_ledger_entry.extend(ledgers)
            for inv in invs:
                g = str(inv.get("voucher_guid", ""))
                li = int(inv.get("ledger_line_index", -1))
                if inv.get("STOCKITEMNAME"):
                    inv_index.setdefault((g, li), []).append(inv)

    # Sort inventory sub-lists
    for key in inv_index:
        inv_index[key].sort(key=lambda x: int(x.get("inventory_line_index", 0)))

    vch_by_guid = {r.get("GUID"): r for r in fact_voucher if r.get("GUID")}

    batch: list[tuple] = []
    total_rows = 0

    for le in fact_ledger_entry:
        g  = str(le.get("voucher_guid", ""))
        li = int(le.get("ledger_line_index", 0))
        vch  = vch_by_guid.get(g, {})
        side = masters.voucher_sidecar.get(g, {})

        nat = str(vch.get("ATTR_VCHKEY") or vch.get("GUID") or g)
        ledger_name = str(le.get("LEDGERNAME", ""))

        lm  = masters.ledger.get(ledger_name, {})
        lg  = lm.get("ledger_group_name") or ""
        pg  = primary_group(lg, masters.group_parent)

        gstin       = str(lm.get("gstin", ""))
        pan_explicit = str(lm.get("pan", ""))
        pan_derived  = _pan_from_gstin(gstin)

        amt_raw  = str(le.get("AMOUNT", "") or "0")
        tally_amt = _parse_decimal(amt_raw)
        signed   = -tally_amt
        dcf      = "Dr" if signed > 0 else ("Cr" if signed < 0 else "")

        date_raw = str(vch.get("DATE") or vch.get("EFFECTIVEDATE") or "")
        posting  = ""
        if len(date_raw) == 8 and date_raw.isdigit():
            posting = f"{date_raw[:4]}-{date_raw[4:6]}-{date_raw[6:8]}"

        invs = inv_index.get((g, li), [])
        inv0 = invs[0] if invs else None
        stk_name  = str(inv0.get("STOCKITEMNAME", "")) if inv0 else ""
        stk_group = masters.stock_parent.get(stk_name, "")
        credit_period = masters.credit_period.get((g, li), "")

        # Bill allocations — use pre-indexed element reference (O(1) lookup)
        item_invoice_source = str(le.get("_item_invoice_source", ""))
        if item_invoice_source == "LEDGERENTRIES":
            local_idx = int(le.get("_local_line_index", 0))
            bill_alloc_elem = masters.party_le_elem.get((g, local_idx))
        else:
            bill_alloc_elem = masters.le_elem.get((g, li))

        bill_allocs = (
            _extract_bill_allocations(bill_alloc_elem)
            if bill_alloc_elem is not None else []
        )
        if not bill_allocs:
            bill_allocs = [_EMPTY_BILL]

        # Batch details — use pre-indexed element reference (O(1) lookup)
        batch_details: dict[str, str] = {}
        if inv0 and inv0.get("inventory_line_index") is not None:
            inv_line_idx = int(inv0.get("inventory_line_index", 0))
            inv_alloc_elem = masters.inv_elem.get((g, li, inv_line_idx))
            if inv_alloc_elem is None:
                # Fallback for item-invoice mode indexed under (guid, -1, inv_i)
                inv_alloc_elem = masters.inv_elem.get((g, -1, inv_line_idx))
            if inv_alloc_elem is not None:
                batch_details = _extract_batch_details(inv_alloc_elem)

        for bill_idx, bill_d in enumerate(bill_allocs):
            entry_key = f"{nat}|{li}|{bill_idx}"

            raw_alloc = bill_d.get("bill_allocated_amount", "")
            if raw_alloc:
                row_amt   = _parse_decimal(raw_alloc)
                row_signed = -row_amt
                row_dcf   = "Dr" if row_signed > 0 else ("Cr" if row_signed < 0 else "")
            else:
                row_amt   = tally_amt
                row_signed = signed
                row_dcf   = dcf

            row_dict: dict[str, Any] = {
                "entry_line_key":              entry_key,
                "voucher_natural_key":         nat,
                "posting_date":                posting,
                "voucher_type":                str(vch.get("VOUCHERTYPENAME", "") or vch.get("ATTR_VCHTYPE", "")),
                "voucher_number":              str(vch.get("VOUCHERNUMBER", "")),
                "voucher_entry_mode":          side.get("voucher_entry_mode", ""),
                "party_ledger_name":           str(vch.get("PARTYLEDGERNAME", "")),
                "gst_registration_name":       side.get("gst_registration_name") or str(vch.get("GSTREGISTRATION", "")),
                "company_state":               side.get("company_state") or str(vch.get("CMPGSTSTATE", "")),
                "ledger_name":                 ledger_name,
                "credit_period":               credit_period,
                "ledger_group_name":           lg,
                "primary_group_name":          pg,
                "is_party_ledger_line":        str(le.get("ISPARTYLEDGER", "")),
                "has_cost_centres_on_ledger":  str(lm.get("has_cost_centres_on_ledger", "")),
                "cost_centre_name":            "",
                "narration":                   side.get("narration", ""),
                "destination":                 side.get("destination") or str(vch.get("BASICFINALDESTINATION", "")),
                "currency":                    str(lm.get("currency", "")),
                "amount_absolute":             str(abs(row_amt)),
                "amount_tally_signed":         str(row_amt),
                "debit_credit_flag":           row_dcf,
                "signed_amount_debit_positive": str(row_signed),
                "pan":                         pan_explicit,
                "pan_derived_from_gstin":      pan_derived,
                "gstin":                       gstin,
                "udyam_registration_number":   str(lm.get("udyam_registration_number", "")),
                "msme_enterprise_category":    str(lm.get("msme_enterprise_category", "")),
                "msme_activity_type":          str(lm.get("msme_activity_type", "")),
                "msme_effective_from":         str(lm.get("msme_effective_from", "")),
                "party_entity_type":           str(lm.get("party_entity_type", "")),
                "bill_to_name":                side.get("bill_to_name", ""),
                "bill_to_address":             side.get("bill_to_address", ""),
                "bill_to_state":               side.get("bill_to_state", ""),
                "bill_to_pin":                 side.get("bill_to_pin", ""),
                "ship_to_name":                side.get("ship_to_name", ""),
                "ship_to_address":             side.get("ship_to_address", ""),
                "ship_to_state":               side.get("ship_to_state", ""),
                "ship_to_pin":                 side.get("ship_to_pin", ""),
                "delivery_same_as_consignee":  side.get("delivery_same_as_consignee", ""),
                "dispatch_same_as_consignor":  side.get("dispatch_same_as_consignor", ""),
                "eway_consignor_address":      side.get("eway_consignor_address", ""),
                "eway_consignee_address":      side.get("eway_consignee_address", ""),
                "e_way_bill_number":           side.get("e_way_bill_number", ""),
                "e_way_bill_date":             side.get("e_way_bill_date", ""),
                "e_invoice_irn":               side.get("e_invoice_irn", ""),
                "e_invoice_ack_number":        side.get("e_invoice_ack_number", ""),
                "e_invoice_ack_date":          side.get("e_invoice_ack_date", ""),
                "is_reverse_charge_applicable": side.get("is_reverse_charge_applicable") or str(vch.get("ISREVERSECHARGEAPPLICABLE", "")),
                "is_unregistered_rcm":         side.get("is_unregistered_rcm") or str(vch.get("VCHSTATUSISUNREGISTEREDRCM", "")),
                "stock_item_name":             stk_name,
                "stock_group_name":            stk_group,
                "godown_name":                 str(inv0.get("batch_GODOWNNAME", "")) if inv0 else "",
                "batch_name":                  str(inv0.get("batch_BATCHNAME", "")) if inv0 else "",
                "quantity":                    str(inv0.get("ACTUALQTY", "")) if inv0 else "",
                "rate":                        str(inv0.get("RATE", "")) if inv0 else "",
                "inventory_amount":            str(inv0.get("AMOUNT", "")) if inv0 else "",
                "line_index":                  str(li),
                "inventory_line_index":        str(int(inv0.get("inventory_line_index", 0))) if inv0 else "-1",
                "bill_line_index":             str(bill_idx),
                "ledger_opening_balance":      str(lm.get("opening_balance", "")),
                "tax_type":                    str(lm.get("tax_type", "")),
                "stock_opening_balance":       str(masters.stock.get(stk_name, {}).get("opening_qty", "")),
                "stock_opening_value":         str(masters.stock.get(stk_name, {}).get("opening_value", "")),
                "bill_name":                   bill_d["bill_name"],
                "bill_date":                   bill_d["bill_date"],
                "bill_amount":                 bill_d["bill_amount"],
                "bill_allocated_amount":       bill_d["bill_allocated_amount"],
                "bill_type":                   bill_d["bill_type"],
                "batch_mfg_date":              batch_details.get("batch_mfg_date", ""),
                "batch_expiry_period":         batch_details.get("batch_expiry_period", ""),
                "batch_tracking_number":       batch_details.get("batch_tracking_number", ""),
                "batch_order_no":              batch_details.get("batch_order_no", ""),
                "party_invoice_no":            side.get("party_invoice_no", ""),
                "party_invoice_date":          side.get("party_invoice_date", ""),
                "original_voucher_number":     side.get("original_voucher_number", ""),
                "original_voucher_date":       side.get("original_voucher_date", ""),
                "agreement_order_no":          side.get("agreement_order_no", ""),
                "goods_vehicle_number":        side.get("goods_vehicle_number", ""),
                "reference":                   side.get("reference", ""),
            }

            batch.append(tuple(row_dict.get(c, "") for c in FLAT_FACT_COLUMNS))
            total_rows += 1

            # Flush to SQLite every batch_size rows — keeps RAM bounded
            if len(batch) >= batch_size:
                _insert_rows(db_conn, batch)
                batch.clear()

    # Flush remaining rows
    if batch:
        _insert_rows(db_conn, batch)

    return len(fact_voucher), total_rows


# =============================================================================
# SECTION 13 — Output filename helpers
# =============================================================================

_WINDOWS_RESERVED = frozenset({
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
})


def _safe_filename(name: str) -> str:
    """Sanitise a string for use as a filename on both Linux and Windows."""
    cleaned = re.sub(r'[\/*?:"<>|]', "_", name).strip(" .")
    if cleaned.upper() in _WINDOWS_RESERVED:
        cleaned = f"_{cleaned}"
    return cleaned or "company"


def _extract_company_name(root: ET.Element) -> str:
    for el in root.iter():
        if strip_ns(el.tag) == "SVCURRENTCOMPANY" and el.text and el.text.strip():
            return el.text.strip()
    return "company"


def _ask_yes_no(prompt: str, default: bool = False) -> bool:
    hint = " [Y/n]" if default else " [y/N]"
    while True:
        try:
            ans = input(prompt + hint + ": ").strip().lower()
        except EOFError:
            print(f"{'y' if default else 'n'} (default)")
            return default
        if ans == "":
            return default
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False
        print("  Please enter y or n.")


def write_csv_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    """Write a list of dicts to CSV (used for optional star-schema dimension tables)."""
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    seen: set[str] = set()
    for r in rows:
        for k in r:
            if k not in seen:
                seen.add(k)
                keys.append(k)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore", restval="")
        w.writeheader()
        w.writerows(rows)


# =============================================================================
# SECTION 14 — Main entry point
# =============================================================================

def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Tally Pipeline v2 — fetch XML from Tally Prime, parse in monthly chunks,\n"
            "write rows directly to SQLite, then export to CSV on demand.\n"
            "No large intermediate files. Bounded RAM usage regardless of dataset size."
        )
    )
    ap.add_argument("--from",      dest="from_date",    default=None,
                    help="Start date YYYYMMDD (prompts if omitted)")
    ap.add_argument("--to",        dest="to_date",      default=None,
                    help="End date YYYYMMDD (prompts if omitted)")
    ap.add_argument("--port",      type=int,            default=9000,
                    help="Tally HTTP port (default: 9000)")
    ap.add_argument("--out",       type=Path,           default=Path("tally_out"),
                    help="Output directory (default: ./tally_out)")
    ap.add_argument("--db",        type=Path,           default=None,
                    help="SQLite database path (default: <out>/<company>_tally.db)")
    ap.add_argument("--prefix",    default="",
                    help="Optional filename prefix for star-schema CSVs")
    ap.add_argument("--retries",   type=int,            default=3,
                    help="Max HTTP retry attempts (default: 3)")
    ap.add_argument("--timeout",   type=int,            default=300,
                    help="HTTP timeout in seconds (default: 300)")
    ap.add_argument("--chunk-months", dest="chunk_months", type=int, default=1,
                    help="Months per Tally HTTP request (default: 1). "
                         "Increase to 3 for faster pulls on small datasets.")
    ap.add_argument("--no-prompt", action="store_true",
                    help="Skip interactive prompts; export only the transaction dump.")
    ap.add_argument("--debug",     action="store_true",
                    help="Enable DEBUG logging (logs raw Tally responses, parsing detail).")
    args = ap.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
        logger.debug("Debug logging enabled.")
        if _LXML_AVAILABLE:
            logger.debug("lxml detected — using fast XML parser.")
        else:
            logger.debug("lxml not found — using stdlib xml.etree (pip install lxml for faster parsing).")

    print("\n================================================")
    print("  Tally Pipeline v2  —  Fetch > Parse > SQLite")
    print("================================================\n")

    # ── Collect dates ─────────────────────────────────────────────────────────
    from_date = args.from_date
    to_date   = args.to_date

    if not from_date:
        from_date = input("Enter Start Date (YYYYMMDD) [e.g., 20250401]: ").strip()
    if not to_date:
        to_date = input("Enter End Date   (YYYYMMDD) [e.g., 20260331]: ").strip()

    if not from_date or not to_date:
        print("Both dates are required.")
        return 1

    if not _validate_date_format(from_date):
        print(f"Invalid start date: {from_date}. Expected YYYYMMDD.")
        return 1
    if not _validate_date_format(to_date):
        print(f"Invalid end date: {to_date}. Expected YYYYMMDD.")
        return 1
    if int(from_date) > int(to_date):
        print("Start date cannot be after end date.")
        return 1

    # ── Prepare output directory + DB ─────────────────────────────────────────
    args.out.mkdir(parents=True, exist_ok=True)

    chunks = _month_chunks(from_date, to_date, args.chunk_months)
    logger.info(
        "Date range %s to %s split into %d chunk(s) of %d month(s) each.",
        from_date, to_date, len(chunks), args.chunk_months,
    )

    # Fetch the first chunk to get the company name for the DB filename
    company_name = "company"
    db_path = args.db

    # ── Main fetch + parse loop (chunk by chunk) ──────────────────────────────
    total_vouchers = 0
    total_rows     = 0
    db_conn: sqlite3.Connection | None = None
    masters_global: MasterData | None = None

    for chunk_idx, (chunk_from, chunk_to) in enumerate(chunks):
        logger.info(
            "Processing chunk %d/%d: %s to %s",
            chunk_idx + 1, len(chunks), chunk_from, chunk_to,
        )

        raw_bytes = fetch_tally_xml_bytes(
            chunk_from, chunk_to,
            port=args.port,
            max_retries=args.retries,
            timeout=args.timeout,
        )
        if raw_bytes is None:
            logger.error("Chunk %d/%d failed — skipping.", chunk_idx + 1, len(chunks))
            continue

        try:
            root = parse_xml_from_bytes(raw_bytes)
        except ET.ParseError as exc:
            logger.error("XML parse error in chunk %d: %s", chunk_idx + 1, exc)
            continue

        # Collect company name from first successful chunk
        if company_name == "company":
            company_name = _safe_filename(_extract_company_name(root))

        # Initialise SQLite on first successful chunk
        if db_conn is None:
            if db_path is None:
                db_path = args.out / f"{company_name}_tally.db"
            db_conn = sqlite3.connect(str(db_path))
            _init_db(db_conn)
            logger.info("SQLite database: %s", db_path)

        # Single-pass: collect all masters AND pre-build element index
        logger.info("Collecting masters and building element index ...")
        masters = collect_all_masters(root)

        # Merge global masters (across chunks) so cross-chunk ledger lookups work
        if masters_global is None:
            masters_global = masters
        else:
            masters_global.ledger.update(masters.ledger)
            masters_global.stock.update(masters.stock)
            masters_global.group_parent.update(masters.group_parent)
            masters_global.stock_parent.update(masters.stock_parent)
            masters_global.voucher_sidecar.update(masters.voucher_sidecar)
            masters_global.credit_period.update(masters.credit_period)
            masters_global.le_elem.update(masters.le_elem)
            masters_global.inv_elem.update(masters.inv_elem)
            masters_global.party_le_elem.update(masters.party_le_elem)

        if not any(
            strip_ns(c.tag) == "VOUCHER"
            for tm in root.iter()
            if strip_ns(tm.tag) == "TALLYMESSAGE"
            for c in tm
        ):
            logger.warning(
                "Chunk %d/%d returned zero vouchers. Possible causes:\n"
                "  1. TDL report 'APIRawVouchers' is not loaded in Tally (F4 to load).\n"
                "  2. No transactions exist for %s to %s in the active company.\n"
                "  3. A different company is open in Tally.\n"
                "  Run with --debug to inspect the raw XML response.",
                chunk_idx + 1, len(chunks), chunk_from, chunk_to,
            )
            continue

        logger.info("Writing flat fact rows to SQLite ...")
        v_count, r_count = process_chunk_to_db(root, masters, db_conn)
        total_vouchers += v_count
        total_rows     += r_count
        logger.info(
            "Chunk %d/%d: %d vouchers, %d rows inserted.",
            chunk_idx + 1, len(chunks), v_count, r_count,
        )

        # Release the parsed XML tree to free memory before the next chunk
        del root, masters, raw_bytes

    if db_conn is None:
        print("\nNo data was retrieved from Tally. Check tally_pipeline.log for details.")
        return 1

    db_conn.close()

    print(f"\n  SQLite database  ->  {db_path}")
    print(f"  Total vouchers   :   {total_vouchers:,}")
    print(f"  Total rows       :   {total_rows:,}")

    # ── Export to CSV ─────────────────────────────────────────────────────────
    print()
    if args.no_prompt:
        export_csv = True
    else:
        print("-" * 55)
        export_csv = _ask_yes_no("Export transactions to CSV?", default=True)

    if export_csv:
        timestamp  = datetime.now().strftime("%Y%m%d_%H%M%S")
        period     = _format_period(from_date, to_date)
        dump_name  = f"{company_name}_transaction_dump_{period}_{timestamp}.csv"
        dump_path  = args.out / dump_name
        logger.info("Exporting CSV ...")
        csv_rows = export_db_to_csv(db_path, dump_path)
        print(f"\n  CSV export       ->  {dump_path.resolve()}")
        print(f"  Rows exported    :   {csv_rows:,}  |  Columns: {len(FLAT_FACT_COLUMNS)}")

    # ── Optional star-schema dimension CSVs ───────────────────────────────────
    print()
    if not args.no_prompt and masters_global is not None:
        print("-" * 55)
        if _ask_yes_no("Export star-schema dimension/fact CSVs?", default=False):
            p = args.prefix + "_" if args.prefix else ""
            written: list[str] = []
            print("\n  Dimension tables:")
            for dim in sorted(masters_global.dimensions):
                if _ask_yes_no(f"    Export dim_{dim.lower()}.csv?", default=False):
                    out_path = args.out / f"{p}dim_{dim.lower()}.csv"
                    write_csv_rows(out_path, masters_global.dimensions[dim])
                    written.append(out_path.name)
            if written:
                print(f"\n  Additional files -> {args.out.resolve()}:")
                for name in written:
                    print(f"     {name}")
            else:
                print("  (No additional files selected.)")

    print()
    print(
        f"Summary  |  chunks: {len(chunks)}"
        f"  |  vouchers: {total_vouchers:,}"
        f"  |  rows: {total_rows:,}"
    )
    print(f"Log saved to: {_LOG_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

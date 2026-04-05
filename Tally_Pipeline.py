#!/usr/bin/env python3
"""
Tally Pipeline — single-script end-to-end:
  1. Fetch raw XML from Tally Prime via TDL API (in memory, no temp file).
  2. Parse the XML envelope into a star-schema result.
  3. Build the flat fact table (transaction dump).
  4. Export to CSV under the output directory.

Usage (interactive):
  python Tally_Pipeline.py

Usage (non-interactive / batch):
  python Tally_Pipeline.py --from 20250401 --to 20260331 --out ./out --no-prompt

The script embeds all logic from:
  - API_Extractor.py          (Tally HTTP fetch)
  - tally_xml_star_schema.py  (XML → star schema)
  - Tally_Transaction_Dump_Creator.py  (flat fact / CSV export)

No intermediate XML file is written to disk.
"""

from __future__ import annotations

# ── stdlib ────────────────────────────────────────────────────────────────────
import argparse
import calendar
import csv
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from datetime import datetime   # <-- added for timestamp

# ── third-party ───────────────────────────────────────────────────────────────
import requests

# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("tally_pipeline.log"),
    ],
)
logger = logging.getLogger(__name__)


# =============================================================================
# SECTION 1 — XML helpers  (from tally_xml_star_schema.py)
# =============================================================================

def strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def sanitize_tally_xml(raw: str) -> str:
    """Remove Tally control-character placeholders that are invalid in XML 1.0."""
    return re.sub(
        r"&#(\d+);",
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


@dataclass
class StarSchemaConfig:
    dimension_tags: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {
                "CURRENCY",
                "GROUP",
                "LEDGER",
                "STOCKGROUP",
                "STOCKITEM",
                "UNIT",
                "GODOWN",
                "VOUCHERTYPE",
                "TAXUNIT",
                "COMPANY",
            }
        )
    )
    voucher_tag: str = "VOUCHER"
    ledger_entries_tag: str = "ALLLEDGERENTRIES.LIST"
    inventory_entries_tag: str = "ALLINVENTORYENTRIES.LIST"
    inventory_alloc_tag: str = "INVENTORYALLOCATIONS.LIST"
    batch_tag: str = "BATCHALLOCATIONS.LIST"
    accounting_alloc_tag: str = "ACCOUNTINGALLOCATIONS.LIST"


@dataclass
class StarSchemaResult:
    dimensions: dict[str, list[dict[str, Any]]]
    fact_voucher: list[dict[str, Any]]
    fact_ledger_entry: list[dict[str, Any]]
    fact_inventory_line: list[dict[str, Any]]


def _entity_name_from_element(el: ET.Element) -> str | None:
    return el.get("NAME") or el.get("RESERVEDNAME")


_VOUCHER_SKIP_LISTS = frozenset(
    {
        "ALLLEDGERENTRIES.LIST",
        "INVENTORYENTRIESIN.LIST",
        "INVENTORYENTRIESOUT.LIST",
        "ALLINVENTORYENTRIES.LIST",
        "OLDAUDITENTRYIDS.LIST",
    }
)


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
    expected_fields = {"LEDGERNAME", "AMOUNT"}
    found_fields = set()
    for child in accounting_entry:
        if _text_leaf(child):
            field_name = strip_ns(child.tag)
            if field_name in expected_fields:
                found_fields.add(field_name)
    return len(found_fields) > 0


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
            led_name = acct_scalars.get("LEDGERNAME", "")
            led_amount = acct_scalars.get("AMOUNT", "")
            led_row: dict[str, Any] = {
                "voucher_guid": vch_guid,
                "voucher_vchkey": vchkey or "",
                "ledger_line_index": inv_i,
                "inventory_line_index": inv_i,
                "LEDGERNAME": led_name,
                "AMOUNT": led_amount,
                "ISPARTYLEDGER": acct_scalars.get("ISPARTYLEDGER", ""),
            }
            led_row.update(acct_scalars)
            ledger_rows.append(led_row)

        inv_rows.append(inv_row)
        inv_i += 1

    # Party ledger entries at voucher level (LEDGERENTRIES.LIST)
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
            "_local_line_index": len(ledger_rows),  # track for later lookup
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
    vch_entry_mode = ""
    for c in voucher:
        if strip_ns(c.tag) == "VCHENTRYMODE" and _text_leaf(c):
            vch_entry_mode = (c.text or "").strip()
            break

    has_item_invoice_structure = False
    for inv_list in voucher:
        if strip_ns(inv_list.tag) == "ALLINVENTORYENTRIES.LIST":
            for inv_entry in inv_list:
                for child in inv_entry:
                    if strip_ns(child.tag) == "ACCOUNTINGALLOCATIONS.LIST":
                        if _has_accounting_allocation_fields(child):
                            has_item_invoice_structure = True
                            break
                if has_item_invoice_structure:
                    break
        if has_item_invoice_structure:
            break

    has_ledger_entries = any(
        strip_ns(c.tag) == "LEDGERENTRIES.LIST" for c in voucher
    )

    voucher_attrib_indicators = any(
        ("item" in k.lower() and "invoice" in k.lower()) or
        (v and "item" in v.lower() and "invoice" in v.lower())
        for k, v in voucher.attrib.items()
    )

    is_item_invoice_mode = (
        vch_entry_mode == "Item Invoice"
        or has_item_invoice_structure
        or (has_ledger_entries and voucher_attrib_indicators)
    )

    if is_item_invoice_mode:
        logger.debug("Detected Item Invoice mode")
        return _extract_from_item_invoice_mode(voucher, vch_guid, vchkey)
    else:
        logger.debug("Detected Standard (As Voucher) mode")
        return _extract_from_standard_mode(voucher, vch_guid, vchkey)


def parse_tally_star_schema(
    root: ET.Element, config: StarSchemaConfig | None = None
) -> StarSchemaResult:
    config = config or StarSchemaConfig()
    dimensions: dict[str, list[dict[str, Any]]] = {t: [] for t in config.dimension_tags}
    fact_voucher: list[dict[str, Any]] = []
    fact_ledger_entry: list[dict[str, Any]] = []
    fact_inventory_line: list[dict[str, Any]] = []

    for tm in root.iter():
        if strip_ns(tm.tag) != "TALLYMESSAGE":
            continue
        for child in tm:
            tag = strip_ns(child.tag)
            if tag in config.dimension_tags:
                row = flatten_scalars(child, skip_lists=True)
                row["entity_tag"] = tag
                name = _entity_name_from_element(child)
                if name:
                    row["entity_name"] = name
                for ak, av in child.attrib.items():
                    row[f"attr_{ak.lower()}"] = av
                dimensions[tag].append(row)
            elif tag == config.voucher_tag:
                hdr = _voucher_header_row(child)
                guid = hdr.get("GUID", "")
                vchkey = child.get("VCHKEY") or hdr.get("ATTR_VCHKEY")
                fact_voucher.append(hdr)
                ledgers, invs = _ledger_lines_for_voucher(child, guid, vchkey)
                fact_ledger_entry.extend(ledgers)
                fact_inventory_line.extend(invs)

    dimensions = {k: v for k, v in dimensions.items() if v}
    return StarSchemaResult(
        dimensions=dimensions,
        fact_voucher=fact_voucher,
        fact_ledger_entry=fact_ledger_entry,
        fact_inventory_line=fact_inventory_line,
    )


def parse_xml_from_bytes(raw_bytes: bytes) -> ET.Element:
    """
    Decode raw bytes from Tally HTTP response and return an XML Element.
    Handles UTF-16 BOM (common in Tally Unicode exports) and UTF-8.
    """
    if raw_bytes[:2] in (b"\xff\xfe", b"\xfe\xff"):
        raw_str = raw_bytes.decode("utf-16")
    else:
        raw_str = raw_bytes.decode("utf-8", errors="replace")
    raw_str = sanitize_tally_xml(raw_str)
    return ET.fromstring(raw_str)


def write_csv_rows(path: Path, rows: list[dict[str, Any]]) -> None:
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
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def export_star_schema(
    result: StarSchemaResult, out_dir: Path, prefix: str = ""
) -> None:
    p = prefix + "_" if prefix else ""
    for dim_name, rows in result.dimensions.items():
        write_csv_rows(out_dir / f"{p}dim_{dim_name.lower()}.csv", rows)
    write_csv_rows(out_dir / f"{p}fact_voucher.csv", result.fact_voucher)
    write_csv_rows(out_dir / f"{p}fact_ledger_entry.csv", result.fact_ledger_entry)
    write_csv_rows(out_dir / f"{p}fact_inventory_line.csv", result.fact_inventory_line)


# =============================================================================
# SECTION 2 — Tally API fetch  (from API_Extractor.py)
# =============================================================================

def _validate_date_format(date_str: str) -> bool:
    return len(date_str) == 8 and date_str.isdigit()


def fetch_tally_xml_bytes(
    from_date: str,
    to_date: str,
    port: int = 9000,
    max_retries: int = 3,
    timeout: int = 60,
) -> bytes | None:
    """
    Call the TDL report 'APIRawVouchers' in Tally and return the raw XML bytes.
    Returns None on failure (errors are logged).
    """
    if not _validate_date_format(from_date):
        logger.error(f"Invalid start date format: {from_date}. Expected YYYYMMDD")
        return None
    if not _validate_date_format(to_date):
        logger.error(f"Invalid end date format: {to_date}. Expected YYYYMMDD")
        return None
    if int(from_date) > int(to_date):
        logger.error(f"Start date {from_date} cannot be after end date {to_date}")
        return None

    url = f"http://localhost:{port}/"
    req_xml = f"""
<ENVELOPE>
    <HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>
    <BODY>
        <EXPORTDATA>
            <REQUESTDESC>
                <REPORTNAME>APIRawVouchers</REPORTNAME>
                <STATICVARIABLES>
                    <SVFROMDATE>{from_date}</SVFROMDATE>
                    <SVTODATE>{to_date}</SVTODATE>
                </STATICVARIABLES>
            </REQUESTDESC>
        </EXPORTDATA>
    </BODY>
</ENVELOPE>
"""
    headers = {"Content-Type": "text/xml;charset=utf-8"}
    logger.info(f"Fetching 'APIRawVouchers' from Tally for period {from_date} → {to_date}")

    for attempt in range(max_retries):
        try:
            logger.info(f"Attempt {attempt + 1}/{max_retries} …")
            response = requests.post(url, data=req_xml, headers=headers, timeout=timeout)
            response.raise_for_status()

            data = response.content
            logger.info(f"✅ Received {len(data):,} bytes from Tally")
            if len(data) < 100:
                logger.warning(f"Response seems unusually small ({len(data)} bytes) — check TDL output")
            return data

        except requests.exceptions.ConnectionError as e:
            logger.error(f"Connection error on attempt {attempt + 1}: {e}")
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                logger.info(f"Retrying in {wait}s …")
                time.sleep(wait)
            else:
                logger.error("Max retries exceeded. Verify:")
                logger.error("  1. Tally ERP is running")
                logger.error("  2. TDL report 'APIRawVouchers' is loaded")
                logger.error("  3. Tally HTTP service is enabled (Gateway → HTTP)")
                logger.error(f"  4. Port {port} is accessible")

        except requests.exceptions.Timeout as e:
            logger.error(f"Timeout on attempt {attempt + 1} (timeout={timeout}s): {e}")
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                logger.info(f"Retrying in {wait}s …")
                time.sleep(wait)
            else:
                logger.error("Max retries exceeded. Consider increasing --timeout.")

        except requests.exceptions.HTTPError as e:
            logger.error(f"HTTP error: {e} | status {response.status_code}")
            logger.error(f"Response: {response.text[:500]}")
            break

        except Exception as e:
            logger.error(f"Unexpected error on attempt {attempt + 1}: {e}", exc_info=True)
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                logger.info(f"Retrying in {wait}s …")
                time.sleep(wait)

    return None


# =============================================================================
# SECTION 3 — Flat fact builder  (from Tally_Transaction_Dump_Creator.py)
# =============================================================================

FLAT_FACT_COLUMNS: tuple[str, ...] = (
    "entry_line_key",
    "voucher_natural_key",
    "posting_date",
    "voucher_type",
    "voucher_number",
    "voucher_entry_mode",
    "party_ledger_name",
    "gst_registration_name",
    "company_state",
    "ledger_name",
    "credit_period",
    "ledger_group_name",
    "primary_group_name",
    "is_party_ledger_line",
    "has_cost_centres_on_ledger",
    "cost_centre_name",
    "narration",
    "destination",
    "currency",
    "amount_absolute",
    "amount_tally_signed",
    "debit_credit_flag",
    "signed_amount_debit_positive",
    "pan",
    "pan_derived_from_gstin",
    "gstin",
    "udyam_registration_number",
    "msme_enterprise_category",
    "msme_activity_type",
    "msme_effective_from",
    "party_entity_type",
    "bill_to_name",
    "bill_to_address",
    "bill_to_state",
    "bill_to_pin",
    "ship_to_name",
    "ship_to_address",
    "ship_to_state",
    "ship_to_pin",
    "delivery_same_as_consignee",
    "dispatch_same_as_consignor",
    "eway_consignor_address",
    "eway_consignee_address",
    "e_way_bill_number",
    "e_way_bill_date",
    "e_invoice_irn",
    "e_invoice_ack_number",
    "e_invoice_ack_date",
    "is_reverse_charge_applicable",
    "is_unregistered_rcm",
    "stock_item_name",
    "stock_group_name",
    "godown_name",
    "batch_name",
    "quantity",
    "rate",
    "inventory_amount",
    "line_index",
    "inventory_line_index",
    "bill_line_index",
    "ledger_opening_balance",
    "tax_type",
    "stock_opening_balance",
    "stock_opening_value",
    "bill_name",
    "bill_date",
    "bill_amount",
    "bill_allocated_amount",
    "bill_type",
    "batch_mfg_date",
    "batch_expiry_period",
    "batch_tracking_number",
    "batch_order_no",
    "party_invoice_no",
    "party_invoice_date",
    "original_voucher_number",
    "original_voucher_date",
    "agreement_order_no",
    "goods_vehicle_number",
    "reference",
)

UDYAM_KEY_TAGS = (
    "UDYAMREGISTRATIONNO", "UDYAMNO", "UDYAMNUMBER",
    "MSMEREGISTRATIONNO", "REGISTRATIONNO", "REGISTRATIONNUMBER",
)
PAN_KEY_TAGS = ("INCOMETAXPAN", "PAN", "ITPAN", "INCOMETAXNUMBER")
EWAY_NO_KEYS = ("EWAYBILLNO", "EWAYBILLNUMBER", "EWBNO")
EWAY_DATE_KEYS = ("EWAYBILLDATE", "EWAYBILLDT", "EWAYDATE")
IRN_KEYS = ("IRN", "IRNNO", "EINVOICEIRN")
ACK_NO_KEYS = ("ACKNO", "ACKNUMBER", "ACKNOWLEDGEMENTNO")
ACK_DATE_KEYS = ("ACKDATE", "ACKNOWLEDGEMENTDATE")
GST_REG_TAGS = ("LEDGSTREGDETAILS.LIST", "LEDGSTREGDEATALS.LIST", "GSTDETAILS.LIST")


def _parse_decimal(s: str | None) -> Decimal:
    if not s or not str(s).strip():
        return Decimal(0)
    try:
        return Decimal(str(s).strip().replace(",", ""))
    except InvalidOperation:
        return Decimal(0)


def _pan_from_gstin(gstin: str) -> str:
    g = (gstin or "").strip().upper()
    if len(g) == 15 and g[:2].isdigit():
        return g[2:12]
    return ""


def _direct_scalar_children(el: Any) -> dict[str, str]:
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


def _first_nested_block_flat(el: Any) -> dict[str, str]:
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


def _format_address_blob(d: dict[str, str]) -> str:
    parts = [d[k] for k in sorted(d.keys()) if "ADDRESS" in k.upper()]
    if parts:
        return " | ".join(parts)
    return d.get("ADDRESS", "") or ""


def _pick_first(d: dict[str, str], keys: tuple[str, ...]) -> str:
    for k in keys:
        if d.get(k):
            return d[k]
    return ""


def _parse_msme_block(led: Any) -> dict[str, str]:
    out = {
        "udyam_registration_number": "",
        "msme_enterprise_category": "",
        "msme_activity_type": "",
        "msme_effective_from": "",
    }
    for ch in led:
        if strip_ns(ch.tag) != "MSMEREGISTRATIONDETAILS.LIST":
            continue
        raw = _direct_scalar_children(ch)
        if not raw:
            raw = _first_nested_block_flat(ch)
        out["msme_effective_from"] = raw.get("FROMDATE", "")
        out["msme_enterprise_category"] = raw.get("ENTERPRISETYPE", "")
        out["msme_activity_type"] = raw.get("MSMEACTIVITYTYPE", "")
        for uk in UDYAM_KEY_TAGS:
            if raw.get(uk):
                out["udyam_registration_number"] = raw[uk]
                break
        if not out["udyam_registration_number"]:
            for k, v in raw.items():
                if v and re.search(r"UDYAM|Udyam", k, re.I):
                    out["udyam_registration_number"] = v
                    break
        break
    return out


def _parse_gstin_and_pan_from_ledger(led: Any) -> tuple[str, str]:
    gstin, pan = "", ""
    for ch in led:
        if _is_list_element(ch) or not _text_leaf(ch):
            continue
        t = strip_ns(ch.tag)
        if t in PAN_KEY_TAGS:
            val = (ch.text or "").strip()
            if val:
                pan = val
                break
    for ch in led:
        tag = strip_ns(ch.tag)
        if tag in GST_REG_TAGS:
            raw = _direct_scalar_children(ch) or _first_nested_block_flat(ch)
            if not gstin:
                gstin = raw.get("GSTIN", "")
            if not pan:
                for pk in PAN_KEY_TAGS:
                    if raw.get(pk):
                        pan = raw[pk]
                        break
            if gstin and pan:
                break
    return gstin, pan


def _parse_party_entity_type(led: Any) -> str:
    for ch in led:
        t = strip_ns(ch.tag)
        if t in ("STATUTORYDETAILS.LIST", "PARTYDETAILS.LIST", "LEDSTATUTORY.LIST"):
            raw = _direct_scalar_children(ch) or _first_nested_block_flat(ch)
            for k in ("CONSTITUTIONNAME", "ENTITYTYPE", "ORGANIZATIONTYPE", "PARTYTYPE"):
                if raw.get(k):
                    return raw[k]
    return ""


def collect_ledger_masters(root: Any) -> dict[str, dict[str, Any]]:
    masters: dict[str, dict[str, Any]] = {}
    for tm in root.iter():
        if strip_ns(tm.tag) != "TALLYMESSAGE":
            continue
        for led in tm:
            if strip_ns(led.tag) != "LEDGER":
                continue
            name = _entity_name_from_element(led)
            if not name:
                continue
            parent = next(
                ((c.text or "").strip() for c in led
                 if strip_ns(c.tag) == "PARENT" and _text_leaf(c)), ""
            )
            cur = next(
                ((c.text or "").strip() for c in led
                 if strip_ns(c.tag) == "CURRENCYNAME" and _text_leaf(c)), ""
            )
            cc_on = next(
                ((c.text or "").strip() for c in led
                 if strip_ns(c.tag) == "ISCOSTCENTRESON" and _text_leaf(c)), ""
            )
            opening_balance = next(
                ((c.text or "").strip() for c in led
                 if strip_ns(c.tag) == "OPENINGBALANCE" and _text_leaf(c)), ""
            )
            tax_type = next(
                ((c.text or "").strip() for c in led
                 if strip_ns(c.tag) == "TAXTYPE" and _text_leaf(c)), ""
            )
            gstin, pan = _parse_gstin_and_pan_from_ledger(led)
            msme = _parse_msme_block(led)
            entity_type = _parse_party_entity_type(led)
            masters[name] = {
                "ledger_group_name": parent,
                "currency": cur,
                "has_cost_centres_on_ledger": cc_on,
                "gstin": gstin,
                "pan": pan,
                "party_entity_type": entity_type,
                "opening_balance": opening_balance,
                "tax_type": tax_type,
                **msme,
            }
    return masters


def collect_stock_masters(root: Any) -> dict[str, dict[str, Any]]:
    masters: dict[str, dict[str, Any]] = {}
    for tm in root.iter():
        if strip_ns(tm.tag) != "TALLYMESSAGE":
            continue
        for si in tm:
            if strip_ns(si.tag) != "STOCKITEM":
                continue
            name = _entity_name_from_element(si)
            if not name:
                continue
            opening_qty = next(
                ((c.text or "").strip() for c in si
                 if strip_ns(c.tag) == "OPENINGBALANCE" and _text_leaf(c)), ""
            )
            opening_value = next(
                ((c.text or "").strip() for c in si
                 if strip_ns(c.tag) == "OPENINGVALUE" and _text_leaf(c)), ""
            )
            masters[name] = {"opening_qty": opening_qty, "opening_value": opening_value}
    return masters


def collect_group_parents(root: Any) -> dict[str, str]:
    parent_of: dict[str, str] = {}
    for tm in root.iter():
        if strip_ns(tm.tag) != "TALLYMESSAGE":
            continue
        for g in tm:
            if strip_ns(g.tag) != "GROUP":
                continue
            name = _entity_name_from_element(g)
            if not name:
                continue
            p = next(
                ((c.text or "").strip() for c in g
                 if strip_ns(c.tag) == "PARENT" and _text_leaf(c)), ""
            )
            parent_of[name] = p
    return parent_of


def primary_group(group_name: str, parent_of: dict[str, str]) -> str:
    if not group_name:
        return ""
    cur = group_name
    seen: set[str] = set()
    while parent_of.get(cur) and cur not in seen:
        seen.add(cur)
        cur = parent_of[cur]
    return cur


def collect_stockitem_parents(root: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    for tm in root.iter():
        if strip_ns(tm.tag) != "TALLYMESSAGE":
            continue
        for si in tm:
            if strip_ns(si.tag) != "STOCKITEM":
                continue
            name = _entity_name_from_element(si)
            if not name:
                continue
            p = next(
                ((c.text or "").strip() for c in si
                 if strip_ns(c.tag) == "PARENT" and _text_leaf(c)), ""
            )
            out[name] = p
    return out


def _find_list(voucher: Any, list_name: str) -> Any:
    for c in voucher:
        if strip_ns(c.tag) == list_name:
            return c
    return None


def parse_voucher_sidecar(voucher: Any) -> dict[str, str]:
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

    _tag_map = {
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

    for c in voucher:
        t = strip_ns(c.tag)
        if t in _tag_map and _text_leaf(c):
            out[_tag_map[t]] = (c.text or "").strip()

    def addr(prefix: str, list_name: str) -> None:
        lst = _find_list(voucher, list_name)
        if lst is None:
            return
        blk = _direct_scalar_children(lst) or _first_nested_block_flat(lst)
        if not blk:
            return
        out[f"{prefix}_name"] = blk.get("NAME") or blk.get("BILLTO") or blk.get("PARTYNAME") or ""
        out[f"{prefix}_state"] = blk.get("STATE") or blk.get("STATENAME") or ""
        out[f"{prefix}_pin"] = blk.get("PINCODE") or blk.get("PIN") or ""
        out[f"{prefix}_address"] = _format_address_blob(blk) or blk.get("ADDRESS", "") or ""

    addr("bill_to", "GSTBUYERADDRESS.LIST")
    addr("ship_to", "GSTCONSIGNEEADDRESS.LIST")

    for list_name, key_a in (
        ("GSTEWAYCONSIGNORADDRESS.LIST", "eway_consignor_address"),
        ("GSTEWAYCONSIGNEEADDRESS.LIST", "eway_consignee_address"),
    ):
        lst = _find_list(voucher, list_name)
        if lst is None:
            continue
        blk = _direct_scalar_children(lst) or _first_nested_block_flat(lst)
        if blk:
            out[key_a] = " | ".join(f"{k}={v}" for k, v in sorted(blk.items()) if v)

    ew = _find_list(voucher, "EWAYBILLDETAILS.LIST")
    if ew is not None:
        blk = _direct_scalar_children(ew) or _first_nested_block_flat(ew)
        out["e_way_bill_number"] = _pick_first(blk, EWAY_NO_KEYS)
        out["e_way_bill_date"] = _pick_first(blk, EWAY_DATE_KEYS)

    for einv_tag in ("GSTEINVOICEDETAILS.LIST", "EINVOICEDETAILS.LIST"):
        ei = _find_list(voucher, einv_tag)
        if ei is None:
            continue
        blk = _direct_scalar_children(ei) or _first_nested_block_flat(ei)
        out["e_invoice_irn"] = _pick_first(blk, IRN_KEYS)
        out["e_invoice_ack_number"] = _pick_first(blk, ACK_NO_KEYS)
        out["e_invoice_ack_date"] = _pick_first(blk, ACK_DATE_KEYS)
        if out["e_invoice_irn"]:
            break

    if not out["voucher_entry_mode"]:
        out["voucher_entry_mode"] = "As Voucher"

    return out


def _extract_bill_credit_period_from_ledger_entry(le: Any) -> str:
    for c in le:
        if strip_ns(c.tag) != "BILLALLOCATIONS.LIST":
            continue
        for f in c:
            if strip_ns(f.tag) != "BILLCREDITPERIOD":
                continue
            txt = (f.text or "").strip()
            return txt if txt else (f.get("P") or "").strip()
    return ""


def collect_credit_period_by_ledger_line(root: Any) -> dict[tuple[str, int], str]:
    out: dict[tuple[str, int], str] = {}
    for tm in root.iter():
        if strip_ns(tm.tag) != "TALLYMESSAGE":
            continue
        for vch in tm:
            if strip_ns(vch.tag) != "VOUCHER":
                continue
            guid = ""
            for c in vch:
                if strip_ns(c.tag) == "GUID" and _text_leaf(c):
                    guid = (c.text or "").strip()
                    break
            idx = 0
            for le in vch:
                if strip_ns(le.tag) != "ALLLEDGERENTRIES.LIST":
                    continue
                cp = _extract_bill_credit_period_from_ledger_entry(le)
                if cp:
                    out[(guid, idx)] = cp
                idx += 1
    return out


def _voucher_guid(voucher: Any) -> str:
    for c in voucher:
        if strip_ns(c.tag) == "GUID" and _text_leaf(c):
            return (c.text or "").strip()
    return ""


def build_inventory_index(
    inv_rows: list[dict[str, Any]],
) -> dict[tuple[str, int], list[dict[str, Any]]]:
    idx: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for r in inv_rows:
        g = str(r.get("voucher_guid", ""))
        li = int(r.get("ledger_line_index", -1))
        if r.get("STOCKITEMNAME"):
            idx.setdefault((g, li), []).append(r)
    for k in idx:
        idx[k].sort(key=lambda x: int(x.get("inventory_line_index", 0)))
    return idx


def _find_ledger_entry_original(root: Any, voucher_guid: str, line_index: int) -> Any:
    for tm in root.iter():
        if strip_ns(tm.tag) != "TALLYMESSAGE":
            continue
        for vch in tm:
            if strip_ns(vch.tag) != "VOUCHER":
                continue
            if _voucher_guid(vch) != voucher_guid:
                continue
            idx = 0
            for le in vch:
                if strip_ns(le.tag) != "ALLLEDGERENTRIES.LIST":
                    continue
                if idx == line_index:
                    return le
                idx += 1
    return None


def _find_party_ledger_entry_original(root: Any, voucher_guid: str, local_index: int) -> Any:
    for tm in root.iter():
        if strip_ns(tm.tag) != "TALLYMESSAGE":
            continue
        for vch in tm:
            if strip_ns(vch.tag) != "VOUCHER":
                continue
            if _voucher_guid(vch) != voucher_guid:
                continue
            idx = 0
            for child in vch:
                if strip_ns(child.tag) != "LEDGERENTRIES.LIST":
                    continue
                if idx == local_index:
                    return child
                idx += 1
    return None


def _extract_inventory_line_original(root: Any, voucher_guid: str, ledger_line_index: int, inv_line_index: int) -> Any:
    # Standard mode: under ALLLEDGERENTRIES.LIST
    le = _find_ledger_entry_original(root, voucher_guid, ledger_line_index)
    if le is not None:
        idx = 0
        for inv in le:
            tag = strip_ns(inv.tag)
            if tag in ("INVENTORYALLOCATIONS.LIST", "ALLINVENTORYENTRIES.LIST"):
                if idx == inv_line_index:
                    return inv
                idx += 1
    # Item Invoice mode: direct ALLINVENTORYENTRIES.LIST on voucher
    for tm in root.iter():
        if strip_ns(tm.tag) != "TALLYMESSAGE":
            continue
        for vch in tm:
            if strip_ns(vch.tag) != "VOUCHER":
                continue
            if _voucher_guid(vch) != voucher_guid:
                continue
            for inv_list in vch:
                if strip_ns(inv_list.tag) != "ALLINVENTORYENTRIES.LIST":
                    continue
                idx = 0
                for inv_entry in inv_list:
                    has_stock = any(strip_ns(c.tag) == "STOCKITEMNAME" and c.text for c in inv_entry)
                    if has_stock:
                        if idx == inv_line_index:
                            return inv_entry
                        idx += 1
    return None


def _extract_batch_details(inv_alloc: Any) -> dict[str, str]:
    out = {
        "batch_mfg_date": "",
        "batch_expiry_period": "",
        "batch_tracking_number": "",
        "batch_order_no": "",
    }
    for c in inv_alloc:
        if strip_ns(c.tag) != "BATCHALLOCATIONS.LIST":
            continue
        for batch in c:
            for field in batch:
                ft = strip_ns(field.tag)
                if ft == "MFDON":
                    out["batch_mfg_date"] = (field.text or "").strip()
                elif ft == "EXPIRYPERIOD":
                    out["batch_expiry_period"] = (field.text or "").strip()
                elif ft == "TRACKINGNUMBER":
                    out["batch_tracking_number"] = (field.text or "").strip()
                elif ft == "ORDERNO":
                    out["batch_order_no"] = (field.text or "").strip()
            break
        break
    return out


def _extract_bill_allocations(le: Any) -> list[dict[str, str]]:
    allocations: list[dict[str, str]] = []
    for c in le:
        if strip_ns(c.tag) != "BILLALLOCATIONS.LIST":
            continue
        name = date = amount = allocated = typ = ""
        for field in c:
            ft = strip_ns(field.tag)
            if ft in ("NAME", "n"):
                name = (field.text or "").strip()
            elif ft == "BILLDATE":
                date = (field.text or "").strip()
            elif ft == "BILLAMOUNT":
                amount = (field.text or "").strip()
            elif ft == "AMOUNT":
                allocated = (field.text or "").strip()
            elif ft == "BILLTYPE":
                typ = (field.text or "").strip()
        allocations.append({
            "bill_name": name,
            "bill_date": date,
            "bill_amount": amount,
            "bill_allocated_amount": allocated,
            "bill_type": typ,
        })
    return allocations


def build_flat_fact_rows(
    root: ET.Element, result: StarSchemaResult
) -> list[dict[str, Any]]:
    ledger_m = collect_ledger_masters(root)
    stock_m = collect_stock_masters(root)
    parent_of = collect_group_parents(root)
    stock_parent = collect_stockitem_parents(root)

    voucher_sidecar: dict[str, dict[str, str]] = {}
    for tm in root.iter():
        if strip_ns(tm.tag) != "TALLYMESSAGE":
            continue
        for child in tm:
            if strip_ns(child.tag) != "VOUCHER":
                continue
            g = _voucher_guid(child)
            if g:
                voucher_sidecar[g] = parse_voucher_sidecar(child)

    inv_index = build_inventory_index(result.fact_inventory_line)
    credit_period_index = collect_credit_period_by_ledger_line(root)
    vch_by_guid = {r.get("GUID"): r for r in result.fact_voucher if r.get("GUID")}

    flat: list[dict[str, Any]] = []

    for le in result.fact_ledger_entry:
        g = str(le.get("voucher_guid", ""))
        li = int(le.get("ledger_line_index", 0))
        vch = vch_by_guid.get(g, {})
        side = voucher_sidecar.get(g, {})

        nat = str(vch.get("ATTR_VCHKEY") or vch.get("GUID") or g)
        ledger_name = str(le.get("LEDGERNAME", ""))

        lm = ledger_m.get(ledger_name, {})
        lg = lm.get("ledger_group_name") or ""
        pg = primary_group(lg, parent_of)

        gstin = str(lm.get("gstin", ""))
        pan_explicit = str(lm.get("pan", ""))
        pan_derived = _pan_from_gstin(gstin)

        amt_raw = str(le.get("AMOUNT", "") or "0")
        tally_amt = _parse_decimal(amt_raw)
        signed = -tally_amt
        dcf = "Dr" if signed > 0 else ("Cr" if signed < 0 else "")

        date_raw = str(vch.get("DATE") or vch.get("EFFECTIVEDATE") or "")
        posting = ""
        if len(date_raw) == 8 and date_raw.isdigit():
            posting = f"{date_raw[:4]}-{date_raw[4:6]}-{date_raw[6:8]}"

        invs = inv_index.get((g, li), [])
        inv0 = invs[0] if invs else None
        stk_name = str(inv0.get("STOCKITEMNAME", "")) if inv0 else ""
        stk_group = stock_parent.get(stk_name, "")
        credit_period = credit_period_index.get((g, li), "")

        item_invoice_source = str(le.get("_item_invoice_source", ""))
        if item_invoice_source == "LEDGERENTRIES":
            local_idx = int(le.get("_local_line_index", 0))
            bill_alloc = _find_party_ledger_entry_original(root, g, local_idx)
        else:
            bill_alloc = _find_ledger_entry_original(root, g, li)

        bill_allocs: list[dict[str, str]] = (
            _extract_bill_allocations(bill_alloc) if bill_alloc is not None else []
        )
        if not bill_allocs:
            bill_allocs = [{"bill_name": "", "bill_date": "", "bill_amount": "",
                            "bill_allocated_amount": "", "bill_type": ""}]

        batch_details = {}
        if inv0 and inv0.get("inventory_line_index") is not None:
            inv_alloc_original = _extract_inventory_line_original(
                root, g, li, int(inv0.get("inventory_line_index", 0))
            )
            if inv_alloc_original is not None:
                batch_details = _extract_batch_details(inv_alloc_original)

        for bill_idx, bill_d in enumerate(bill_allocs):
            entry_key = f"{nat}|{li}|{bill_idx}"

            raw_alloc = bill_d.get("bill_allocated_amount", "")
            if raw_alloc:
                row_amt = _parse_decimal(raw_alloc)
                row_signed = -row_amt
                row_dcf = "Dr" if row_signed > 0 else ("Cr" if row_signed < 0 else "")
            else:
                row_amt = tally_amt
                row_signed = signed
                row_dcf = dcf

            row: dict[str, Any] = {
                "entry_line_key": entry_key,
                "voucher_natural_key": nat,
                "posting_date": posting,
                "voucher_type": str(vch.get("VOUCHERTYPENAME", "") or vch.get("ATTR_VCHTYPE", "")),
                "voucher_number": str(vch.get("VOUCHERNUMBER", "")),
                "voucher_entry_mode": side.get("voucher_entry_mode", ""),
                "party_ledger_name": str(vch.get("PARTYLEDGERNAME", "")),
                "gst_registration_name": side.get("gst_registration_name") or str(vch.get("GSTREGISTRATION", "")),
                "company_state": side.get("company_state") or str(vch.get("CMPGSTSTATE", "")),
                "ledger_name": ledger_name,
                "credit_period": credit_period,
                "ledger_group_name": lg,
                "primary_group_name": pg,
                "is_party_ledger_line": str(le.get("ISPARTYLEDGER", "")),
                "has_cost_centres_on_ledger": str(lm.get("has_cost_centres_on_ledger", "")),
                "cost_centre_name": "",
                "narration": side.get("narration", ""),
                "destination": side.get("destination") or str(vch.get("BASICFINALDESTINATION", "")),
                "currency": str(lm.get("currency", "")),
                "amount_absolute": str(abs(row_amt)),
                "amount_tally_signed": str(row_amt),
                "debit_credit_flag": row_dcf,
                "signed_amount_debit_positive": str(row_signed),
                "pan": pan_explicit,
                "pan_derived_from_gstin": pan_derived,
                "gstin": gstin,
                "udyam_registration_number": str(lm.get("udyam_registration_number", "")),
                "msme_enterprise_category": str(lm.get("msme_enterprise_category", "")),
                "msme_activity_type": str(lm.get("msme_activity_type", "")),
                "msme_effective_from": str(lm.get("msme_effective_from", "")),
                "party_entity_type": str(lm.get("party_entity_type", "")),
                "bill_to_name": side.get("bill_to_name", ""),
                "bill_to_address": side.get("bill_to_address", ""),
                "bill_to_state": side.get("bill_to_state", ""),
                "bill_to_pin": side.get("bill_to_pin", ""),
                "ship_to_name": side.get("ship_to_name", ""),
                "ship_to_address": side.get("ship_to_address", ""),
                "ship_to_state": side.get("ship_to_state", ""),
                "ship_to_pin": side.get("ship_to_pin", ""),
                "delivery_same_as_consignee": side.get("delivery_same_as_consignee", ""),
                "dispatch_same_as_consignor": side.get("dispatch_same_as_consignor", ""),
                "eway_consignor_address": side.get("eway_consignor_address", ""),
                "eway_consignee_address": side.get("eway_consignee_address", ""),
                "e_way_bill_number": side.get("e_way_bill_number", ""),
                "e_way_bill_date": side.get("e_way_bill_date", ""),
                "e_invoice_irn": side.get("e_invoice_irn", ""),
                "e_invoice_ack_number": side.get("e_invoice_ack_number", ""),
                "e_invoice_ack_date": side.get("e_invoice_ack_date", ""),
                "is_reverse_charge_applicable": side.get("is_reverse_charge_applicable") or str(vch.get("ISREVERSECHARGEAPPLICABLE", "")),
                "is_unregistered_rcm": side.get("is_unregistered_rcm") or str(vch.get("VCHSTATUSISUNREGISTEREDRCM", "")),
                "stock_item_name": stk_name,
                "stock_group_name": stk_group,
                "godown_name": str(inv0.get("batch_GODOWNNAME", "")) if inv0 else "",
                "batch_name": str(inv0.get("batch_BATCHNAME", "")) if inv0 else "",
                "quantity": str(inv0.get("ACTUALQTY", "")) if inv0 else "",
                "rate": str(inv0.get("RATE", "")) if inv0 else "",
                "inventory_amount": str(inv0.get("AMOUNT", "")) if inv0 else "",
                "line_index": li,
                "inventory_line_index": int(inv0.get("inventory_line_index", 0)) if inv0 else -1,
                "bill_line_index": bill_idx,
                "ledger_opening_balance": str(lm.get("opening_balance", "")),
                "tax_type": str(lm.get("tax_type", "")),
                "stock_opening_balance": str(stock_m.get(stk_name, {}).get("opening_qty", "")),
                "stock_opening_value": str(stock_m.get(stk_name, {}).get("opening_value", "")),
                "bill_name": bill_d["bill_name"],
                "bill_date": bill_d["bill_date"],
                "bill_amount": bill_d["bill_amount"],
                "bill_allocated_amount": bill_d["bill_allocated_amount"],
                "bill_type": bill_d["bill_type"],
                "batch_mfg_date": batch_details.get("batch_mfg_date", ""),
                "batch_expiry_period": batch_details.get("batch_expiry_period", ""),
                "batch_tracking_number": batch_details.get("batch_tracking_number", ""),
                "batch_order_no": batch_details.get("batch_order_no", ""),
                "party_invoice_no": side.get("party_invoice_no", ""),
                "party_invoice_date": side.get("party_invoice_date", ""),
                "original_voucher_number": side.get("original_voucher_number", ""),
                "original_voucher_date": side.get("original_voucher_date", ""),
                "agreement_order_no": side.get("agreement_order_no", ""),
                "goods_vehicle_number": side.get("goods_vehicle_number", ""),
                "reference": side.get("reference", ""),
            }
            flat.append({c: row.get(c, "") for c in FLAT_FACT_COLUMNS})

    return flat


def export_flat_fact(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text(",".join(FLAT_FACT_COLUMNS) + "\n", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(FLAT_FACT_COLUMNS), extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FLAT_FACT_COLUMNS})


# =============================================================================
# SECTION 4 — Output filename helpers
# =============================================================================

def _extract_company_name(root: ET.Element) -> str:
    for el in root.iter():
        if strip_ns(el.tag) == "SVCURRENTCOMPANY" and el.text and el.text.strip():
            return el.text.strip()
    return "company"


def _voucher_date_range(fact_voucher: list[dict[str, Any]]) -> tuple[str, str]:
    dates = [
        str(row.get("DATE") or row.get("EFFECTIVEDATE") or "").strip()
        for row in fact_voucher
        if len(str(row.get("DATE") or row.get("EFFECTIVEDATE") or "").strip()) == 8
        and str(row.get("DATE") or row.get("EFFECTIVEDATE") or "").strip().isdigit()
    ]
    return (min(dates), max(dates)) if dates else ("", "")


def _format_period(min_date: str, max_date: str) -> str:
    def fmt(d: str) -> str:
        y, m = d[:4], d[4:6]
        return f"{calendar.month_abbr[int(m)]}-{y[2:]}"
    if not min_date or not max_date:
        return "unknown-period"
    if min_date[:6] == max_date[:6]:
        return fmt(min_date)
    return f"{fmt(min_date)}-{fmt(max_date)}"


def _safe_filename(name: str) -> str:
    return re.sub(r'[\/*?:"<>|]', "_", name).strip()


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


# =============================================================================
# SECTION 5 — Main entry point
# =============================================================================

def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Tally Pipeline — fetch XML from Tally Prime → parse in memory → export CSV.\n"
            "No intermediate XML file is written to disk."
        )
    )
    ap.add_argument("--from", dest="from_date", default=None,
                    help="Start date YYYYMMDD (prompts if omitted)")
    ap.add_argument("--to", dest="to_date", default=None,
                    help="End date YYYYMMDD (prompts if omitted)")
    ap.add_argument("--port", type=int, default=9000,
                    help="Tally HTTP port (default: 9000)")
    ap.add_argument("--out", type=Path, default=Path("tally_out"),
                    help="Output directory (default: ./tally_out)")
    ap.add_argument("--prefix", default="",
                    help="Optional filename prefix for star-schema CSVs")
    ap.add_argument("--retries", type=int, default=3,
                    help="Max HTTP retry attempts (default: 3)")
    ap.add_argument("--timeout", type=int, default=60,
                    help="HTTP timeout in seconds (default: 60)")
    ap.add_argument("--no-prompt", action="store_true",
                    help="Skip interactive prompts; export only the transaction dump")
    args = ap.parse_args()

    print("\n══════════════════════════════════════════")
    print("  Tally Pipeline — Fetch → Parse → Export")
    print("══════════════════════════════════════════\n")

    # ── Step 1: Collect dates ────────────────────────────────────────────────
    from_date = args.from_date
    to_date = args.to_date

    if not from_date:
        from_date = input("Enter Start Date (YYYYMMDD) [e.g., 20250401]: ").strip()
    if not to_date:
        to_date = input("Enter End Date   (YYYYMMDD) [e.g., 20260331]: ").strip()

    if not from_date or not to_date:
        print("❌ Both dates are required.")
        return 1

    # ── Step 2: Fetch XML bytes from Tally (in memory) ───────────────────────
    raw_bytes = fetch_tally_xml_bytes(
        from_date, to_date,
        port=args.port,
        max_retries=args.retries,
        timeout=args.timeout,
    )

    if raw_bytes is None:
        print("\n❌ Failed to fetch data from Tally. Check tally_pipeline.log for details.")
        return 1

    # ── Step 3: Parse XML in memory ──────────────────────────────────────────
    logger.info("Parsing XML envelope …")
    try:
        root = parse_xml_from_bytes(raw_bytes)
    except ET.ParseError as e:
        logger.error(f"XML parse error: {e}")
        print("\n❌ Tally returned malformed XML. Check tally_pipeline.log.")
        return 1

    result = parse_tally_star_schema(root)
    logger.info(
        f"Parsed — vouchers: {len(result.fact_voucher)} | "
        f"ledger lines: {len(result.fact_ledger_entry)} | "
        f"inventory lines: {len(result.fact_inventory_line)}"
    )

    # ── Step 4: Build flat fact rows ─────────────────────────────────────────
    logger.info("Building flat fact table …")
    flat_rows = build_flat_fact_rows(root, result)

    # ── Step 5: Derive output filename with timestamp ────────────────────────
    company = _safe_filename(_extract_company_name(root))
    min_d, max_d = _voucher_date_range(result.fact_voucher)
    period = _format_period(min_d, max_d)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    # Insert timestamp before the .csv extension
    dump_name = f"{company}_transaction_dump_{period}_{timestamp}.csv"

    args.out.mkdir(parents=True, exist_ok=True)
    dump_path = args.out / dump_name
    export_flat_fact(dump_path, flat_rows)

    print(f"\n✓  Transaction dump  →  {dump_path.resolve()}")
    print(f"   Rows: {len(flat_rows):,}  |  Columns: {len(FLAT_FACT_COLUMNS)}")

    # ── Step 6: Optional star-schema CSVs ────────────────────────────────────
    print()
    if args.no_prompt:
        export_other = False
    else:
        print("─" * 55)
        print("Optional: export additional star-schema CSVs")
        print("─" * 55)
        export_other = _ask_yes_no("Export additional star-schema CSVs?", default=False)

    if export_other:
        p = args.prefix + "_" if args.prefix else ""
        written: list[str] = []

        print("\n  Dimension tables:")
        dim_choices: dict[str, bool] = {}
        for dim in sorted(result.dimensions):
            dim_choices[dim] = _ask_yes_no(f"    Export dim_{dim.lower()}.csv?", default=False)

        print("\n  Fact tables:")
        want_voucher   = _ask_yes_no("    Export fact_voucher.csv?",        default=False)
        want_ledger    = _ask_yes_no("    Export fact_ledger_entry.csv?",   default=False)
        want_inventory = _ask_yes_no("    Export fact_inventory_line.csv?", default=False)

        for dim, wanted in dim_choices.items():
            if wanted:
                out_path = args.out / f"{p}dim_{dim.lower()}.csv"
                write_csv_rows(out_path, result.dimensions[dim])
                written.append(out_path.name)

        if want_voucher:
            out_path = args.out / f"{p}fact_voucher.csv"
            write_csv_rows(out_path, result.fact_voucher)
            written.append(out_path.name)
        if want_ledger:
            out_path = args.out / f"{p}fact_ledger_entry.csv"
            write_csv_rows(out_path, result.fact_ledger_entry)
            written.append(out_path.name)
        if want_inventory:
            out_path = args.out / f"{p}fact_inventory_line.csv"
            write_csv_rows(out_path, result.fact_inventory_line)
            written.append(out_path.name)

        if written:
            print(f"\n✓  Additional files written to {args.out.resolve()}:")
            for name in written:
                print(f"   • {name}")
        else:
            print("\n  (No additional files selected.)")

    print()
    print(
        f"Summary — vouchers: {len(result.fact_voucher)}"
        f"  |  ledger lines: {len(result.fact_ledger_entry)}"
        f"  |  inventory lines: {len(result.fact_inventory_line)}"
        f"  |  flat fact rows: {len(flat_rows)}"
    )
    print("\nLog saved to: tally_pipeline.log")
    return 0


if __name__ == "__main__":
    exit(main())
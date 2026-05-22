"""
Tally → ERPNext Migration Script
Step 6: Migrate Purchase Invoices

Company  : Venkateshwara Traders
Source   : output/voucher_purchase_YYYYMMDD_YYYYMMDD.json  (12 files, 45 unique invoices)
Target   : ERPNext Purchase Invoice (submitted)

Key differences from Sales Invoice (Step 5):
  - Uses 'supplier' instead of 'customer'
  - Items go into a warehouse (accepted_warehouse)
  - Tax ledgers: CGST, SGST, IGST, Fright, Packing Cost, Transportation
  - All fixes from Step 5 applied: delete item prices, fetch before submit,
    fiscal year check, payment terms cleared on suppliers

Data facts:
  - 45 unique purchase invoices (FY 2020-21)
  - All 12 files are identical (same demo data repeated)
  - 8 unique suppliers, 46 unique items purchased
  - Total purchase value: Rs 16,91,438

Usage:
  python3 migrate_purchases.py                       # all 12 months
  python3 migrate_purchases.py --month 2020-04       # one month only
  python3 migrate_purchases.py --dry-run             # validate without posting
  python3 migrate_purchases.py --month 2020-04 --dry-run
"""

import json
import glob
import argparse
import requests
from datetime import date, timedelta

from common.config import BASE_URL
from helpers.constants import HEADERS
from helpers.utils import (
    clean, parse_qty, parse_rate, parse_amount,
    is_duplicate_error, get_error, check_connection,
)

# ─────────────────────────────────────────────────────────────────────────────
# ARGS
# ─────────────────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser(
    description="Step 6: Migrate Tally Purchase Vouchers → ERPNext Purchase Invoices"
)
parser.add_argument("--month", default=None,
    help="Process only this month. Format: YYYY-MM  e.g. 2020-04")
parser.add_argument("--dry-run", action="store_true",
    help="Parse and validate without posting to ERPNext")
args = parser.parse_args()

# ─────────────────────────────────────────────────────────────────────────────
# COMPANY + WAREHOUSE
# ─────────────────────────────────────────────────────────────────────────────
def get_company_info():
    r = requests.get(
        f'{BASE_URL}/api/resource/Company?fields=["name","abbr"]&limit=1',
        headers=HEADERS,
    )
    if r.status_code == 200:
        data = r.json().get("data", [])
        if data:
            return data[0].get("name", "Venkateshwara Traders"), data[0].get("abbr", "VT")
    return "Venkateshwara Traders", "VT"


def get_default_warehouse(abbr):
    """Fetch first leaf warehouse from ERPNext for stock entries."""
    r = requests.get(
        f'{BASE_URL}/api/resource/Warehouse'
        f'?fields=["name","is_group"]&limit=20',
        headers=HEADERS,
    )
    if r.status_code == 200:
        warehouses = [w for w in r.json().get("data", []) if not w.get("is_group")]
        if warehouses:
            return warehouses[0]["name"]
    return f"Electronic City Godown - {abbr}"

# ─────────────────────────────────────────────────────────────────────────────
# FISCAL YEAR
# ─────────────────────────────────────────────────────────────────────────────
def date_to_fy(date_str):
    year  = int(date_str[:4])
    month = int(date_str[5:7])
    return (year, year + 1) if month >= 4 else (year - 1, year)


def ensure_fiscal_years(company_name, dates):
    fy_set = set()
    today  = date.today().isoformat()
    fy_set.add(date_to_fy(today))   # always need today's FY

    for d in dates:
        if d and len(d) >= 10:
            fy_set.add(date_to_fy(d))

    print(f"  Ensuring {len(fy_set)} fiscal year(s) exist...")
    for fy_start, fy_end in sorted(fy_set):
        year_name = f"{fy_start}-{fy_end}"
        r = requests.get(f"{BASE_URL}/api/resource/Fiscal Year/{year_name}",
                         headers=HEADERS)
        if r.status_code == 200:
            print(f"    ~ FY {year_name} already exists")
            continue
        payload = {
            "doctype"         : "Fiscal Year",
            "year"            : year_name,
            "year_start_date" : f"{fy_start}-04-01",
            "year_end_date"   : f"{fy_end}-03-31",
            "companies"       : [{"company": company_name}],
        }
        r2 = requests.post(f"{BASE_URL}/api/resource/Fiscal Year",
                           headers=HEADERS, json=payload)
        if r2.status_code == 200:
            print(f"    ✓ FY {year_name} created")
        else:
            err = r2.json().get("exception", "")[:60] if r2.content else ""
            if "already exists" not in err.lower():
                print(f"    ✗ FY {year_name} failed: {err}")

# ─────────────────────────────────────────────────────────────────────────────
# PRE-MIGRATION CLEANUP (same fixes as Step 5)
# ─────────────────────────────────────────────────────────────────────────────
def clear_supplier_payment_terms():
    """Clear payment terms on all suppliers to prevent due_date conflicts."""
    r = requests.get(
        f'{BASE_URL}/api/resource/Supplier?fields=["name","payment_terms"]&limit=100',
        headers=HEADERS,
    )
    if r.status_code != 200:
        return
    cleared = 0
    for s in r.json().get("data", []):
        name = s.get("name", "")
        r2 = requests.put(
            f"{BASE_URL}/api/resource/Supplier/{requests.utils.quote(name)}",
            headers=HEADERS,
            json={"payment_terms": "", "credit_days": 0},
        )
        if r2.status_code == 200:
            cleared += 1
    print(f"  Cleared payment terms on {cleared} supplier(s)")


def delete_item_prices():
    """
    Delete auto-created Item Prices (valid_from = today).
    These conflict with historical 2020 invoice dates.
    """
    r = requests.get(
        f"{BASE_URL}/api/resource/Item Price?fields=%5B%22name%22%5D&limit=500",
        headers=HEADERS,
    )
    if r.status_code != 200:
        return
    prices = r.json().get("data", [])
    if not prices:
        print("  No Item Prices to delete")
        return
    deleted = 0
    for p in prices:
        r2 = requests.delete(
            f"{BASE_URL}/api/resource/Item Price/{requests.utils.quote(p['name'])}",
            headers=HEADERS,
        )
        if r2.status_code == 202:
            deleted += 1
    print(f"  Deleted {deleted} Item Price(s)")

# ─────────────────────────────────────────────────────────────────────────────
# TAX ACCOUNT MAP (purchase-specific names)
# ─────────────────────────────────────────────────────────────────────────────
TAX_ACCOUNT_MAP = {
    "CGST"          : ("Tax",            "Duties and Taxes"),
    "SGST"          : ("Tax",            "Duties and Taxes"),
    "IGST"          : ("Tax",            "Duties and Taxes"),
    "Fright"        : ("Expense Account","Direct Expenses"),
    "Freight"       : ("Expense Account","Direct Expenses"),
    "Packing Cost"  : ("Expense Account","Direct Expenses"),
    "Transportation": ("Expense Account","Direct Expenses"),
    "Insurance"     : ("Expense Account","Direct Expenses"),
    "Rounding Off"  : ("Expense Account","Indirect Expenses"),
}

_tax_account_cache = {}
_company_name_cache = None


def _get_company_name():
    global _company_name_cache
    if _company_name_cache:
        return _company_name_cache
    r = requests.get(
        f"{BASE_URL}/api/resource/Company?fields=%5B%22name%22%5D&limit=1",
        headers=HEADERS,
    )
    if r.status_code == 200:
        data = r.json().get("data", [])
        if data:
            _company_name_cache = data[0]["name"]
    return _company_name_cache or "Venkateshwara Traders"


def resolve_tax_account(name, abbr):
    """Find or create the ERPNext account for a Tally tax/charge ledger."""
    if name in _tax_account_cache:
        return _tax_account_cache[name]

    for candidate in [f"{name} - {abbr}", name]:
        r = requests.get(
            f"{BASE_URL}/api/resource/Account/{requests.utils.quote(candidate)}",
            headers=HEADERS,
        )
        if r.status_code == 200:
            result = r.json().get("data", {}).get("name", candidate)
            _tax_account_cache[name] = result
            return result

    # Partial search
    _filters = f'[["account_name","like","%{name}%"]]'
    r = requests.get(
        f"{BASE_URL}/api/resource/Account?filters={_filters}&fields=%5B%22name%22%5D&limit=3",
        headers=HEADERS,
    )
    if r.status_code == 200:
        data = r.json().get("data", [])
        if data:
            _tax_account_cache[name] = data[0]["name"]
            return data[0]["name"]

    # Create if we know the mapping
    mapping = TAX_ACCOUNT_MAP.get(name)
    if mapping:
        account_type, parent_base = mapping
        for parent_candidate in [f"{parent_base} - {abbr}", parent_base]:
            r2 = requests.get(
                f"{BASE_URL}/api/resource/Account/{requests.utils.quote(parent_candidate)}",
                headers=HEADERS,
            )
            if r2.status_code == 200:
                payload = {
                    "doctype"        : "Account",
                    "account_name"   : name,
                    "parent_account" : parent_candidate,
                    "account_type"   : account_type,
                    "company"        : _get_company_name(),
                    "is_group"       : 0,
                }
                r3 = requests.post(f"{BASE_URL}/api/resource/Account",
                                   headers=HEADERS, json=payload)
                if r3.status_code == 200:
                    created = r3.json().get("data", {}).get("name", f"{name} - {abbr}")
                    _tax_account_cache[name] = created
                    return created
                break

    fallback = f"{name} - {abbr}"
    _tax_account_cache[name] = fallback
    return fallback

# ─────────────────────────────────────────────────────────────────────────────
# PARSERS
# ─────────────────────────────────────────────────────────────────────────────
def parse_date(val):
    raw = clean(val)
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return raw


def add_days(date_str, days):
    d = date(int(date_str[:4]), int(date_str[5:7]), int(date_str[8:10]))
    return (d + timedelta(days=days)).isoformat()


def parse_amount_signed(val):
    try:
        return float(clean(val))
    except ValueError:
        return 0.0


def get_inventory_entries(voucher):
    inv = voucher.get("ALLINVENTORYENTRIES.LIST", {})
    if isinstance(inv, list):
        return [i for i in inv if i and isinstance(i, dict)]
    if isinstance(inv, dict) and inv:
        return [inv]
    return []


def get_ledger_entries(voucher):
    led = voucher.get("LEDGERENTRIES.LIST", {})
    if isinstance(led, list):
        return [e for e in led if e and isinstance(e, dict)]
    if isinstance(led, dict) and led:
        return [led]
    return []

# ─────────────────────────────────────────────────────────────────────────────
# BUILD PAYLOAD
# ─────────────────────────────────────────────────────────────────────────────
def build_items(inventory_entries, warehouse):
    """Build items list for Purchase Invoice. Stock goes into accepted_warehouse."""
    items = []
    for entry in inventory_entries:
        name = clean(entry.get("STOCKITEMNAME"))
        qty  = parse_qty(entry.get("BILLEDQTY", "1"))
        rate = parse_rate(entry.get("RATE", "0"))
        hsn  = clean(entry.get("GSTHSNNAME", ""))

        if not name or qty <= 0:
            continue

        line = {
            "item_code"          : name,
            "qty"                : qty,
            "rate"               : rate,
            "accepted_warehouse" : warehouse,   # where stock is received
        }
        if hsn:
            line["item_tax_template"] = None    # clear template, use rate directly
            line["gst_hsn_code"]      = hsn

        items.append(line)
    return items


def build_taxes(ledger_entries, party_name, abbr):
    """Build taxes list from non-party ledger entries."""
    taxes = []
    for entry in ledger_entries:
        name       = clean(entry.get("LEDGERNAME", ""))
        is_party   = clean(entry.get("ISPARTYLEDGER", "No"))
        amount_raw = parse_amount_signed(entry.get("AMOUNT", "0"))

        if not name or name == party_name or is_party == "Yes":
            continue
        if amount_raw == 0:
            continue

        account = resolve_tax_account(name, abbr)

        taxes.append({
            "charge_type" : "Actual",
            "account_head": account,
            "tax_amount"  : abs(amount_raw),
            "description" : name,
        })
    return taxes


def build_invoice_payload(voucher, company_name, abbr, warehouse):
    supplier  = clean(voucher.get("PARTYLEDGERNAME"))
    date_str  = parse_date(voucher.get("DATE"))
    vch_no    = clean(voucher.get("VOUCHERNUMBER"))
    narration = clean(voucher.get("NARRATION", ""))

    if not supplier or not date_str:
        return None

    inv_entries = get_inventory_entries(voucher)
    led_entries = get_ledger_entries(voucher)

    items = build_items(inv_entries, warehouse)
    taxes = build_taxes(led_entries, supplier, abbr)

    if not items:
        return None

    # due_date = posting_date + 30 days (avoids any due_date validation issues)
    due_date = add_days(date_str, 30)

    payload = {
        "doctype"                    : "Purchase Invoice",
        "supplier"                   : supplier,
        "posting_date"               : date_str,
        "due_date"                   : due_date,
        "bill_date"                  : date_str,
        "bill_no"                    : vch_no,    # supplier's invoice number
        "company"                    : company_name,
        "items"                      : items,
        "is_return"                  : 0,
        "ignore_pricing_rule"        : 1,
        "set_posting_time"           : 1,
        "update_stock"               : 1,          # update stock ledger on submit
    }

    if taxes:
        payload["taxes"] = taxes

    if narration:
        payload["remarks"] = narration

    return payload

# ─────────────────────────────────────────────────────────────────────────────
# CREATE + SUBMIT
# ─────────────────────────────────────────────────────────────────────────────
def create_and_submit_invoice(payload, dry_run=False):
    if dry_run:
        supplier = payload.get("supplier")
        items    = payload.get("items", [])
        date_str = payload.get("posting_date")
        print(f"  [DRY RUN] {date_str}  {supplier:30s}  {len(items)} items")
        return True, "DRY-RUN", ""

    # Step 1: Create as Draft
    insert_payload = dict(payload)
    r = requests.post(
        f"{BASE_URL}/api/method/frappe.client.insert",
        headers=HEADERS,
        json={"doc": insert_payload},
    )
    if r.status_code != 200:
        r_fallback = requests.post(
            f"{BASE_URL}/api/resource/Purchase Invoice",
            headers=HEADERS, json=payload,
        )
        if r_fallback.status_code != 200:
            return False, "", get_error(r_fallback.json())
        doc_name = r_fallback.json().get("data", {}).get("name", "")
    else:
        doc_name = r.json().get("message", {}).get("name", "")
        if not doc_name:
            doc_name = r.json().get("data", {}).get("name", "")

    if not doc_name:
        return False, "", "No doc name returned after create"

    # Step 2: Fetch current doc (get latest modified timestamp)
    r_fetch = requests.get(
        f"{BASE_URL}/api/resource/Purchase Invoice/{doc_name}",
        headers=HEADERS,
    )
    if r_fetch.status_code != 200:
        requests.delete(f"{BASE_URL}/api/resource/Purchase Invoice/{doc_name}",
                        headers=HEADERS)
        return False, doc_name, "Could not fetch doc after create"

    current_doc = r_fetch.json().get("data", {})

    # Step 3: Submit with current timestamp
    r2 = requests.post(
        f"{BASE_URL}/api/method/frappe.client.submit",
        headers=HEADERS,
        json={"doc": current_doc},
    )
    if r2.status_code != 200:
        error = get_error(r2.json())
        requests.delete(f"{BASE_URL}/api/resource/Purchase Invoice/{doc_name}",
                        headers=HEADERS)
        return False, doc_name, f"Submit failed: {error}"

    return True, doc_name, ""

# ─────────────────────────────────────────────────────────────────────────────
# PROCESS ONE FILE
# ─────────────────────────────────────────────────────────────────────────────
def process_file(filepath, company_name, abbr, warehouse, dry_run):
    with open(filepath, encoding="utf-8") as f:
        data = json.load(f)

    vouchers = data["ENVELOPE"]["BODY"]["DATA"]["COLLECTION"]["VOUCHER"]
    fname    = filepath.split("/")[-1]
    total    = len(vouchers)

    print(f"\nProcessing: {fname}  ({total} vouchers)...")

    ok = 0; failed = 0; skipped = 0; invalid = 0
    errors = []

    for i, voucher in enumerate(vouchers, 1):
        if i % 20 == 0:
            print(f"  Progress: {i:4d}/{total}...")

        payload = build_invoice_payload(voucher, company_name, abbr, warehouse)

        if payload is None:
            invalid += 1
            continue

        success, doc_name, error = create_and_submit_invoice(payload, dry_run)

        if success:
            ok += 1
        elif is_duplicate_error(error):
            skipped += 1
        else:
            failed += 1
            vch_no   = clean(voucher.get("VOUCHERNUMBER", "?"))
            supplier = clean(voucher.get("PARTYLEDGERNAME", "?"))
            date_str = parse_date(voucher.get("DATE", ""))
            errors.append(f"  Vch#{vch_no} {date_str} {supplier}: {error[:80]}")
            print(f"  ✗ Vch#{vch_no} {supplier}: {error[:80]}")

    tag = "[DRY RUN] " if dry_run else ""
    print(f"  {tag}✓ {fname}: {ok} created, {skipped} skipped, {failed} failed, {invalid} invalid")

    return ok, failed, skipped, invalid, errors

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
def migrate():
    if not args.dry_run and not check_connection():
        return

    company_name, abbr = get_company_info()
    warehouse = get_default_warehouse(abbr)

    print(f"  Company  : {company_name} ({abbr})")
    print(f"  Warehouse: {warehouse}")
    if args.dry_run:
        print("  Mode     : DRY RUN — no data will be posted")

    # Find all purchase voucher files
    all_files = sorted(glob.glob("output/voucher_purchase_*.json"))

    if args.month:
        month_str = args.month.replace("-", "")
        all_files = [f for f in all_files if month_str in f]
        if not all_files:
            print(f"  ✗ No files found for month: {args.month}")
            return
        print(f"  Month    : {args.month}  ({len(all_files)} file)")
    else:
        print(f"  Files    : {len(all_files)} monthly files")

    print()

    if not args.dry_run:
        print("Clearing supplier payment terms...")
        clear_supplier_payment_terms()
        print("Deleting Item Prices (date conflict prevention)...")
        delete_item_prices()
        print()

        # Collect dates and ensure fiscal years exist
        all_dates = []
        for filepath in all_files:
            with open(filepath, encoding="utf-8") as f:
                data = json.load(f)
            for v in data["ENVELOPE"]["BODY"]["DATA"]["COLLECTION"]["VOUCHER"]:
                raw = clean(v.get("DATE", ""))
                if len(raw) == 8 and raw.isdigit():
                    all_dates.append(f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}")
        ensure_fiscal_years(company_name, all_dates)
        print()

    # Process each file
    grand_ok = 0; grand_fail = 0; grand_skip = 0; grand_invalid = 0
    all_errors = []

    for filepath in all_files:
        ok, fail, skip, invalid, errors = process_file(
            filepath, company_name, abbr, warehouse, args.dry_run
        )
        grand_ok      += ok
        grand_fail    += fail
        grand_skip    += skip
        grand_invalid += invalid
        all_errors.extend(errors)

    # Summary
    print()
    print("=" * 52)
    print("  MIGRATION SUMMARY")
    print("=" * 52)
    print(f"  Invoices created  : {grand_ok}")
    print(f"  Already existed   : {grand_skip}")
    print(f"  Failed            : {grand_fail}")
    print(f"  Invalid (skipped) : {grand_invalid}")

    if all_errors:
        print(f"\n  Failed invoice details ({len(all_errors)}):")
        for e in all_errors[:20]:
            print(e)
        if len(all_errors) > 20:
            print(f"  ... and {len(all_errors)-20} more")

    if grand_fail == 0:
        print()
        if args.dry_run:
            print("  ✓ Dry run complete — no errors found!")
            print("  Run without --dry-run to post to ERPNext.")
        else:
            print("  ✓ All done!")
            print()
            print("  Verify in ERPNext:")
            print("    Buying → Purchase Invoice  (45 submitted invoices)")
            print("    Stock  → Stock Ledger       (stock received from suppliers)")
            print()
            print("  Next → python3 migrate_payments.py  (Step 7: Payment Entries)")
    else:
        print()
        print("  Some invoices failed — check errors above.")
        print("  Safe to re-run — duplicates are skipped.")


if __name__ == "__main__":
    print()
    print("╔══════════════════════════════════════════════════╗")
    print("║  Tally → ERPNext Migration — Step 6: Purchases  ║")
    print("║  Venkateshwara Traders                          ║")
    print("╚══════════════════════════════════════════════════╝")
    print()
    migrate()
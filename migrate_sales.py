"""
Tally → ERPNext Migration Script
Step 5: Migrate Sales Invoices

Company  : Venkateshwara Traders
Source   : output/voucher_sales_YYYYMMDD_YYYYMMDD.json  (12 files, 1,884 invoices)
Target   : ERPNext Sales Invoice (submitted)

What this script does:
  1. Reads all 12 monthly sales voucher JSON files
  2. For each voucher: maps date, customer, items, GST/charges
  3. Creates and submits Sales Invoice in ERPNext
  4. Tracks progress, handles duplicates, reports summary

Usage:
  python3 migrate_sales.py                      # all 12 months
  python3 migrate_sales.py --month 2024-04      # one month only (for testing)
  python3 migrate_sales.py --dry-run            # validate without posting
  python3 migrate_sales.py --month 2024-04 --dry-run
"""

import json
import glob
import argparse
import requests

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
    description="Step 5: Migrate Tally Sales Vouchers → ERPNext Sales Invoices"
)
parser.add_argument(
    "--month", default=None,
    help="Process only this month. Format: YYYY-MM  e.g. 2024-04"
)
parser.add_argument(
    "--dry-run", action="store_true",
    help="Parse and validate without posting to ERPNext"
)
args = parser.parse_args()

# ─────────────────────────────────────────────────────────────────────────────
# COMPANY INFO
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


def delete_item_prices():
    """
    Delete all Item Prices created by ERPNext when items were added.
    These prices have today as valid_from, which conflicts with historical
    invoice dates (2020) causing 'Due Date cannot be before Posting Date'
    because ERPNext tries to apply the price and its validity check fails.
    After migration, prices will be set correctly from actual invoice data.
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
        name = p.get("name", "")
        r2 = requests.delete(
            f"{BASE_URL}/api/resource/Item Price/{requests.utils.quote(name)}",
            headers=HEADERS,
        )
        if r2.status_code == 202:
            deleted += 1

    print(f"  Deleted {deleted} Item Price(s) (had today's date — conflict with 2020 invoices)")


def clear_payment_terms_everywhere():
    """
    Clear payment terms at Customer level AND Company level.
    ERPNext checks both when computing due_date on a Sales Invoice.
    """
    cleared = 0

    # 1. Clear on all customers
    r = requests.get(
        f'{BASE_URL}/api/resource/Customer?fields=["name","payment_terms"]&limit=100',
        headers=HEADERS,
    )
    if r.status_code == 200:
        for c in r.json().get("data", []):
            name = c.get("name", "")
            # Always patch — even if empty, ensure it's truly blank
            r2 = requests.put(
                f"{BASE_URL}/api/resource/Customer/{requests.utils.quote(name)}",
                headers=HEADERS,
                json={"payment_terms": "", "credit_days": 0,
                      "credit_days_based_on": ""},
            )
            if r2.status_code == 200:
                cleared += 1

    # 2. Clear on the Company default
    r3 = requests.get(
        f'{BASE_URL}/api/resource/Company?fields=["name","payment_terms"]&limit=5',
        headers=HEADERS,
    )
    if r3.status_code == 200:
        for co in r3.json().get("data", []):
            if co.get("payment_terms"):
                requests.put(
                    f"{BASE_URL}/api/resource/Company/{requests.utils.quote(co['name'])}",
                    headers=HEADERS,
                    json={"payment_terms": ""},
                )
                print(f"  Cleared payment terms on company: {co['name']}")

    print(f"  Cleared payment terms on {cleared} customer(s)")


def date_to_fy(date_str):
    """Convert YYYY-MM-DD to Indian FY tuple. e.g. 2024-05-15 → (2024, 2025)"""
    year  = int(date_str[:4])
    month = int(date_str[5:7])
    return (year, year + 1) if month >= 4 else (year - 1, year)


def ensure_fiscal_years(company_name, dates):
    """
    Create any fiscal years needed for the given dates.
    ERPNext requires a matching FY to exist before any dated document
    can be submitted — regardless of whether the date is past or future.
    Voucher dates in your data: 2020-2021 and 2024-2025.
    """
    fy_set = set()
    for d in dates:
        if d and len(d) >= 10:
            fy_set.add(date_to_fy(d))

    if not fy_set:
        return

    print(f"  Ensuring {len(fy_set)} fiscal year(s) exist...")
    for fy_start, fy_end in sorted(fy_set):
        year_name = f"{fy_start}-{fy_end}"
        r = requests.get(
            f"{BASE_URL}/api/resource/Fiscal Year/{year_name}",
            headers=HEADERS,
        )
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
            err = r2.json().get("exception","")[:60] if r2.content else ""
            if "already exists" not in err.lower():
                print(f"    ✗ FY {year_name} failed: {err}")

# ─────────────────────────────────────────────────────────────────────────────
# DATA PARSERS
# ─────────────────────────────────────────────────────────────────────────────
def parse_date(val):
    """
    Tally DATE is a dict: {'_': '20200401', 'TYPE': 'Date'}
    Convert YYYYMMDD → YYYY-MM-DD for ERPNext.
    Uses the real date as-is — no year shifting.
    """
    raw = clean(val)   # extracts '_' from dict automatically
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return raw


def add_days(date_str, days):
    """Add days to a YYYY-MM-DD date string. Returns new YYYY-MM-DD string."""
    from datetime import date, timedelta
    d = date(int(date_str[:4]), int(date_str[5:7]), int(date_str[8:10]))
    return (d + timedelta(days=days)).isoformat()


def parse_amount_signed(val):
    """
    Return float preserving sign.
    Tally uses negative for receivables (sales).
    """
    try:
        return float(clean(val))
    except ValueError:
        return 0.0


def get_inventory_entries(voucher):
    """
    ALLINVENTORYENTRIES.LIST can be:
      - a dict  (single item)
      - a list  (multiple items)
      - empty string / None
    Always returns a list.
    """
    inv = voucher.get("ALLINVENTORYENTRIES.LIST", {})
    if isinstance(inv, list):
        return [i for i in inv if i and isinstance(i, dict)]
    if isinstance(inv, dict) and inv:
        return [inv]
    return []


def get_ledger_entries(voucher):
    """
    LEDGERENTRIES.LIST can be a dict or list.
    Always returns a list.
    """
    led = voucher.get("LEDGERENTRIES.LIST", {})
    if isinstance(led, list):
        return [e for e in led if e and isinstance(e, dict)]
    if isinstance(led, dict) and led:
        return [led]
    return []

# ─────────────────────────────────────────────────────────────────────────────
# BUILD ERPNext PAYLOAD
# ─────────────────────────────────────────────────────────────────────────────
def build_items(inventory_entries):
    """Build the 'items' list for the ERPNext Sales Invoice."""
    items = []
    for entry in inventory_entries:
        name = clean(entry.get("STOCKITEMNAME"))
        qty  = parse_qty(entry.get("BILLEDQTY", "1"))
        rate = parse_rate(entry.get("RATE", "0"))
        hsn  = clean(entry.get("GSTHSNNAME", ""))

        if not name or qty <= 0:
            continue

        line = {
            "item_code"    : name,
            "qty"          : qty,
            "rate"         : rate,
        }
        if hsn:
            line["gst_hsn_code"] = hsn

        items.append(line)
    return items


# Cache of resolved tax account names to avoid repeated API calls
_tax_account_cache = {}


# Map of known Tally tax/charge ledgers → ERPNext account type + parent
TAX_ACCOUNT_MAP = {
    "CGST"                   : ("Tax",           "Duties and Taxes"),
    "SGST"                   : ("Tax",           "Duties and Taxes"),
    "SGST/UTGST"             : ("Tax",           "Duties and Taxes"),
    "IGST"                   : ("Tax",           "Duties and Taxes"),
    "Packing Charges"        : ("Income Account","Direct Income"),
    "Transportation Charges" : ("Income Account","Direct Income"),
    "Insurance"              : ("Income Account","Direct Income"),
    "Freight Charges"        : ("Income Account","Direct Income"),
    "Discount"               : ("Expense Account","Direct Expenses"),
    "Rounding Off"           : ("Expense Account","Indirect Expenses"),
}


def resolve_tax_account(name, abbr):
    """
    Find or create the ERPNext account for a Tally tax/charge ledger.
    1. Try exact match with/without company suffix
    2. Try partial LIKE search
    3. Create the account if still not found (using TAX_ACCOUNT_MAP)
    """
    if name in _tax_account_cache:
        return _tax_account_cache[name]

    # Try to find existing account
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

    # Not found — create it using TAX_ACCOUNT_MAP
    mapping = TAX_ACCOUNT_MAP.get(name)
    if mapping:
        account_type, parent_base = mapping
        # Find parent account
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

    # Final fallback
    fallback = f"{name} - {abbr}"
    _tax_account_cache[name] = fallback
    return fallback


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
            return _company_name_cache
    return "Venkateshwara Traders"


def build_taxes(ledger_entries, party_name, abbr):
    """
    Build the 'taxes' list from non-party ledger entries.
    IGST, CGST, SGST, Packing Charges, Insurance, Transportation → taxes table.
    Account names are resolved via ERPNext API (cached after first lookup).
    """
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


def build_invoice_payload(voucher, company_name, abbr):
    """
    Build the complete ERPNext Sales Invoice payload from a Tally voucher.
    Returns the payload dict or None if the voucher is invalid.
    """
    customer   = clean(voucher.get("PARTYLEDGERNAME"))
    date       = parse_date(voucher.get("DATE"))
    vch_no     = clean(voucher.get("VOUCHERNUMBER"))
    narration  = clean(voucher.get("NARRATION", ""))

    if not customer or not date:
        return None

    inv_entries = get_inventory_entries(voucher)
    led_entries = get_ledger_entries(voucher)

    items = build_items(inv_entries)
    taxes = build_taxes(led_entries, customer, abbr)

    if not items:
        return None   # no line items — skip this voucher

    # Set due_date 30 days after posting to guarantee it's always >= posting_date
    due_date = add_days(date, 30)

    payload = {
        "doctype"                    : "Sales Invoice",
        "customer"                   : customer,
        "posting_date"               : date,
        "due_date"                   : due_date,
        "company"                    : company_name,
        "items"                      : items,
        "is_return"                  : 0,
        "ignore_pricing_rule"        : 1,
        "selling_price_list"         : "Standard Selling",
        "price_list_currency"        : "INR",
        "ignore_default_payment_terms_template": 1,
        "set_posting_time"           : 1,
    }

    if taxes:
        payload["taxes"] = taxes

    if narration:
        payload["remarks"] = narration

    if vch_no:
        payload["po_no"] = vch_no    # store Tally voucher number as PO reference

    return payload


# ─────────────────────────────────────────────────────────────────────────────
# CREATE + SUBMIT
# ─────────────────────────────────────────────────────────────────────────────
def create_and_submit_invoice(payload, dry_run=False):
    """
    Create a Sales Invoice (Draft) then submit it.
    Returns (success: bool, doc_name: str, error: str)
    """
    if dry_run:
        customer = payload.get("customer")
        items    = payload.get("items", [])
        date     = payload.get("posting_date")
        print(f"  [DRY RUN] {date}  {customer:30s}  {len(items)} items")
        return True, "DRY-RUN", ""

    # Step 1: Create as Draft using frappe.client.insert
    # This bypasses payment terms recomputation that happens in before_save hooks
    insert_payload = dict(payload)   # copy
    r = requests.post(
        f"{BASE_URL}/api/method/frappe.client.insert",
        headers=HEADERS,
        json={"doc": insert_payload},
    )
    if r.status_code != 200:
        # Log full raw response for first failure to help diagnose
        if not hasattr(create_and_submit_invoice, '_logged'):
            create_and_submit_invoice._logged = True
            print(f"\n  === FULL ERROR RESPONSE (first failure) ===")
            try:
                resp = r.json()
                print(f"  status_code : {r.status_code}")
                print(f"  exc_type    : {resp.get('exc_type','')}")
                print(f"  exception   : {resp.get('exception','')[:300]}")
                print(f"  message     : {resp.get('message','')[:200]}")
                # Show server messages
                import json as _json
                smsgs = resp.get('_server_messages','')
                if smsgs:
                    try:
                        msgs = _json.loads(smsgs)
                        for m in msgs[:3]:
                            try: m = _json.loads(m)
                            except: pass
                            print(f"  server_msg  : {str(m)[:200]}")
                    except: print(f"  server_msgs : {smsgs[:300]}")
            except Exception as e:
                print(f"  raw text: {r.text[:400]}")
            print(f"  === PAYLOAD SENT ===")
            print(f"  posting_date: {payload.get('posting_date')}")
            print(f"  due_date    : {payload.get('due_date')}")
            print(f"  customer    : {payload.get('customer')}")
            print()
        return False, "", get_error(r.json())
    else:
        doc_name = r.json().get("message", {}).get("name", "")
        if not doc_name:
            doc_name = r.json().get("data", {}).get("name", "")

    # Step 2: Fetch the created doc to get current modified timestamp
    # ERPNext uses optimistic locking — submit must include the exact
    # modified timestamp of the current doc, otherwise TimestampMismatchError
    r_fetch = requests.get(
        f"{BASE_URL}/api/resource/Sales Invoice/{doc_name}",
        headers=HEADERS,
    )
    if r_fetch.status_code != 200:
        requests.delete(
            f"{BASE_URL}/api/resource/Sales Invoice/{doc_name}",
            headers=HEADERS,
        )
        return False, doc_name, "Could not fetch doc after create"

    current_doc = r_fetch.json().get("data", {})

    # Step 3: Submit with current timestamp to pass optimistic lock check
    r2 = requests.post(
        f"{BASE_URL}/api/method/frappe.client.submit",
        headers=HEADERS,
        json={"doc": current_doc},
    )
    if r2.status_code != 200:
        error = get_error(r2.json())
        requests.delete(
            f"{BASE_URL}/api/resource/Sales Invoice/{doc_name}",
            headers=HEADERS,
        )
        return False, doc_name, f"Submit failed: {error}"

    return True, doc_name, ""


# ─────────────────────────────────────────────────────────────────────────────
# PROCESS ONE FILE
# ─────────────────────────────────────────────────────────────────────────────
def process_file(filepath, company_name, abbr, dry_run):
    """Process all vouchers in one monthly JSON file."""
    with open(filepath, encoding="utf-8") as f:
        data = json.load(f)

    vouchers = data["ENVELOPE"]["BODY"]["DATA"]["COLLECTION"]["VOUCHER"]
    fname    = filepath.split("/")[-1]
    total    = len(vouchers)

    print(f"\nProcessing: {fname}  ({total} vouchers)...")

    ok = 0; failed = 0; skipped = 0; invalid = 0
    errors = []

    for i, voucher in enumerate(vouchers, 1):
        # Progress every 50
        if i % 50 == 0:
            print(f"  Progress: {i:4d}/{total}...")

        payload = build_invoice_payload(voucher, company_name, abbr)

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
            customer = clean(voucher.get("PARTYLEDGERNAME", "?"))
            date     = parse_date(voucher.get("DATE", ""))
            errors.append(f"  Vch#{vch_no} {date} {customer}: {error[:80]}")
            print(f"  ✗ Vch#{vch_no} {customer}: {error[:80]}")

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
    print(f"  Company: {company_name} ({abbr})")
    if args.dry_run:
        print("  Mode   : DRY RUN — no data will be posted to ERPNext")

    # Find all sales voucher files
    all_files = sorted(glob.glob("output/voucher_sales_*.json"))

    # Filter by month if --month given
    if args.month:
        month_str = args.month.replace("-", "")   # '2024-04' → '202404'
        all_files = [f for f in all_files if month_str in f]
        if not all_files:
            print(f"  ✗ No files found for month: {args.month}")
            return
        print(f"  Month  : {args.month}  ({len(all_files)} file)")
    else:
        print(f"  Files  : {len(all_files)} monthly files")

    print()

    # Clear customer payment terms to prevent due_date conflicts
    if not args.dry_run:
        print("Clearing payment terms on customers and company...")
        clear_payment_terms_everywhere()
        print("Deleting Item Prices (conflict with historical dates)...")
        delete_item_prices()
        print()

    # Collect all dates across files and ensure fiscal years exist
    if not args.dry_run:
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
            filepath, company_name, abbr, args.dry_run
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
        for e in all_errors[:20]:   # show first 20
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
            print("    Selling → Sales Invoice  (filter by FY 2024-25)")
            print()
            print("  Next → python3 migrate_purchases.py  (Step 6)")
    else:
        print()
        print("  Some invoices failed — check errors above.")
        print("  Safe to re-run — duplicates are skipped.")


if __name__ == "__main__":
    print()
    print("╔══════════════════════════════════════════════════╗")
    print("║  Tally → ERPNext Migration — Step 5: Sales      ║")
    print("║  Venkateshwara Traders                          ║")
    print("╚══════════════════════════════════════════════════╝")
    print()
    migrate()
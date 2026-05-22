"""
Step 4: Opening Balances — Stock + Accounts

Fixes in this version:
  1. Stock Reconciliation: add expense_account (Temporary Opening) to payload
  2. Journal Entry: account_type=Payable → blank for loan/provision accounts
  3. Parent account lookup: try multiple candidate names per group
  4. Warehouse: auto-picks first leaf warehouse from ERPNext

Usage:
  python3 migrate_balances.py                               # auto-picks warehouse
  python3 migrate_balances.py --warehouse "Main Location"   # specify warehouse
  python3 migrate_balances.py --list-warehouses             # show options
"""

import json
import argparse
import requests

from common.config import BASE_URL
from helpers.constants import (
    HEADERS, SKIP_LEDGER_GROUPS, SKIP_PL_GROUPS, GROUP_MAP, STOCK_DIFFERENCE_ACCOUNT_BASE
)
from helpers.utils import (
    clean, parse_rate, parse_qty, parse_amount,
    is_duplicate_error, get_error, check_connection,
)

# FY 2024-25 starts 2024-04-01.  Using 2024-03-31 causes FiscalYearError.
# Opening date = first day of the fiscal year your Tally data is from.
# Your data is FY 2020-21 so opening date is 2020-04-01.
# Change this if your real data is from a different year.
OPENING_DATE = "2020-04-01"

# ─────────────────────────────────────────────────────────────────────────────
# ARGS
# ─────────────────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser(
    description="Step 4: Migrate opening balances (stock + accounts) to ERPNext"
)
parser.add_argument(
    "--warehouse", default=None,
    help="Warehouse base name (without suffix). Auto-picked if omitted.",
)
parser.add_argument(
    "--list-warehouses", action="store_true",
    help="List all available warehouses and exit",
)
args = parser.parse_args()

# ─────────────────────────────────────────────────────────────────────────────
# COMPANY
# ─────────────────────────────────────────────────────────────────────────────
def get_company_info():
    r = requests.get(
        f'{BASE_URL}/api/resource/Company?fields=["name","abbr"]&limit=5',
        headers=HEADERS,
    )
    if r.status_code == 200:
        data = r.json().get("data", [])
        if data:
            return data[0].get("name", "Venkateshwara Traders"), data[0].get("abbr", "VT")
    return "Venkateshwara Traders", "VT"


def date_to_fy(date_str):
    """
    Convert a YYYY-MM-DD date to Indian fiscal year tuple (start_year, end_year).
    Indian FY runs April 1 to March 31.
    e.g. 2024-05-15 → (2024, 2025),  2025-01-10 → (2024, 2025)
    """
    year  = int(date_str[:4])
    month = int(date_str[5:7])
    if month >= 4:
        return (year, year + 1)
    else:
        return (year - 1, year)


def ensure_fiscal_year(company_name, extra_dates=None):
    """
    Ensure all required fiscal years exist in ERPNext.

    WHY THIS IS NEEDED:
    ERPNext checks fiscal years in two ways:
      1. The document's posting_date must belong to an active FY
      2. Stock Reconciliation also uses the SERVER'S CURRENT DATE
         (today) for internal stock ledger entries — so a FY covering
         today must also exist, regardless of what posting_date you pass.

    This function creates:
      - FY covering the OPENING_DATE (for journal entries)
      - FY covering TODAY (for stock reconciliation internal entries)
      - Any additional FYs in extra_dates
    """
    from datetime import date as _date
    today = _date.today().isoformat()   # e.g. '2026-05-18'

    # Always need: FY for opening date + FY for today (stock recon uses sysdate)
    fy_set = {date_to_fy(OPENING_DATE), date_to_fy(today)}

    if extra_dates:
        for d in extra_dates:
            if d and len(d) >= 10:
                fy_set.add(date_to_fy(d))

    created = 0
    existed = 0

    for fy_start, fy_end in sorted(fy_set):
        year_name  = f"{fy_start}-{fy_end}"
        start_date = f"{fy_start}-04-01"
        end_date   = f"{fy_end}-03-31"

        # Check if already exists
        r = requests.get(
            f"{BASE_URL}/api/resource/Fiscal Year/{year_name}",
            headers=HEADERS,
        )
        if r.status_code == 200:
            print(f"  ~ FY {year_name} already exists")
            existed += 1
            continue

        # Create it
        payload = {
            "doctype"         : "Fiscal Year",
            "year"            : year_name,
            "year_start_date" : start_date,
            "year_end_date"   : end_date,
            "companies"       : [{"company": company_name}],
        }
        r = requests.post(
            f"{BASE_URL}/api/resource/Fiscal Year",
            headers=HEADERS, json=payload,
        )
        if r.status_code == 200:
            print(f"  ✓ FY {year_name} created  ({start_date} → {end_date})")
            created += 1
        else:
            error = ""
            if r.content:
                error = r.json().get("exception", r.json().get("message", ""))[:80]
            if "already exists" in error.lower():
                print(f"  ~ FY {year_name} already exists")
                existed += 1
            else:
                print(f"  ✗ Could not create FY {year_name}: {error}")

    print(f"  Fiscal years ready: {existed} existed, {created} created")
    return True

# ─────────────────────────────────────────────────────────────────────────────
# WAREHOUSE
# ─────────────────────────────────────────────────────────────────────────────
def fetch_warehouses():
    """Return all leaf warehouses (is_group=0) from ERPNext."""
    r = requests.get(
        f'{BASE_URL}/api/resource/Warehouse'
        f'?fields=["name","warehouse_name","is_group"]&limit=50',
        headers=HEADERS,
    )
    if r.status_code == 200:
        return [w for w in r.json().get("data", []) if not w.get("is_group")]
    return []


def list_warehouses():
    warehouses = fetch_warehouses()
    if warehouses:
        print("\nAvailable warehouses in ERPNext:")
        for w in warehouses:
            base = w.get("warehouse_name", w["name"])
            print(f"  {w['name']:50s}  → --warehouse \"{base}\"")
        print("\nIf --warehouse is omitted, the first one is used automatically.")
    else:
        print("No warehouses found — run Step 3 first.")


def resolve_warehouse(abbr):
    """--warehouse flag → auto-pick first leaf warehouse."""
    if args.warehouse:
        full = f"{args.warehouse} - {abbr}"
        print(f"  Warehouse : {full}  (from --warehouse flag)")
        return full
    warehouses = fetch_warehouses()
    if not warehouses:
        print("  ✗ No warehouses found — run Step 3 first.")
        exit(1)
    full = warehouses[0]["name"]
    print(f"  Warehouse : {full}  (auto-picked — use --warehouse to override)")
    return full

# ─────────────────────────────────────────────────────────────────────────────
# ACCOUNT HELPERS
# ─────────────────────────────────────────────────────────────────────────────
_account_cache = {}


def find_account(name, abbr):
    """
    Search ERPNext for an account by Tally ledger name.
    Tries: exact with suffix → exact without → partial LIKE search.
    """
    if name in _account_cache:
        return _account_cache[name]

    for url in [
        f"{BASE_URL}/api/resource/Account/{requests.utils.quote(name + ' - ' + abbr)}",
        f"{BASE_URL}/api/resource/Account/{requests.utils.quote(name)}",
    ]:
        r = requests.get(url, headers=HEADERS)
        if r.status_code == 200:
            result = r.json().get("data", {}).get("name", f"{name} - {abbr}")
            _account_cache[name] = result
            return result

    # Partial search
    r = requests.get(
        f'{BASE_URL}/api/resource/Account'
        f'?filters=[["account_name","like","%{name}%"]]'
        f'&fields=["name"]&limit=3',
        headers=HEADERS,
    )
    if r.status_code == 200:
        data = r.json().get("data", [])
        if data:
            _account_cache[name] = data[0]["name"]
            return data[0]["name"]

    _account_cache[name] = None
    return None


def find_parent_account(candidates, abbr):
    """
    Try each candidate parent name (with and without company suffix)
    until one exists in ERPNext. Returns the first match or None.
    Falls back to searching by root_type if all candidates fail.
    """
    for base in candidates:
        for full in [f"{base} - {abbr}", base]:
            r = requests.get(
                f"{BASE_URL}/api/resource/Account/{requests.utils.quote(full)}",
                headers=HEADERS,
            )
            if r.status_code == 200:
                return full
    return None


def find_any_parent_by_root(root_type, abbr):
    """
    Fallback: find any group account with the given root_type.
    Used when all named candidates fail (e.g. fresh site with different CoA names).
    Returns the first group account found, or None.
    """
    filters = f'[["root_type","=","{root_type}"],["is_group","=","1"]]'
    r = requests.get(
        f"{BASE_URL}/api/resource/Account?filters={filters}&fields=[%22name%22]&limit=5",
        headers=HEADERS,
    )
    if r.status_code == 200:
        data = r.json().get("data", [])
        # Prefer accounts that aren't the root itself
        for acc in data:
            name = acc.get("name", "")
            if name.lower() not in ("liabilities", "assets", "income",
                                     "expense", "equity", "root"):
                return name
        if data:
            return data[0].get("name")
    return None


def create_account(name, tally_parent, abbr, company_name):
    """
    Create a missing account in ERPNext Chart of Accounts.
    Uses GROUP_MAP for account_type, root_type, and candidate parent names.
    """
    mapping = GROUP_MAP.get(tally_parent)
    if not mapping:
        return None

    account_type, root_type, parent_candidates = mapping

    erp_parent = find_parent_account(parent_candidates, abbr)
    if not erp_parent:
        # All named candidates failed — try any group account with the right root_type
        erp_parent = find_any_parent_by_root(root_type, abbr)
    if not erp_parent:
        return None   # no parent found — cannot create safely

    payload = {
        "doctype"        : "Account",
        "account_name"   : name,
        "parent_account" : erp_parent,
        "account_type"   : account_type,   # "" = no party requirement
        "root_type"      : root_type,
        "company"        : company_name,
        "is_group"       : 0,
    }
    r = requests.post(
        f"{BASE_URL}/api/resource/Account",
        headers=HEADERS, json=payload,
    )
    if r.status_code == 200:
        created = r.json().get("data", {}).get("name", f"{name} - {abbr}")
        _account_cache[name] = created
        return created

    error = get_error(r.json())
    if is_duplicate_error(error):
        return find_account(name, abbr)

    return None


def resolve_account(name, tally_parent, abbr, company_name):
    """Find existing account or create it. Returns (account_name, was_created)."""
    found = find_account(name, abbr)
    if found:
        return found, False

    created = create_account(name, tally_parent, abbr, company_name)
    if created:
        return created, True

    return None, False


def patch_account_type(account_name):
    """
    Fix accounts that were previously created with account_type=Payable or Receivable.
    These types require a party on every journal line — unusable for loan/provision accounts.
    Patches them to account_type='' (blank) so they work without a party.

    This handles accounts wrongly created in earlier job runs.
    """
    # First fetch current account_type
    r = requests.get(
        f'{BASE_URL}/api/resource/Account/{requests.utils.quote(account_name)}',
        headers=HEADERS,
    )
    if r.status_code != 200:
        return

    data = r.json().get("data", {})
    current_type = data.get("account_type", "")

    if current_type in ("Payable", "Receivable"):
        patch = requests.put(
            f'{BASE_URL}/api/resource/Account/{requests.utils.quote(account_name)}',
            headers=HEADERS,
            json={"account_type": ""},
        )
        if patch.status_code == 200:
            print(f"    ✎ Patched account_type: {account_name}  (Payable → blank)")
        else:
            print(f"    ⚠ Could not patch {account_name}: {get_error(patch.json())[:80]}")

# ─────────────────────────────────────────────────────────────────────────────
# PART A — STOCK RECONCILIATION
# ─────────────────────────────────────────────────────────────────────────────
def build_stock_lines(items, full_warehouse):
    lines, skipped = [], 0
    for item in items:
        name = clean(item.get("NAME"))
        qty  = parse_qty(item.get("OPENINGBALANCE", "0"))
        rate = parse_rate(item.get("OPENINGRATE", "0"))

        if not name or qty <= 0:
            skipped += 1
            continue

        if rate == 0:
            ov = parse_amount(item.get("OPENINGVALUE", "0"))
            rate = round(ov / qty, 2) if ov > 0 else 0

        lines.append({
            "item_code"      : name,
            "warehouse"      : full_warehouse,
            "qty"            : qty,
            "valuation_rate" : max(rate, 0.01),
        })
    return lines, skipped


def stock_reconciliation_exists():
    """Check if a submitted Opening Stock Reconciliation already exists."""
    r = requests.get(
        f'{BASE_URL}/api/resource/Stock Reconciliation'
        f'?filters=[["purpose","=","Opening Stock"],["docstatus","=","1"]]'
        f'&fields=["name"]&limit=1',
        headers=HEADERS,
    )
    if r.status_code == 200:
        data = r.json().get("data", [])
        if data:
            return data[0]["name"]
    return None


def create_stock_reconciliation(items, company_name, full_warehouse, abbr):
    # Skip if already submitted from a previous run
    existing = stock_reconciliation_exists()
    if existing:
        print(f"  ~ Already submitted: {existing}  (skipping — stock already set)")
        return existing   # return name so caller knows it exists

    lines, skipped = build_stock_lines(items, full_warehouse)
    print(f"  Items with opening stock : {len(lines)}")
    print(f"  Items with zero stock    : {skipped} (skipped)")

    if not lines:
        print("  ✗ No items with opening stock found.")
        return None

    # FIX 1: ERPNext requires expense_account for Stock Reconciliation.
    # "Temporary Opening" is created automatically by ERPNext for this purpose.
    diff_account = f"{STOCK_DIFFERENCE_ACCOUNT_BASE} - {abbr}"

    payload = {
        "doctype"          : "Stock Reconciliation",
        "purpose"          : "Opening Stock",
        "posting_date"     : OPENING_DATE,
        "posting_time"     : "00:00:01",
        "company"          : company_name,
        "expense_account"  : diff_account,   # ← required field, was missing
        "items"            : lines,
    }
    r = requests.post(
        f"{BASE_URL}/api/resource/Stock Reconciliation",
        headers=HEADERS, json=payload,
    )
    if r.status_code != 200:
        print(f"  ✗ Stock Reconciliation FAILED: {get_error(r.json())[:150]}")
        return None

    doc_name = r.json().get("data", {}).get("name")
    print(f"  ✓ Stock Reconciliation created: {doc_name}")
    return doc_name


def is_already_submitted(doctype, doc_name):
    """Return True if the document is already in submitted state."""
    r = requests.get(
        f"{BASE_URL}/api/resource/{doctype}/{doc_name}",
        headers=HEADERS,
    )
    if r.status_code == 200:
        return r.json().get("data", {}).get("docstatus") == 1
    return False


def submit_document(doctype, doc_name):
    """Change docstatus 0→1 (Draft→Submitted). Skips if already submitted."""
    if is_already_submitted(doctype, doc_name):
        print(f"  ~ Already submitted: {doc_name}")
        return True

    r = requests.put(
        f"{BASE_URL}/api/resource/{doctype}/{doc_name}",
        headers=HEADERS,
        json={"docstatus": 1},
    )
    if r.status_code == 200:
        print(f"  ✓ Submitted: {doc_name}")
        return True
    print(f"  ✗ Submit failed: {get_error(r.json())[:120]}")
    return False

# ─────────────────────────────────────────────────────────────────────────────
# PART B — OPENING JOURNAL ENTRY
# ─────────────────────────────────────────────────────────────────────────────
def build_journal_lines(ledgers, abbr, company_name):
    """
    Build account lines for the Opening Journal Entry.
    Dr/Cr determined by sign of Tally opening balance:
      negative → Debit  (asset/expense side)
      positive → Credit (liability/equity/income side)
    """
    lines = []
    created_count = 0
    skipped = []

    for ledger in ledgers:
        parent = clean(ledger.get("PARENT", ""))
        name   = clean(ledger.get("NAME", ""))
        ob     = clean(ledger.get("OPENINGBALANCE", "0"))

        # Skip customer/supplier groups and Tally internal root
        if not name or parent in SKIP_LEDGER_GROUPS:
            continue

        # Skip P&L accounts — Opening Entry only accepts Balance Sheet accounts
        # (Assets, Liabilities, Equity). Expense/Income accounts are not allowed.
        if parent in SKIP_PL_GROUPS:
            continue

        try:
            ob_float = float(ob)
        except ValueError:
            continue
        if ob_float == 0:
            continue

        amount = abs(ob_float)
        account_name, was_created = resolve_account(name, parent, abbr, company_name)

        if not account_name:
            print(f"    ✗ Cannot find or create: '{name}' (group: {parent}) — skipping")
            skipped.append(name)
            continue

        # Fix any account wrongly created with Payable/Receivable type
        patch_account_type(account_name)

        tag   = "CREATED" if was_created else "found  "
        dr_cr = "DR" if ob_float < 0 else "CR"
        print(f"    {dr_cr} [{tag}] {name:38s}  ₹{amount:>14,.2f}")

        if ob_float < 0:
            lines.append({"account": account_name,
                          "debit_in_account_currency": amount,
                          "credit_in_account_currency": 0})
        else:
            lines.append({"account": account_name,
                          "debit_in_account_currency": 0,
                          "credit_in_account_currency": amount})

        if was_created:
            created_count += 1

    return lines, created_count, skipped


def create_opening_journal_entry(ledgers, company_name, abbr):
    print("  Resolving accounts (finding or creating in ERPNext)...")
    lines, created_count, skipped = build_journal_lines(ledgers, abbr, company_name)

    total_dr = sum(l["debit_in_account_currency"]  for l in lines)
    total_cr = sum(l["credit_in_account_currency"] for l in lines)
    diff     = abs(total_dr - total_cr)

    print(f"\n  Accounts auto-created  : {created_count}")
    print(f"  Accounts skipped       : {len(skipped)}")
    if skipped:
        for s in skipped:
            print(f"    - {s}")
    print(f"  Total Debit            : ₹{total_dr:>14,.2f}")
    print(f"  Total Credit           : ₹{total_cr:>14,.2f}")

    if not lines:
        print("  ✗ No valid lines — nothing to post.")
        return None

    # Balance via Temporary Opening account
    if diff > 0.01:
        print(f"  ⚠ Difference ₹{diff:,.2f} → balanced via Temporary Opening - {abbr}")
        temp = f"Temporary Opening - {abbr}"
        if total_dr > total_cr:
            lines.append({"account": temp,
                          "credit_in_account_currency": diff,
                          "debit_in_account_currency": 0})
        else:
            lines.append({"account": temp,
                          "debit_in_account_currency": diff,
                          "credit_in_account_currency": 0})

    payload = {
        "doctype"      : "Journal Entry",
        "voucher_type" : "Opening Entry",
        "posting_date" : OPENING_DATE,
        "company"      : company_name,
        "accounts"     : lines,
        "remark"       : "Opening balances migrated from Tally — FY 2024-25",
    }
    r = requests.post(
        f"{BASE_URL}/api/resource/Journal Entry",
        headers=HEADERS, json=payload,
    )
    if r.status_code != 200:
        print(f"  ✗ Journal Entry FAILED: {get_error(r.json())[:200]}")
        return None

    doc_name = r.json().get("data", {}).get("name")
    print(f"  ✓ Journal Entry created: {doc_name}")
    return doc_name

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
def migrate():
    if not check_connection():
        return

    if args.list_warehouses:
        list_warehouses()
        return

    company_name, abbr = get_company_info()
    full_warehouse     = resolve_warehouse(abbr)

    print(f"  Company   : {company_name}  ({abbr})")
    print(f"  Date      : {OPENING_DATE}")
    print()

    # Ensure FY 2024-25 exists — a fresh site has no fiscal years
    print("Checking Fiscal Year...")
    ensure_fiscal_year(company_name)
    print()

    with open("output/master_stock_items.json", encoding="utf-8") as f:
        items = json.load(f)["ENVELOPE"]["BODY"]["DATA"]["COLLECTION"]["STOCKITEM"]

    with open("output/master_ledgers.json", encoding="utf-8") as f:
        ledgers = json.load(f)["ENVELOPE"]["BODY"]["DATA"]["COLLECTION"]["LEDGER"]

    # ── PART A ────────────────────────────────────────────────────────────────
    print("─" * 52)
    print("  PART A: Stock Reconciliation")
    print("─" * 52)
    stock_doc = create_stock_reconciliation(items, company_name, full_warehouse, abbr)
    stock_ok  = False
    if stock_doc:
        stock_ok = submit_document("Stock Reconciliation", stock_doc)

    # ── PART B ────────────────────────────────────────────────────────────────
    print()
    print("─" * 52)
    print("  PART B: Opening Journal Entry (Accounts)")
    print("─" * 52)
    journal_doc = create_opening_journal_entry(ledgers, company_name, abbr)
    journal_ok  = False
    if journal_doc:
        journal_ok = submit_document("Journal Entry", journal_doc)

    # ── SUMMARY ───────────────────────────────────────────────────────────────
    print()
    print("=" * 52)
    print("  MIGRATION SUMMARY")
    print("=" * 52)
    print(f"  Stock Reconciliation : {'✓ Submitted' if stock_ok  else '✗ Failed'}")
    print(f"  Opening Journal Entry: {'✓ Submitted' if journal_ok else '✗ Failed'}")

    if stock_ok and journal_ok:
        print()
        print("  ✓ Step 4 complete!")
        print()
        print("  Verify in ERPNext:")
        print("    Stock    → Stock Reconciliation  (1 submitted entry)")
        print("    Stock    → Stock Balance          (148 items with qty)")
        print("    Accounts → Journal Entry          (1 Opening Entry)")
        print("    Accounts → Trial Balance          (compare with Tally)")
        print()
        print("  Next → python3 migrate_sales.py  (Step 5: Sales Invoices)")
    else:
        print()
        print("  Some parts failed — check errors above and re-run.")
        print("  Safe to re-run: existing docs won't be duplicated.")


if __name__ == "__main__":
    print()
    print("╔══════════════════════════════════════════════════╗")
    print("║  Tally → ERPNext Migration — Step 4: Opening    ║")
    print("║  Venkateshwara Traders                          ║")
    print("╚══════════════════════════════════════════════════╝")
    print()
    migrate()
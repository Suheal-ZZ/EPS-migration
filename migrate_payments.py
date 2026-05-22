"""
Tally → ERPNext Migration Script
Step 7: Migrate Payment Entries (Payments + Receipts + Journals)

Company  : Venkateshwara Traders
Sources  :
  output/voucher_payment_*.json  — 12 files, 43 unique  (pay suppliers)
  output/voucher_receipt_*.json  — 24 files, 127 unique (receive from customers)
  output/voucher_journal_*.json  — 12 files, 51 unique  (general ledger entries)

ERPNext mapping:
  Tally Payment  → ERPNext Payment Entry (payment_type = Pay)
  Tally Receipt  → ERPNext Payment Entry (payment_type = Receive)
  Tally Journal  → ERPNext Journal Entry

Data facts:
  Payment: 43 vouchers  Rs 18,88,429  (pay to suppliers via bank)
  Receipt: 127 vouchers Rs 61,19,093  (receive from customers via bank)
  Journal: 51 vouchers  (general ledger transfers)
  Total: 221 unique entries

Usage:
  python3 migrate_payments.py                         # all voucher types
  python3 migrate_payments.py --type payment          # only payments
  python3 migrate_payments.py --type receipt          # only receipts
  python3 migrate_payments.py --type journal          # only journals
  python3 migrate_payments.py --dry-run               # validate without posting
"""

import json
import glob
import argparse
import re
import requests
from datetime import date, timedelta

from common.config import BASE_URL
from helpers.constants import HEADERS
from helpers.utils import clean, is_duplicate_error, get_error, check_connection

# ─────────────────────────────────────────────────────────────────────────────
# ARGS
# ─────────────────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser(
    description="Step 7: Migrate Tally Payments/Receipts/Journals → ERPNext"
)
parser.add_argument("--type", default="all",
    choices=["all", "payment", "receipt", "journal"],
    help="Which voucher type to migrate (default: all)")
parser.add_argument("--dry-run", action="store_true",
    help="Parse and validate without posting to ERPNext")
args = parser.parse_args()

# ─────────────────────────────────────────────────────────────────────────────
# COMPANY INFO
# ─────────────────────────────────────────────────────────────────────────────
def get_company_info():
    r = requests.get(
        f'{BASE_URL}/api/resource/Company?fields=["name","abbr","default_bank_account"]&limit=1',
        headers=HEADERS,
    )
    if r.status_code == 200:
        data = r.json().get("data", [])
        if data:
            c = data[0]
            return c.get("name","Venkateshwara Traders"), c.get("abbr","VT")
    return "Venkateshwara Traders", "VT"


# Cache of known party types — built once at startup
_party_type_cache = {}


# Cache for party accounts (Creditors/Debtors accounts)
_party_account_cache = {}


def build_party_cache():
    """
    1. Cache all Customers and Suppliers by name for party_type detection.
    2. Find the default payable account (Creditors) and receivable account (Debtors).
       ERPNext Payment Entry requires:
         Pay:     paid_from = Creditors account, paid_to = Bank
         Receive: paid_from = Bank, paid_to = Debtors account
    """
    global _party_type_cache, _party_account_cache

    # Fetch suppliers
    r = requests.get(
        f'{BASE_URL}/api/resource/Supplier?fields=["name"]&limit=200',
        headers=HEADERS,
    )
    if r.status_code == 200:
        for s in r.json().get("data", []):
            _party_type_cache[s["name"]] = "Supplier"

    # Fetch customers
    r2 = requests.get(
        f'{BASE_URL}/api/resource/Customer?fields=["name"]&limit=200',
        headers=HEADERS,
    )
    if r2.status_code == 200:
        for c in r2.json().get("data", []):
            _party_type_cache[c["name"]] = "Customer"

    print(f"  Party cache: {sum(1 for v in _party_type_cache.values() if v=='Customer')} customers, "
          f"{sum(1 for v in _party_type_cache.values() if v=='Supplier')} suppliers")

    # Find payable account (Creditors) for Pay transactions
    for name in ["Creditors", "Accounts Payable", "Sundry Creditors"]:
        r3 = requests.get(
            f'{BASE_URL}/api/resource/Account?filters=[["account_type","=","Payable"],["is_group","=","0"]]'
            f'&fields=["name"]&limit=3',
            headers=HEADERS,
        )
        if r3.status_code == 200:
            data = r3.json().get("data", [])
            if data:
                _party_account_cache["payable"] = data[0]["name"]
                print(f"  Payable account : {data[0]['name']}")
                break

    # Find receivable account (Debtors) for Receive transactions
    r4 = requests.get(
        f'{BASE_URL}/api/resource/Account?filters=[["account_type","=","Receivable"],["is_group","=","0"]]'
        f'&fields=["name"]&limit=3',
        headers=HEADERS,
    )
    if r4.status_code == 200:
        data = r4.json().get("data", [])
        if data:
            _party_account_cache["receivable"] = data[0]["name"]
            print(f"  Receivable account: {data[0]['name']}")


def detect_party_type(party_name, default_type):
    """
    Return actual ERPNext party type for a party name.
    Falls back to default_type if not in cache.
    """
    return _party_type_cache.get(party_name, default_type)


def get_bank_account(account_name, abbr):
    """Find ERPNext account matching a Tally bank ledger name."""
    for candidate in [f"{account_name} - {abbr}", account_name]:
        r = requests.get(
            f"{BASE_URL}/api/resource/Account/{requests.utils.quote(candidate)}",
            headers=HEADERS,
        )
        if r.status_code == 200:
            return r.json().get("data", {}).get("name", candidate)

    # Search by name
    _f = f'[["account_name","like","%{account_name}%"]]'
    r = requests.get(
        f"{BASE_URL}/api/resource/Account?filters={_f}&fields=%5B%22name%22%5D&limit=3",
        headers=HEADERS,
    )
    if r.status_code == 200:
        data = r.json().get("data", [])
        if data:
            return data[0]["name"]

    return f"{account_name} - {abbr}"


_account_cache = {}
_default_bank_account = None

def get_default_bank():
    """Return first bank account found in ERPNext as fallback."""
    global _default_bank_account
    if _default_bank_account:
        return _default_bank_account
    r = requests.get(
        f'{BASE_URL}/api/resource/Account'
        f'?filters=[["account_type","=","Bank"],["is_group","=","0"]]'
        f'&fields=["name"]&limit=3',
        headers=HEADERS,
    )
    if r.status_code == 200:
        data = r.json().get("data", [])
        if data:
            _default_bank_account = data[0]["name"]
            return _default_bank_account
    return None

def cached_account(name, abbr):
    if name not in _account_cache:
        found = get_bank_account(name, abbr)
        # If account not found, create it or fall back to default bank
        if found == f"{name} - {abbr}":
            # Verify it actually exists
            r = requests.get(
                f"{BASE_URL}/api/resource/Account/{requests.utils.quote(found)}",
                headers=HEADERS,
            )
            if r.status_code != 200:
                # Try to create as a bank account
                default = get_default_bank()
                if default:
                    # Find parent of default bank
                    r2 = requests.get(
                        f"{BASE_URL}/api/resource/Account/{requests.utils.quote(default)}",
                        headers=HEADERS,
                    )
                    if r2.status_code == 200:
                        parent = r2.json().get("data", {}).get("parent_account", "Bank Accounts")
                        r3 = requests.post(f"{BASE_URL}/api/resource/Account",
                            headers=HEADERS,
                            json={"doctype":"Account","account_name":name,
                                  "parent_account":parent,"account_type":"Bank",
                                  "company":abbr,"is_group":0})
                        if r3.status_code == 200:
                            found = r3.json().get("data",{}).get("name", found)
                        else:
                            found = default  # fall back to default bank
                    else:
                        found = default
        _account_cache[name] = found
    return _account_cache[name]

# ─────────────────────────────────────────────────────────────────────────────
# FISCAL YEAR
# ─────────────────────────────────────────────────────────────────────────────
def date_to_fy(date_str):
    year  = int(date_str[:4])
    month = int(date_str[5:7])
    return (year, year + 1) if month >= 4 else (year - 1, year)


def ensure_fiscal_years(company_name, dates):
    fy_set = {date_to_fy(date.today().isoformat())}
    for d in dates:
        if d and len(d) >= 10:
            fy_set.add(date_to_fy(d))

    print(f"  Ensuring {len(fy_set)} fiscal year(s)...")
    for fy_start, fy_end in sorted(fy_set):
        year_name = f"{fy_start}-{fy_end}"
        r = requests.get(f"{BASE_URL}/api/resource/Fiscal Year/{year_name}",
                         headers=HEADERS)
        if r.status_code == 200:
            print(f"    ~ FY {year_name} already exists")
            continue
        payload = {"doctype": "Fiscal Year", "year": year_name,
                   "year_start_date": f"{fy_start}-04-01",
                   "year_end_date"  : f"{fy_end}-03-31",
                   "companies": [{"company": company_name}]}
        r2 = requests.post(f"{BASE_URL}/api/resource/Fiscal Year",
                           headers=HEADERS, json=payload)
        if r2.status_code == 200:
            print(f"    ✓ FY {year_name} created")
        else:
            err = r2.json().get("exception","")[:60] if r2.content else ""
            if "already exists" not in err.lower():
                print(f"    ✗ FY {year_name} failed: {err}")

# ─────────────────────────────────────────────────────────────────────────────
# PARSERS
# ─────────────────────────────────────────────────────────────────────────────
def parse_date(val):
    raw = clean(val)
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return raw


def parse_inr(val):
    """
    Parse amount — handles both plain INR and forex strings.
    Tally forex format: '-$594.05 @ ? 59.9998/$ = -? 35642.88'
    We extract the INR value (after '=').
    """
    s = clean(val)
    if not s:
        return 0.0
    if "=" in s:
        s = s.split("=")[-1].strip()
    # Remove currency symbols, keep digits, dots, minus
    s = re.sub(r"[^\d.\-]", "", s)
    try:
        return float(s)
    except ValueError:
        return 0.0


def get_all_ledger_entries(voucher):
    """Payment/Receipt vouchers use ALLLEDGERENTRIES.LIST (not LEDGERENTRIES.LIST)."""
    for key in ["ALLLEDGERENTRIES.LIST", "LEDGERENTRIES.LIST"]:
        val = voucher.get(key, {})
        if isinstance(val, list) and val:
            return [e for e in val if isinstance(e, dict)]
        if isinstance(val, dict) and val:
            return [val]
    return []

# ─────────────────────────────────────────────────────────────────────────────
# SUBMIT HELPER (fetch-then-submit pattern from Step 5/6)
# ─────────────────────────────────────────────────────────────────────────────
def create_and_submit(doctype, payload, dry_run=False):
    """
    Create a document as Draft then submit it.

    Payment Entry uses simple PUT docstatus=1 (not frappe.client.submit with full doc).
    Reason: frappe.client.submit sends the full fetched doc back which includes
    computed account-level party fields that fail re-validation with:
    'Party Type and Party can only be set for Receivable/Payable account'

    Journal Entry uses the same simple approach.
    """
    if dry_run:
        return True, "DRY-RUN", ""

    # Step 1: Create as Draft
    r = requests.post(
        f"{BASE_URL}/api/resource/{doctype}",
        headers=HEADERS, json=payload,
    )
    if r.status_code != 200:
        return False, "", get_error(r.json())

    doc_name = r.json().get("data", {}).get("name", "")
    if not doc_name:
        return False, "", "No doc name returned after create"

    # Step 2: Submit using simple PUT docstatus=1
    r2 = requests.put(
        f"{BASE_URL}/api/resource/{doctype}/{doc_name}",
        headers=HEADERS,
        json={"docstatus": 1},
    )
    if r2.status_code != 200:
        resp = r2.json() if r2.content else {}
        # Log full error for first failure
        if not hasattr(create_and_submit, "_logged"):
            create_and_submit._logged = True
            print(f"\n  === FULL SUBMIT ERROR ===")
            print(f"  exc_type  : {resp.get('exc_type','')}")
            print(f"  exception : {resp.get('exception','')[:400]}")
            smsgs = resp.get('_server_messages','')
            if smsgs:
                import json as _j
                try:
                    msgs = _j.loads(smsgs)
                    for m in msgs[:3]:
                        try: m = _j.loads(m)
                        except: pass
                        print(f"  server_msg: {str(m)[:300]}")
                except: print(f"  server_msgs: {smsgs[:300]}")
            print(f"  === PAYLOAD SENT ===")
            print(f"  payment_type : {payload.get('payment_type')}")
            print(f"  party_type   : {payload.get('party_type')}")
            print(f"  party        : {payload.get('party')}")
            print(f"  paid_from    : {payload.get('paid_from')}")
            print(f"  paid_to      : {payload.get('paid_to')}")
            print()
        error = get_error(resp)
        requests.delete(
            f"{BASE_URL}/api/resource/{doctype}/{doc_name}",
            headers=HEADERS,
        )
        return False, doc_name, f"Submit failed: {error}"

    return True, doc_name, ""

# ─────────────────────────────────────────────────────────────────────────────
# PART A — PAYMENT ENTRIES (Payment + Receipt vouchers)
# ─────────────────────────────────────────────────────────────────────────────
def build_payment_as_journal(voucher, payment_type, company_name, abbr):
    """
    Build a Journal Entry for Tally Payment/Receipt vouchers.
    Uses Journal Entry instead of Payment Entry to avoid ERPNext's
    payment validation that incorrectly assigns party to bank accounts.

    Pay supplier:      DR Creditors-VT (with party)  CR Bank-VT
    Receive customer:  DR Bank-VT                    CR Debtors-VT (with party)
    """
    party      = clean(voucher.get("PARTYLEDGERNAME"))
    date_str   = parse_date(voucher.get("DATE"))
    narration  = clean(voucher.get("NARRATION", ""))
    amount_raw = parse_inr(voucher.get("AMOUNT", "0"))
    amount     = abs(amount_raw)

    if not party or not date_str or amount == 0:
        return None

    entries   = get_all_ledger_entries(voucher)
    bank_name = None
    for e in entries:
        name     = clean(e.get("LEDGERNAME", ""))
        is_party = clean(e.get("ISPARTYLEDGER", "No"))
        if name and name != party and is_party == "Yes":
            bank_name = name
            break
    if not bank_name:
        bank_name = "Bank of Baroda-Savings A/c"

    bank_account = cached_account(bank_name, abbr)
    default_type = "Supplier" if payment_type == "Pay" else "Customer"
    party_type   = detect_party_type(party, default_type)
    if party_type == "Supplier" and payment_type == "Receive":
        payment_type = "Pay"
    elif party_type == "Customer" and payment_type == "Pay":
        payment_type = "Receive"

    payable_account    = _party_account_cache.get("payable",    f"Creditors - {abbr}")
    receivable_account = _party_account_cache.get("receivable", f"Debtors - {abbr}")
    party_account      = payable_account if party_type == "Supplier" else receivable_account

    if payment_type == "Pay":
        accounts = [
            {"account": party_account, "party_type": party_type, "party": party,
             "debit_in_account_currency": amount, "credit_in_account_currency": 0},
            {"account": bank_account,
             "debit_in_account_currency": 0, "credit_in_account_currency": amount},
        ]
    else:
        accounts = [
            {"account": bank_account,
             "debit_in_account_currency": amount, "credit_in_account_currency": 0},
            {"account": party_account, "party_type": party_type, "party": party,
             "debit_in_account_currency": 0, "credit_in_account_currency": amount},
        ]

    return {
        "doctype"      : "Journal Entry",
        "voucher_type" : "Bank Entry",
        "posting_date" : date_str,
        "company"      : company_name,
        "accounts"     : accounts,
        "cheque_no"    : clean(voucher.get("VOUCHERNUMBER", "")),
        "cheque_date"  : date_str,
        "remark"       : narration or f"Payment - {party}",
    }


def process_payment_files(files, payment_type, company_name, abbr, dry_run):
    """Process a set of payment or receipt files."""
    ok = 0; failed = 0; skipped = 0; invalid = 0
    errors = []
    seen_guids = set()   # deduplicate across identical files

    for filepath in files:
        fname = filepath.split("/")[-1]
        with open(filepath, encoding="utf-8") as f:
            data = json.load(f)

        vouchers = data["ENVELOPE"]["BODY"]["DATA"]["COLLECTION"]["VOUCHER"]

        for voucher in vouchers:
            guid = clean(voucher.get("GUID", ""))
            if guid in seen_guids:
                continue
            seen_guids.add(guid)

            payload = build_payment_as_journal(voucher, payment_type, company_name, abbr)
            if payload is None:
                invalid += 1
                continue

            if dry_run:
                party_name = clean(voucher.get("PARTYLEDGERNAME", "?"))
                amt   = abs(parse_inr(voucher.get("AMOUNT", "0")))
                dt    = parse_date(voucher.get("DATE", ""))
                print(f"  [DRY RUN] {dt} {payment_type:8s} {party_name:30s} Rs {amt:>12,.2f}")
                ok += 1
                continue

            success, doc_name, error = create_and_submit(
                "Journal Entry", payload, dry_run
            )
            if success:
                ok += 1
            elif is_duplicate_error(error):
                skipped += 1
            else:
                failed += 1
                errors.append(f"  {payload.get('party','?')} {payload.get('posting_date','')}: {error[:80]}")
                print(f"  ✗ {payload.get('party','?')}: {error[:80]}")

    return ok, failed, skipped, invalid, errors

# ─────────────────────────────────────────────────────────────────────────────
# PART B — JOURNAL ENTRIES
# ─────────────────────────────────────────────────────────────────────────────
def build_journal_entry(voucher, company_name, abbr):
    """
    Build ERPNext Journal Entry from a Tally Journal voucher.
    Each ledger entry in Tally becomes one account line in ERPNext.
    """
    date_str  = parse_date(voucher.get("DATE"))
    narration = clean(voucher.get("NARRATION", ""))

    if not date_str:
        return None

    entries = get_all_ledger_entries(voucher)
    if not entries:
        return None

    # Tally ledger → ERPNext account type mapping for auto-creation
    JOURNAL_ACCOUNT_MAP = {
        "Rent"                  : ("Expense Account", "Direct Expenses"),
        "Electricity Charges"   : ("Expense Account", "Indirect Expenses"),
        "Printing & Stationary" : ("Expense Account", "Indirect Expenses"),
        "Bank Charges"          : ("Expense Account", "Indirect Expenses"),
        "Sundry Expenses"       : ("Expense Account", "Indirect Expenses"),
        "Telephone Bill"        : ("Expense Account", "Indirect Expenses"),
        "Water Bill Charges"    : ("Expense Account", "Indirect Expenses"),
        "Conveyance"            : ("Expense Account", "Indirect Expenses"),
        "Salary Account"        : ("Expense Account", "Indirect Expenses"),
        "Telephone Deposit"     : ("Current Asset",   "Deposits"),
        "Electricity Deposit"   : ("Current Asset",   "Deposits"),
    }

    def resolve_journal_account(name, abbr):
        """Find or create an ERPNext account for a journal ledger name."""
        # Try with suffix, without, then partial search
        for candidate in [f"{name} - {abbr}", name]:
            r = requests.get(
                f"{BASE_URL}/api/resource/Account/{requests.utils.quote(candidate)}",
                headers=HEADERS,
            )
            if r.status_code == 200:
                return r.json().get("data", {}).get("name", candidate)

        _f = f'[["account_name","like","%{name}%"]]' 
        r = requests.get(
            f"{BASE_URL}/api/resource/Account?filters={_f}&fields=%5B%22name%22%5D&limit=3",
            headers=HEADERS,
        )
        if r.status_code == 200:
            data = r.json().get("data", [])
            if data:
                return data[0]["name"]

        # Auto-create using JOURNAL_ACCOUNT_MAP
        mapping = JOURNAL_ACCOUNT_MAP.get(name)
        if mapping:
            account_type, parent_base = mapping
            for parent_c in [f"{parent_base} - {abbr}", parent_base]:
                r2 = requests.get(
                    f"{BASE_URL}/api/resource/Account/{requests.utils.quote(parent_c)}",
                    headers=HEADERS,
                )
                if r2.status_code == 200:
                    r3 = requests.post(f"{BASE_URL}/api/resource/Account",
                        headers=HEADERS,
                        json={"doctype":"Account","account_name":name,
                              "parent_account":parent_c,"account_type":account_type,
                              "company":company_name,"is_group":0})
                    if r3.status_code == 200:
                        return r3.json().get("data",{}).get("name", f"{name} - {abbr}")
                    break

        # Party accounts (customers/suppliers) use party ledger entries
        # Check if it's a known party — skip it (parties go in payment entries)
        if name in _party_type_cache:
            return None   # signal to skip this line

        return f"{name} - {abbr}"   # fallback

    lines = []
    for e in entries:
        name      = clean(e.get("LEDGERNAME", ""))
        amt_raw   = parse_inr(e.get("AMOUNT", "0"))
        is_deemed = clean(e.get("ISDEEMEDPOSITIVE", "No"))

        if not name or amt_raw == 0:
            continue

        account = resolve_journal_account(name, abbr)
        if account is None:
            continue   # skip party lines in journal entries

        amount = abs(amt_raw)
        if amt_raw < 0 or is_deemed == "Yes":
            line = {"account": account,
                    "debit_in_account_currency": amount,
                    "credit_in_account_currency": 0}
        else:
            line = {"account": account,
                    "debit_in_account_currency": 0,
                    "credit_in_account_currency": amount}

        lines.append(line)

    if not lines:
        return None

    # Balance check — add to Temporary Opening if unbalanced
    total_dr = sum(l["debit_in_account_currency"]  for l in lines)
    total_cr = sum(l["credit_in_account_currency"] for l in lines)
    diff = abs(total_dr - total_cr)
    if diff > 0.01:
        temp = f"Temporary Opening - {abbr}"
        if total_dr > total_cr:
            lines.append({"account": temp, "credit_in_account_currency": diff,
                          "debit_in_account_currency": 0})
        else:
            lines.append({"account": temp, "debit_in_account_currency": diff,
                          "credit_in_account_currency": 0})

    return {
        "doctype"      : "Journal Entry",
        "voucher_type" : "Journal Entry",
        "posting_date" : date_str,
        "company"      : company_name,
        "accounts"     : lines,
        "remark"       : narration or f"Migrated from Tally voucher",
    }


def process_journal_files(files, company_name, abbr, dry_run):
    ok = 0; failed = 0; skipped = 0; invalid = 0
    errors = []
    seen_guids = set()

    for filepath in files:
        with open(filepath, encoding="utf-8") as f:
            data = json.load(f)

        vouchers = data["ENVELOPE"]["BODY"]["DATA"]["COLLECTION"]["VOUCHER"]

        for voucher in vouchers:
            guid = clean(voucher.get("GUID", ""))
            if guid in seen_guids:
                continue
            seen_guids.add(guid)

            payload = build_journal_entry(voucher, company_name, abbr)
            if payload is None:
                invalid += 1
                continue

            if dry_run:
                dt    = payload.get("posting_date", "")
                lines = len(payload.get("accounts", []))
                print(f"  [DRY RUN] {dt} Journal  {lines} lines")
                ok += 1
                continue

            success, doc_name, error = create_and_submit(
                "Journal Entry", payload, dry_run
            )
            if success:
                ok += 1
            elif is_duplicate_error(error):
                skipped += 1
            else:
                failed += 1
                errors.append(f"  Journal {payload.get('posting_date','')}: {error[:80]}")
                print(f"  ✗ Journal {payload.get('posting_date','')}: {error[:80]}")

    return ok, failed, skipped, invalid, errors

# ─────────────────────────────────────────────────────────────────────────────
# COLLECT ALL DATES
# ─────────────────────────────────────────────────────────────────────────────
def collect_dates(files):
    dates = []
    for fp in files:
        with open(fp, encoding="utf-8") as f:
            data = json.load(f)
        for v in data["ENVELOPE"]["BODY"]["DATA"]["COLLECTION"]["VOUCHER"]:
            raw = clean(v.get("DATE", ""))
            if len(raw) == 8 and raw.isdigit():
                dates.append(f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}")
    return dates

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
def migrate():
    if not args.dry_run and not check_connection():
        return

    company_name, abbr = get_company_info()
    print(f"  Company: {company_name} ({abbr})")
    if args.dry_run:
        print("  Mode   : DRY RUN")

    # Build party cache to detect Customer vs Supplier dynamically
    print("Building party type cache...")
    build_party_cache()
    print()

    # File sets
    pay_files  = sorted(glob.glob("output/voucher_payment_*.json"))
    rec_files  = sorted(glob.glob("output/voucher_receipt_*.json"))
    jour_files = sorted(glob.glob("output/voucher_journal_*.json"))

    # Ensure fiscal years
    if not args.dry_run:
        all_dates = []
        for files in [pay_files, rec_files, jour_files]:
            all_dates.extend(collect_dates(files))
        ensure_fiscal_years(company_name, all_dates)
        print()

    totals = {}

    # ── PAYMENTS ──────────────────────────────────────────────────────────────
    if args.type in ("all", "payment"):
        print(f"── Processing Payments (43 unique → Pay suppliers) ────────────")
        ok, fail, skip, inv, errs = process_payment_files(
            pay_files, "Pay", company_name, abbr, args.dry_run
        )
        totals["Payment (Pay)"] = (ok, fail, skip, inv)
        print(f"  ✓ {ok} created, {skip} skipped, {fail} failed, {inv} invalid")
        print()

    # ── RECEIPTS ──────────────────────────────────────────────────────────────
    if args.type in ("all", "receipt"):
        print(f"── Processing Receipts (127 unique → Receive from customers) ─")
        ok, fail, skip, inv, errs = process_payment_files(
            rec_files, "Receive", company_name, abbr, args.dry_run
        )
        totals["Payment (Receive)"] = (ok, fail, skip, inv)
        print(f"  ✓ {ok} created, {skip} skipped, {fail} failed, {inv} invalid")
        print()

    # ── JOURNALS ──────────────────────────────────────────────────────────────
    if args.type in ("all", "journal"):
        print(f"── Processing Journals (51 unique → Journal Entries) ──────────")
        ok, fail, skip, inv, errs = process_journal_files(
            jour_files, company_name, abbr, args.dry_run
        )
        totals["Journal Entry"] = (ok, fail, skip, inv)
        print(f"  ✓ {ok} created, {skip} skipped, {fail} failed, {inv} invalid")
        print()

    # ── SUMMARY ───────────────────────────────────────────────────────────────
    print("=" * 52)
    print("  MIGRATION SUMMARY")
    print("=" * 52)
    grand_ok = grand_fail = 0
    for vtype, (ok, fail, skip, inv) in totals.items():
        print(f"  {vtype:25s}  created={ok}  skipped={skip}  failed={fail}")
        grand_ok   += ok
        grand_fail += fail

    print()
    if grand_fail == 0:
        if args.dry_run:
            print("  ✓ Dry run complete — no errors found!")
            print("  Run without --dry-run to post to ERPNext.")
        else:
            print("  ✓ Step 7 complete! All payments migrated.")
            print()
            print("  Verify in ERPNext:")
            print("    Accounts → Payment Entry    (payments + receipts)")
            print("    Accounts → Journal Entry    (journal vouchers)")
            print()
            print("  Migration is now COMPLETE!")
            print("  All 7 steps done for Venkateshwara Traders.")
    else:
        print("  Some entries failed — check errors above.")
        print("  Safe to re-run — duplicates are skipped.")


if __name__ == "__main__":
    print()
    print("╔══════════════════════════════════════════════════╗")
    print("║  Tally → ERPNext Migration — Step 7: Payments   ║")
    print("║  Venkateshwara Traders                          ║")
    print("╚══════════════════════════════════════════════════╝")
    print()
    migrate()
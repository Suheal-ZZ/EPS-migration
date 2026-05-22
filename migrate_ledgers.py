"""
Step 1: Migrate Ledgers → Customers & Suppliers 
"""

import json
import requests
from helpers.constants import HEADERS
from common.config import BASE_URL, API_KEY, API_SECRET
from helpers.utils import clean, check_connection
# ─────────────────────────────────────────────────────────────────────────────

JSON_FILE = "output/master_ledgers.json"

CUSTOMER_GROUPS = ["Sundry Debtors"]
SUPPLIER_GROUPS = ["Sundry Creditors"]
SKIP_GROUPS     = ["\x04 Primary", ""]


def load_ledgers():
    print(f"\nReading {JSON_FILE}...")
    with open(JSON_FILE, encoding="utf-8") as f:
        data = json.load(f)
    ledgers = data["ENVELOPE"]["BODY"]["DATA"]["COLLECTION"]["LEDGER"]
    print(f"Found {len(ledgers)} ledgers in Tally export\n")
    return ledgers


def get_valid_group(doctype):
    """
    Fetch leaf-level (is_group=0) groups from ERPNext.
    Only leaf groups can be assigned to Customer/Supplier records.
    """
    r = requests.get(
        f"{BASE_URL}/api/resource/{doctype}?fields=[\"name\",\"is_group\"]&limit=50",
        headers=HEADERS
    )
    if r.status_code == 200:
        groups = r.json().get("data", [])
        leaf_groups = [g["name"] for g in groups if not g.get("is_group")]
        print(f"  {doctype} leaf groups: {leaf_groups}")
        for preferred in ["Commercial", "All Supplier Groups"]:
            if preferred in leaf_groups:
                return preferred
        if leaf_groups:
            return leaf_groups[0]
    return "Commercial" if "Customer" in doctype else "All Supplier Groups"


def create_customer(ledger, customer_group):
    name   = clean(ledger.get("NAME"))
    mobile = clean(ledger.get("LEDGERMOBILE"))
    payload = {
        "doctype"       : "Customer",
        "customer_name" : name,
        "customer_type" : "Company",
        "customer_group": customer_group,
        "territory"     : "All Territories",
    }
    if mobile:
        payload["mobile_no"] = mobile
    r = requests.post(f"{BASE_URL}/api/resource/Customer",
                      headers=HEADERS, json=payload)
    return r.status_code, r.json()


def create_supplier(ledger, supplier_group):
    name = clean(ledger.get("NAME"))
    payload = {
        "doctype"       : "Supplier",
        "supplier_name" : name,
        "supplier_type" : "Company",
        "supplier_group": supplier_group,
    }
    r = requests.post(f"{BASE_URL}/api/resource/Supplier",
                      headers=HEADERS, json=payload)
    return r.status_code, r.json()

def migrate():
    if not check_connection():
        return

    print("\nDetecting valid Customer/Supplier groups...")
    customer_group = get_valid_group("Customer Group")
    supplier_group = get_valid_group("Supplier Group")
    ledgers = load_ledgers()

    results = {
        "customers_ok": [], "customers_fail": [],
        "suppliers_ok": [], "suppliers_fail": [],
        "skipped": [],
    }

    for ledger in ledgers:
        parent = clean(ledger.get("PARENT"))
        name   = clean(ledger.get("NAME"))

        if not name or parent in SKIP_GROUPS:
            results["skipped"].append(name)
            continue

        if parent in CUSTOMER_GROUPS:
            status, resp = create_customer(ledger, customer_group)
            if status == 200:
                print(f"Customer: {name}")
                results["customers_ok"].append(name)
            else:
                error = str(resp.get("exception", resp.get("message", resp)))[:120] if isinstance(resp, dict) else str(resp)[:120]
                if "already exists" in error.lower() or "DuplicateEntryError" in error:
                    print(f"Already exists: {name}")
                    results["customers_ok"].append(name)
                else:
                    print(f"Customer FAILED: {name}\n      Error: {error}")
                    results["customers_fail"].append(name)

        elif parent in SUPPLIER_GROUPS:
            status, resp = create_supplier(ledger, supplier_group)
            if status == 200:
                print(f"Supplier: {name}")
                results["suppliers_ok"].append(name)
            else:
                error = str(resp.get("exception", resp.get("message", resp)))[:120] if isinstance(resp, dict) else str(resp)[:120]
                if "already exists" in error.lower() or "DuplicateEntryError" in error:
                    print(f"Already exists: {name}")
                    results["suppliers_ok"].append(name)
                else:
                    print(f"Supplier FAILED: {name}\n      Error: {error}")
                    results["suppliers_fail"].append(name)
        else:
            results["skipped"].append(name)

    print()
    print("  MIGRATION SUMMARY")
    # print(f"  Customers created   : {len(results['customers_ok'])}")
    # print(f"  Customers failed    : {len(results['customers_fail'])}")
    # print(f"  Suppliers created   : {len(results['suppliers_ok'])}")
    # print(f"  Suppliers failed    : {len(results['suppliers_fail'])}")
    # print(f"  Skipped (accounts)  : {len(results['skipped'])}")

    if results["customers_fail"] or results["suppliers_fail"]:
        print("\n  Failed items:", results["customers_fail"] + results["suppliers_fail"])
    else:
        print("\n All done! No errors.")
        print("\n  Verify in ERPNext:")
        print("    Selling - Customers  → should show 16")
        print("    Buying  - Suppliers  → should show 9")
        # print("\n  Next → python3 migrate_items.py  (Step 2: Stock Items)")


if __name__ == "__main__":
    print()
    migrate()
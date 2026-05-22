"""
Validation Script — Step 1: Customers & Suppliers
Compares Tally export data against ERPNext to confirm migration accuracy.

Usage: python3 validate_step1.py
"""

import json
import requests

from common.config import BASE_URL
from helpers.constants import HEADERS
from helpers.utils import clean

# ─────────────────────────────────────────────────────────────────────────────
# EXPECTED DATA FROM TALLY
# ─────────────────────────────────────────────────────────────────────────────
EXPECTED_CUSTOMERS = [
    "A to Z Stationers", "Alfa Provisions", "Anup and Co",
    "BMS Mart", "Candles and Matches", "Express Stores",
    "Indus Stores", "K. L. Stores", "RV Stores",
    "Retail Mart", "Salt and Pepper", "Sun Stores",
    "Sunday to Monday", "SuperFoods", "Titan Stores",
    "Zeta Provisions",
]

EXPECTED_SUPPLIERS = [
    "AVN Traders", "Confident Traders", "Ecko Honey Farms",
    "HKN Enterprises", "MM Frozen Foods", "S.M Traders",
    "SLV Enterprises", "Sai Farms", "VKN Transports",
]

# ─────────────────────────────────────────────────────────────────────────────
# FETCH FROM ERPNEXT
# ─────────────────────────────────────────────────────────────────────────────
def fetch_customers():
    r = requests.get(
        f'{BASE_URL}/api/resource/Customer'
        f'?fields=["customer_name","customer_group","territory"]&limit=200',
        headers=HEADERS,
    )
    if r.status_code == 200:
        return r.json().get("data", [])
    print(f"  ✗ Could not fetch customers: {r.status_code}")
    return []


def fetch_suppliers():
    r = requests.get(
        f'{BASE_URL}/api/resource/Supplier'
        f'?fields=["supplier_name","supplier_group","supplier_type"]&limit=200',
        headers=HEADERS,
    )
    if r.status_code == 200:
        return r.json().get("data", [])
    print(f"  ✗ Could not fetch suppliers: {r.status_code}")
    return []

# ─────────────────────────────────────────────────────────────────────────────
# COMPARE
# ─────────────────────────────────────────────────────────────────────────────
def compare(label, expected_list, actual_records, name_field):
    print(f"\n{'─'*60}")
    print(f"  {label}")
    print(f"{'─'*60}")

    actual_names = {r.get(name_field, "").strip() for r in actual_records}
    expected_set = {n.strip() for n in expected_list}

    # Check each expected name
    missing    = []
    found      = []
    duplicates = []

    for name in sorted(expected_list):
        if name in actual_names:
            found.append(name)
            print(f"  ✓ {name}")
        else:
            # Try fuzzy match (case-insensitive)
            match = next(
                (a for a in actual_names if a.lower() == name.lower()), None
            )
            if match:
                print(f"  ~ {name}  (found as '{match}' — case difference)")
                found.append(name)
            else:
                missing.append(name)
                print(f"  ✗ MISSING: {name}")

    # Check for unexpected extra records
    extra = sorted(actual_names - expected_set - {
        a for a in actual_names
        if any(e.lower() == a.lower() for e in expected_set)
    })
    if extra:
        print()
        print("  Extra records in ERPNext (not in Tally):")
        for e in extra:
            print(f"    + {e}")

    # Summary
    print()
    print(f"  Expected : {len(expected_list)}")
    print(f"  Found    : {len(found)}")
    print(f"  Missing  : {len(missing)}")
    print(f"  Extra    : {len(extra)}")

    if not missing and not extra:
        print(f"  ✓ PERFECT MATCH — all {len(expected_list)} records correct")
    else:
        if missing:
            print(f"  ✗ Missing from ERPNext: {missing}")
        if extra:
            print(f"  ⚠ Extra in ERPNext (not in Tally): {extra}")

    return len(missing) == 0 and len(extra) == 0

# ─────────────────────────────────────────────────────────────────────────────
# TALLY CROSS-CHECK
# ─────────────────────────────────────────────────────────────────────────────
def load_from_tally_export():
    """Load expected data directly from the Tally JSON export for cross-check."""
    try:
        with open("output/master_ledgers.json", encoding="utf-8") as f:
            d = json.load(f)
        ledgers = d["ENVELOPE"]["BODY"]["DATA"]["COLLECTION"]["LEDGER"]

        customers = []
        suppliers = []
        for l in ledgers:
            parent = clean(l.get("PARENT", ""))
            name   = clean(l.get("NAME", ""))
            if parent == "Sundry Debtors" and name:
                customers.append(name)
            elif parent == "Sundry Creditors" and name:
                suppliers.append(name)

        return sorted(customers), sorted(suppliers)
    except FileNotFoundError:
        return None, None

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
def validate():
    print()
    print("╔══════════════════════════════════════════════════╗")
    print("║  Migration Validation — Step 1: Customers &     ║")
    print("║  Suppliers                                      ║")
    print("╚══════════════════════════════════════════════════╝")
    print()

    # Try to load from actual Tally export file
    tally_customers, tally_suppliers = load_from_tally_export()
    if tally_customers:
        print(f"  Loaded from Tally export: "
              f"{len(tally_customers)} customers, {len(tally_suppliers)} suppliers")
        expected_customers = tally_customers
        expected_suppliers = tally_suppliers
    else:
        print("  Using hardcoded expected list (output/ folder not found)")
        expected_customers = EXPECTED_CUSTOMERS
        expected_suppliers = EXPECTED_SUPPLIERS

    # Fetch from ERPNext
    print("\nFetching data from ERPNext...")
    erp_customers = fetch_customers()
    erp_suppliers = fetch_suppliers()
    print(f"  ERPNext has: {len(erp_customers)} customers, {len(erp_suppliers)} suppliers")

    # Compare
    cust_ok = compare("CUSTOMERS", expected_customers, erp_customers, "customer_name")
    supp_ok = compare("SUPPLIERS", expected_suppliers, erp_suppliers, "supplier_name")

    # Overall result
    print()
    print("=" * 60)
    print("  VALIDATION RESULT")
    print("=" * 60)
    print(f"  Customers : {'✓ PASS' if cust_ok else '✗ FAIL'}")
    print(f"  Suppliers : {'✓ PASS' if supp_ok else '✗ FAIL'}")
    print()
    if cust_ok and supp_ok:
        print("  ✓ Step 1 validation PASSED")
        print("  All customers and suppliers match Tally exactly.")
    else:
        print("  ✗ Step 1 validation FAILED")
        print("  Fix discrepancies above, then re-run migrate_ledgers.py")

    print()
    print("  How to verify manually in Tally Prime:")
    print("    Gateway of Tally → Display More Reports")
    print("    → Account Books → Ledger")
    print("    → Group: Sundry Debtors   (should show 16 ledgers)")
    print("    → Group: Sundry Creditors (should show 9 ledgers)")
    print()
    print("  How to verify manually in ERPNext:")
    print("    Selling → Customers   (should show 16 records)")
    print("    Buying  → Suppliers   (should show 9 records)")


if __name__ == "__main__":
    validate()
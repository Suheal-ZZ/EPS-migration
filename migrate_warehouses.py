"""
Step 3: Migrate Godowns → Warehouses
"""

import json
import requests
from helpers.constants import HEADERS
from common.config import BASE_URL
from helpers.utils import is_duplicate_error, get_error, clean, check_connection

JSON_FILE = "output/master_godowns.json"

# Fetch company name and abbreviation (for warehouse naming)
def get_company_info():

    r = requests.get(
        f'{BASE_URL}/api/resource/Company?fields=["name","abbr"]&limit=5',
        headers=HEADERS
    )
    if r.status_code == 200:
        companies = r.json().get("data", [])
        if companies:
            company = companies[0]
            name = company.get("name", "Venkateshwara Traders")
            abbr = company.get("abbr", "VT")
            print(f"  → Company: {name}  |  Abbr: {abbr}\n")
            return name, abbr
    print(" Could not fetch company info, using defaults")
    return "Venkateshwara Traders", "VT"

# ─────────────────────────────────────────────────────────────────────────────
# Load Godowns
def load_godowns():
    print(f"Reading {JSON_FILE}...")
    with open(JSON_FILE, encoding="utf-8") as f:
        data = json.load(f)
    godowns = data["ENVELOPE"]["BODY"]["DATA"]["COLLECTION"]["GODOWN"]
    print(f"Found {len(godowns)} godowns\n")
    return godowns

# ─────────────────────────────────────────────────────────────────────────────
# Create Warehouse in ERPNext
def create_warehouse(warehouse_name, parent_warehouse, company_name):

    payload = {
        "doctype"          : "Warehouse",
        "warehouse_name"   : warehouse_name,
        "parent_warehouse" : parent_warehouse,
        "company"          : company_name,
        "is_group"         : 0,
    }
    r = requests.post(f"{BASE_URL}/api/resource/Warehouse",
                      headers=HEADERS, json=payload)
    return r.status_code, r.json()

# ─────────────────────────────────────────────────────────────────────────────

def migrate():
    if not check_connection():
        return

    print("Fetching company abbreviation...")
    company_name, abbr = get_company_info()

    godowns = load_godowns()

    top_level = [] 
    children  = [] 

    # Build a set of all godown names for lookup
    all_names = {clean(g.get("NAME")) for g in godowns}

    for g in godowns:
        name   = clean(g.get("NAME"))
        parent = clean(g.get("PARENT", ""))
        # If parent is a real godown name → child; else → top-level
        if parent and not parent.startswith("\x04") and parent in all_names:
            children.append(g)
        else:
            top_level.append(g)

    ordered = top_level + children   # parents always before children

    print(f"Creating {len(ordered)} warehouses (parents first)...")
    results = {"ok": [], "fail": [], "skipped": []}

    for g in ordered:
        name   = clean(g.get("NAME"))
        parent = clean(g.get("PARENT", ""))

        if not name:
            continue

        # Determine parent warehouse in ERPNext
        if parent and not parent.startswith("\x04") and parent in all_names:
            # Child warehouse → parent is another godown (with company suffix)
            erp_parent = f"{parent} - {abbr}"
        else:
            # Top-level → attach to ERPNext root
            erp_parent = f"All Warehouses - {abbr}"

        status, resp = create_warehouse(name, erp_parent, company_name)

        if status == 200:
            created_name = resp.get("data", {}).get("name", f"{name} - {abbr}")
            print(f"  Warehouse: {created_name}")
            results["ok"].append(name)
        else:
            error = get_error(resp)
            if is_duplicate_error(error):
                print(f"  Already exists: {name} - {abbr}")
                results["skipped"].append(name)
            else:
                print(f"  FAILED: {name}")
                print(f"      Error: {error[:100]}")
                results["fail"].append(name)

    # ── Summary ───────────────────────────────────────────────────────────────
    print()
    print("=" * 50)
    print("  MIGRATION SUMMARY")
    print("=" * 50)
    print(f"  Warehouses created   : {len(results['ok'])}")
    print(f"  Already existed      : {len(results['skipped'])}")
    print(f"  Failed               : {len(results['fail'])}")

    if results["fail"]:
        print("\n  Failed warehouses:")
        for n in results["fail"]:
            print(f"    - {n}")
    else:
        total = len(results["ok"]) + len(results["skipped"])
        print(f"\n  All {total} warehouses are now in ERPNext!")

# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    migrate()
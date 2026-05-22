"""
Step 2: Migrate Ledgers → Customers & Suppliers 
"""
import json
import requests

from common.config import BASE_URL, API_KEY, API_SECRET, load_env
from helpers.constants import UOM_MAP, HEADERS
from helpers.utils import parse_rate, is_duplicate_error, get_error, clean, check_connection

JSON_FILE = "output/master_stock_items.json"



def load_items():
    print(f"\nReading {JSON_FILE}...")
    with open(JSON_FILE, encoding="utf-8") as f:
        data = json.load(f)
    items = data["ENVELOPE"]["BODY"]["DATA"]["COLLECTION"]["STOCKITEM"]
    print(f"Found {len(items)} stock items\n")
    return items

 #─────────────────────────────────────────────────────────────────────────────
# CREATE ITEM GROUPS
def create_item_group(group_name):
    payload = {
        "doctype"           : "Item Group",
        "item_group_name"   : group_name,
        "parent_item_group" : "All Item Groups",
        "is_group"          : 0,             
    }
    r = requests.post(
        f"{BASE_URL}/api/resource/Item Group",
        headers=HEADERS, json=payload
    )
    return r.status_code, r.json()
 
 
def create_all_item_groups(items):
    """Collect unique PARENT values and create them as Item Groups."""
    # Collect unique group names (skip blank and Tally internal groups)
    groups = set()
    for item in items:
        parent = clean(item.get("PARENT", ""))
        if parent and not parent.startswith("\x04"):
            groups.add(parent)
 
    print(f"Creating {len(groups)} Item Groups first...")
    ok = 0
    skipped = 0
 
    for group in sorted(groups):
        status, resp = create_item_group(group)
 
        if status == 200:
            print(f"Group: {group}")
            ok += 1
        else:
            error = get_error(resp)
            if is_duplicate_error(error):
                print(f"Group already exists: {group}")
                skipped += 1
            else:
                print(f"Group FAILED: {group}  →  {error[:80]}")
 
    print(f"\n  Groups created: {ok}  |  Already existed: {skipped}\n")
    return ok + skipped  # total groups available
 
 
# ─────────────────────────────────────────────────────────────────────────────
# CREATE ITEMS
def build_item_payload(item):

    name   = clean(item.get("NAME"))
    parent = clean(item.get("PARENT", ""))
    uom    = clean(item.get("BASEUNITS", "Pcs"))
    rate   = parse_rate(item.get("OPENINGRATE", "0"))
 
    # Map Tally group → ERPNext item_group
    # Items under Tally's internal '\x04 Primary' have no category
    if not parent or parent.startswith("\x04"):
        item_group = "All Item Groups"
    else:
        item_group = parent
 
    # Map Tally UOM → ERPNext UOM
    erp_uom = UOM_MAP.get(uom, "Nos")
 
    payload = {
        "doctype"        : "Item",
        "item_code"      : name,     # item_code = item_name (no separate SKU in Tally)
        "item_name"      : name,
        "item_group"     : item_group,
        "stock_uom"      : erp_uom,
        "is_stock_item"  : 1,        # YES — track this in inventory
        "description"    : name,     # use name as description
    }
 
    # Add valuation rate only if we have a non-zero rate
    # (will be used as the default cost price)
    if rate > 0:
        payload["valuation_rate"] = rate
 
    return payload
 
 
def create_item(item):
    """Create a single Item in ERPNext."""
    payload = build_item_payload(item)
    r = requests.post(
        f"{BASE_URL}/api/resource/Item",
        headers=HEADERS, json=payload
    )
    return r.status_code, r.json(), payload["item_name"]
 
 
def migrate():
    if not check_connection():
        return
 
    # Load all items from Tally JSON
    items = load_items()
 
    # Step1: Create all item groups first
    create_all_item_groups(items)
 
    # Step2: Create all items
    print(f"Creating {len(items)} Items...")
    results = {"ok": [], "fail": [], "skipped": []}
 
    for item in items:
        status, resp, name = create_item(item)
 
        if status == 200:
            print(f"Item: {name}")
            results["ok"].append(name)
        else:
            error = get_error(resp)
            if is_duplicate_error(error):
                print(f"Already exists: {name}")
                results["skipped"].append(name)
            else:
                print(f"FAILED: {name}")
                print(f"Error: {error[:100]}")
                results["fail"].append(name)
 
    # ── Summary ────────────────────────────────────────────────────────────
    print()
    print("=" * 50)
    print("  MIGRATION SUMMARY")
    print("=" * 50)
    print(f"  Items created        : {len(results['ok'])}")
    print(f"  Already existed      : {len(results['skipped'])}")
    print(f"  Failed               : {len(results['fail'])}")
 
    if results["fail"]:
        print("\n  Failed items:")
        for n in results["fail"]:
            print(f"    - {n}")
    else:
        total = len(results["ok"]) + len(results["skipped"])
 
# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    migrate()
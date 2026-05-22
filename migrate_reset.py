"""
ERPNext Migration Reset Script
Deletes all data created by the migration scripts so you can start fresh.

Deletion order (reverse of creation, respecting dependencies):
  1. Sales Invoices       → cancel then delete
  2. Stock Reconciliation → cancel then delete
  3. Journal Entries      → cancel then delete
  4. Customers            → delete
  5. Suppliers            → delete
  6. Items                → delete
  7. Item Groups          → delete (leaf groups only)
  8. Warehouses           → optional (--include-warehouses flag)

Usage:
  python3 reset_erpnext.py                      # reset everything (keeps warehouses)
  python3 reset_erpnext.py --include-warehouses # also delete warehouses
  python3 reset_erpnext.py --dry-run            # show what WOULD be deleted
  python3 reset_erpnext.py --only customers     # reset one doctype only
"""

import argparse
import requests

from common.config import BASE_URL
from helpers.constants import HEADERS
from helpers.utils import get_error, check_connection

# ─────────────────────────────────────────────────────────────────────────────
# ARGS
# ─────────────────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser(
    description="Reset ERPNext — delete all migration data to start fresh"
)
parser.add_argument(
    "--dry-run", action="store_true",
    help="Show what would be deleted without actually deleting anything"
)
parser.add_argument(
    "--include-warehouses", action="store_true",
    help="Also delete warehouses created during migration"
)
parser.add_argument(
    "--only", default=None,
    metavar="DOCTYPE",
    help="Delete only this doctype. E.g: --only Customer"
)
args = parser.parse_args()

# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def fetch_all(doctype, filters=None, fields=None, limit=500):
    """Fetch all records of a doctype. Returns list of dicts."""
    params = f"?limit={limit}"
    if fields:
        params += f'&fields={fields}'
    if filters:
        params += f'&filters={filters}'

    r = requests.get(
        f"{BASE_URL}/api/resource/{doctype}{params}",
        headers=HEADERS,
    )
    if r.status_code == 200:
        return r.json().get("data", [])
    return []


def cancel_doc(doctype, name):
    """
    Cancel a submitted document using Frappe's cancel method.
    Uses /api/method/frappe.client.cancel which is the correct ERPNext approach.
    Falls back to PUT docstatus=2 if method call fails.
    """
    # Primary: use frappe client cancel method
    r = requests.post(
        f"{BASE_URL}/api/method/frappe.client.cancel",
        headers=HEADERS,
        json={"doctype": doctype, "name": name},
    )
    if r.status_code == 200:
        return True

    # Fallback: PUT docstatus=2
    r2 = requests.put(
        f"{BASE_URL}/api/resource/{doctype}/{requests.utils.quote(name)}",
        headers=HEADERS,
        json={"docstatus": 2},
    )
    return r2.status_code == 200


def verify_cancelled(doctype, name):
    """Return True if the document is in cancelled state (docstatus=2)."""
    r = requests.get(
        f"{BASE_URL}/api/resource/{doctype}/{requests.utils.quote(name)}",
        headers=HEADERS,
    )
    if r.status_code == 200:
        return r.json().get("data", {}).get("docstatus") == 2
    return False


def delete_doc(doctype, name):
    """Delete a document by name."""
    r = requests.delete(
        f"{BASE_URL}/api/resource/{doctype}/{requests.utils.quote(name)}",
        headers=HEADERS,
    )
    return r.status_code == 202


def cancel_and_delete(doctype, records, dry_run):
    """
    Cancel then delete submitted documents.
    For draft documents, delete directly.
    Returns (deleted_count, failed_count).
    """
    if not records:
        print(f"  No {doctype} records found — skipping")
        return 0, 0

    print(f"  Found {len(records)} {doctype} record(s)")
    deleted = 0
    failed  = 0

    for rec in records:
        name      = rec.get("name", "")
        docstatus = rec.get("docstatus", 0)

        if dry_run:
            status_label = {0: "Draft", 1: "Submitted", 2: "Cancelled"}.get(docstatus, "?")
            print(f"    [DRY RUN] Would delete: {name}  ({status_label})")
            deleted += 1
            continue

        # Step 1: Cancel if submitted
        if docstatus == 1:
            cancel_doc(doctype, name)
            # Verify cancellation succeeded before attempting delete
            if not verify_cancelled(doctype, name):
                print(f"    ✗ Could not cancel: {name}")
                failed += 1
                continue

        # Step 2: Delete
        ok = delete_doc(doctype, name)
        if ok:
            print(f"    ✓ Deleted: {name}")
            deleted += 1
        else:
            # Get full error for diagnosis
            r = requests.delete(
                f"{BASE_URL}/api/resource/{doctype}/{requests.utils.quote(name)}",
                headers=HEADERS,
            )
            error = get_error(r.json()) if r.content else "Unknown error"
            print(f"    ✗ Could not delete: {name}  ({error[:80]})")
            failed += 1

    return deleted, failed


def delete_masters(doctype, records, dry_run):
    """
    Delete master records (Customer, Supplier, Item) directly.
    These don't need cancellation — but may fail if linked to transactions.
    """
    if not records:
        print(f"  No {doctype} records found — skipping")
        return 0, 0

    print(f"  Found {len(records)} {doctype} record(s)")
    deleted = 0
    failed  = 0

    for rec in records:
        name = rec.get("name", "")

        if dry_run:
            print(f"    [DRY RUN] Would delete: {name}")
            deleted += 1
            continue

        ok = delete_doc(doctype, name)
        if ok:
            print(f"    ✓ Deleted: {name}")
            deleted += 1
        else:
            # Try to get a more helpful error
            r = requests.delete(
                f"{BASE_URL}/api/resource/{doctype}/{requests.utils.quote(name)}",
                headers=HEADERS,
            )
            error = get_error(r.json()) if r.content else "Unknown error"
            print(f"    ✗ Could not delete: {name}  ({error[:80]})")
            failed += 1

    return deleted, failed

# ─────────────────────────────────────────────────────────────────────────────
# RESET FUNCTIONS — one per doctype
# ─────────────────────────────────────────────────────────────────────────────
def reset_sales_invoices(dry_run):
    print("\n── Sales Invoices ──────────────────────────────────")
    records = fetch_all(
        "Sales Invoice",
        fields='["name","docstatus","customer","posting_date"]',
    )
    return cancel_and_delete("Sales Invoice", records, dry_run)


def reset_stock_reconciliation(dry_run):
    print("\n── Stock Reconciliation ────────────────────────────")
    records = fetch_all(
        "Stock Reconciliation",
        fields='["name","docstatus","purpose"]',
    )
    # Cancel and delete must succeed before items/warehouses can be deleted
    deleted, failed = cancel_and_delete("Stock Reconciliation", records, dry_run)
    if failed > 0 and not dry_run:
        print("  ⚠ Stock Reconciliation cancel/delete failed.")
        print("    Items and Warehouses cannot be deleted until this is resolved.")
        print("    Try: ERPNext → Stock → Stock Reconciliation → cancel manually")
    return deleted, failed


def reset_journal_entries(dry_run):
    print("\n── Journal Entries ─────────────────────────────────")
    # Only delete Opening Entries we created — not system entries
    records = fetch_all(
        "Journal Entry",
        filters='[["voucher_type","=","Opening Entry"]]',
        fields='["name","docstatus","voucher_type","posting_date"]',
    )
    return cancel_and_delete("Journal Entry", records, dry_run)


def reset_customers(dry_run):
    print("\n── Customers ───────────────────────────────────────")
    records = fetch_all("Customer", fields='["name"]')
    return delete_masters("Customer", records, dry_run)


def reset_suppliers(dry_run):
    print("\n── Suppliers ───────────────────────────────────────")
    records = fetch_all("Supplier", fields='["name"]')
    return delete_masters("Supplier", records, dry_run)


def disable_item(name):
    """
    Disable an Item instead of deleting when it has stock/transaction links.
    Sets disabled=1 which hides it from all transactions.
    """
    r = requests.put(
        f"{BASE_URL}/api/resource/Item/{requests.utils.quote(name)}",
        headers=HEADERS,
        json={"disabled": 1},
    )
    return r.status_code == 200


def reset_items(dry_run):
    print("\n── Items ───────────────────────────────────────────")
    records = fetch_all("Item", fields='["name"]')
    if not records:
        print("  No Item records found — skipping")
        return 0, 0

    print(f"  Found {len(records)} Item record(s)")
    deleted = 0; failed = 0

    for rec in records:
        name = rec.get("name", "")
        if dry_run:
            print(f"    [DRY RUN] Would delete/disable: {name}")
            deleted += 1
            continue

        # Try delete first
        if delete_doc("Item", name):
            print(f"    ✓ Deleted: {name}")
            deleted += 1
        else:
            # Item has stock/transaction links — disable it instead
            if disable_item(name):
                print(f"    ~ Disabled (has links): {name}")
                deleted += 1   # count as success — item is gone from view
            else:
                print(f"    ✗ Could not delete or disable: {name}")
                failed += 1

    return deleted, failed


def reset_item_groups(dry_run):
    print("\n── Item Groups (leaf only) ─────────────────────────")
    # Only delete leaf groups (is_group=0) — never delete root 'All Item Groups'
    all_groups = fetch_all(
        "Item Group",
        fields='["name","is_group","parent_item_group"]',
    )
    # Filter to only leaf groups that we created (not ERPNext system groups)
    system_groups = {
        "All Item Groups", "Products", "Sub Assemblies",
        "Raw Material", "Services", "Consumable"
    }
    leaf_groups = [
        g for g in all_groups
        if not g.get("is_group")
        and g.get("name") not in system_groups
    ]
    return delete_masters("Item Group", leaf_groups, dry_run)


def reset_warehouses(dry_run):
    print("\n── Warehouses ──────────────────────────────────────")
    all_wh = fetch_all(
        "Warehouse",
        fields='["name","is_group","parent_warehouse"]',
    )
    # Only delete leaf warehouses we created — not 'All Warehouses'
    leaf_wh = [w for w in all_wh if not w.get("is_group")]
    return delete_masters("Warehouse", leaf_wh, dry_run)

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
def reset():
    if not args.dry_run and not check_connection():
        return

    if args.dry_run:
        print("  Mode: DRY RUN — nothing will be deleted\n")

    # If --only is specified, run just that one
    if args.only:
        doctype = args.only.strip()
        fn_map = {
            "Sales Invoice"       : reset_sales_invoices,
            "Stock Reconciliation": reset_stock_reconciliation,
            "Journal Entry"       : reset_journal_entries,
            "Customer"            : reset_customers,
            "Supplier"            : reset_suppliers,
            "Item"                : reset_items,
            "Item Group"          : reset_item_groups,
            "Warehouse"           : reset_warehouses,
        }
        fn = fn_map.get(doctype)
        if not fn:
            print(f"Unknown doctype: {doctype!r}")
            print(f"Valid options: {', '.join(fn_map.keys())}")
            return
        d, f = fn(args.dry_run)
        print(f"\n  Deleted: {d}  Failed: {f}")
        return

    # Full reset — in dependency order
    totals = {}

    # Transactions first (reverse of creation order)
    totals["Sales Invoice"]        = reset_sales_invoices(args.dry_run)
    totals["Stock Reconciliation"] = reset_stock_reconciliation(args.dry_run)
    totals["Journal Entry"]        = reset_journal_entries(args.dry_run)

    # Masters after transactions are gone
    totals["Customer"]             = reset_customers(args.dry_run)
    totals["Supplier"]             = reset_suppliers(args.dry_run)
    totals["Item"]                 = reset_items(args.dry_run)
    totals["Item Group"]           = reset_item_groups(args.dry_run)

    # Warehouses only if explicitly requested
    if args.include_warehouses:
        totals["Warehouse"]        = reset_warehouses(args.dry_run)

    # ── Summary ───────────────────────────────────────────────────────────────
    print()
    print("=" * 52)
    print("  RESET SUMMARY")
    print("=" * 52)

    grand_deleted = 0
    grand_failed  = 0

    for doctype, (deleted, failed) in totals.items():
        status = "DRY RUN" if args.dry_run else ("✓" if failed == 0 else "⚠")
        print(f"  {status}  {doctype:25s}  deleted={deleted}  failed={failed}")
        grand_deleted += deleted
        grand_failed  += failed

    print()
    print(f"  Total deleted : {grand_deleted}")
    print(f"  Total failed  : {grand_failed}")

    if not args.dry_run:
        if grand_failed == 0:
            print()
            print("  ✓ ERPNext is clean — ready to restart migration from Step 1")
            print()
            print("  Run in order:")
            print("    python3 migrate_ledgers.py")
            print("    python3 migrate_items.py")
            print("    python3 migrate_warehouses.py")
            print("    python3 migrate_balances.py")
            print("    python3 migrate_sales.py")
        else:
            print()
            print("  Some records could not be deleted.")
            print("  They may still be linked to other documents.")
            print("  Re-run this script — it is safe to run multiple times.")


if __name__ == "__main__":
    print()
    print("╔══════════════════════════════════════════════════╗")
    print("║  ERPNext Migration Reset                        ║")
    print("║  Venkateshwara Traders                          ║")
    print("╚══════════════════════════════════════════════════╝")
    print()

    if not args.dry_run:
        print("  ⚠  WARNING: This will DELETE all migration data from ERPNext.")
        print("     This cannot be undone.")
        print()
        confirm = input("  Type YES to confirm: ").strip()
        if confirm != "YES":
            print("  Cancelled.")
            exit(0)
        print()

    reset()
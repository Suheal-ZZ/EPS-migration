from __future__ import annotations

SCRIPT_REGISTRY = {
    "migrate_ledgers": "migrate_ledgers.py",
    "migrate_items": "migrate_items.py",
    "migrate_warehouses": "migrate_warehouses.py",
    "migrate_balances": "migrate_balances.py",
    "migrate_sales": "migrate_sales.py",
    "migrate_purchases": "migrate_purchases.py",
    "migrate_payments": "migrate_payments.py",
    "migrate_reset": "migrate_reset.py",
    "validate_ledgers": "validate_ledgers.py",
}

ALLOWED_ARGS = {
    "month": str,
    "dry_run": bool,
    "type": str,
    "warehouse": str,
    "list_warehouses": bool,
    "include_warehouses": bool,
    "only": str,
}
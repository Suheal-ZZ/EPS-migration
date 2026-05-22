from common.config import API_KEY, API_SECRET, BASE_URL

# ─────────────────────────────────────────────────────────────────────────────
# API request headers
# ─────────────────────────────────────────────────────────────────────────────
HEADERS = {
    "Authorization": f"token {API_KEY}:{API_SECRET}",
    "Content-Type": "application/json",
}

# ─────────────────────────────────────────────────────────────────────────────
# UOM mapping — Tally → ERPNext
# ─────────────────────────────────────────────────────────────────────────────
UOM_MAP = {
    "Pcs" : "Nos",
    "Nos" : "Nos",
    "Kg"  : "Kg",
    "Kgs" : "Kg",
    "Gm"  : "Gram",
    "Gms" : "Gram",
    "Ltr" : "Liter",
    "Lts" : "Liter",
    "Box" : "Box",
    "Pkt" : "Nos",
    "Pk"  : "Nos",
    "Dz"  : "Dozen",
    ""    : "Nos",
}

# ─────────────────────────────────────────────────────────────────────────────
# Ledger groups to skip in opening balance entry.
# Customers/Suppliers use party balances, not account balances.
# ─────────────────────────────────────────────────────────────────────────────
SKIP_LEDGER_GROUPS = [
    "Sundry Debtors",
    "Sundry Creditors",
    "\x04 Primary",
    "",
]

# ─────────────────────────────────────────────────────────────────────────────
# P&L groups to EXCLUDE from Opening Entry journal voucher.
#
# ERPNext Opening Entry only accepts Balance Sheet accounts
# (Assets, Liabilities, Equity). Income and Expense accounts are P&L
# and cannot be in an Opening Entry — ERPNext throws:
#   "Profit and Loss type account not allowed in Opening Entry"
#
# These opening balances (Sales, Purchase, Expenses) represent prior-year
# activity already captured in the Profit & Loss A/c balance. They are
# NOT entered separately in the opening journal — only the net P&L result
# flows into retained earnings via the Balance Sheet.
# ─────────────────────────────────────────────────────────────────────────────
SKIP_PL_GROUPS = [
    "Sales Accounts",
    "Purchase Accounts",
    "Direct Expenses",
    "Indirect Expenses",
    "Expenses on Purchase",
    "Indirect Incomes",
]

# ─────────────────────────────────────────────────────────────────────────────
# GROUP_MAP — Tally PARENT → ERPNext account attributes
#
# Format per entry:
#   "Tally PARENT": (account_type, root_type, [candidate parent names])
#
# account_type:
#   Use "" (blank) for liability/loan accounts that do NOT need a party.
#   ERPNext "Payable" and "Receivable" types REQUIRE a party on every
#   journal line — using them for loans/provisions causes ValidationError.
#   Only use "Payable"/"Receivable" for actual party accounts (Suppliers/Customers).
#
# parent candidates:
#   List of possible ERPNext parent account base names in priority order.
#   The script tries each with and without the company suffix until one exists.
#   This handles ERPNext India CoA name variations across versions.
# ─────────────────────────────────────────────────────────────────────────────
GROUP_MAP = {
    # ── Assets ───────────────────────────────────────────────────────────────
    "Bank Accounts"           : ("Bank",           "Asset",     ["Bank Accounts"]),
    "Cash-in-Hand"            : ("Cash",           "Asset",     ["Cash In Hand"]),
    "Fixed Assets"            : ("Fixed Asset",    "Asset",     ["Fixed Assets"]),
    "Current Assets"          : ("Current Asset",  "Asset",     ["Current Assets"]),
    "Investments"             : ("Current Asset",  "Asset",     ["Investments"]),

    # Deposits — ERPNext India may call this 'Deposits' or 'Security Deposits'
    "Deposits (Asset)"        : ("Current Asset",  "Asset",
                                 ["Deposits", "Security Deposits", "Current Assets"]),

    # Loans given out (asset side)
    "Loans & Advances (Asset)": ("Current Asset",  "Asset",
                                 ["Loans and Advances (Assets)",
                                  "Loans & Advances (Assets)", "Current Assets"]),

    # ── Liabilities ───────────────────────────────────────────────────────────
    # account_type="" avoids the party requirement on journal lines
    "Bank OD A/c"             : ("Bank",           "Liability",
                                 ["Bank Overdraft Account", "Bank Overdraft",
                                  "Loans (Liabilities)", "Unsecured Loans",
                                  "Current Liabilities"]),

    "Loans (Liability)"       : ("",              "Liability",
                                 ["Unsecured Loans", "Loans (Liabilities)",
                                  "Long Term Loans", "Current Liabilities"]),

    "Secured Loans"           : ("",              "Liability",
                                 ["Secured Loans", "Loans (Liabilities)",
                                  "Long Term Loans", "Current Liabilities"]),

    "Duties & Taxes"          : ("Tax",            "Liability",
                                 ["Duties and Taxes"]),

    "Provisions"              : ("",              "Liability",
                                 ["Provisions", "Current Liabilities"]),

    # ── Equity ────────────────────────────────────────────────────────────────
    "Capital Account"         : ("Equity",         "Equity",
                                 ["Capital Accounts", "Capital Account"]),

    # ── Income ────────────────────────────────────────────────────────────────
    "Sales Accounts"          : ("Income Account", "Income",    ["Direct Income"]),
    "Indirect Incomes"        : ("Income Account", "Income",    ["Indirect Income"]),

    # ── Expense ───────────────────────────────────────────────────────────────
    "Purchase Accounts"       : ("Expense Account","Expense",   ["Direct Expenses"]),
    "Expenses on Purchase"    : ("Expense Account","Expense",   ["Direct Expenses"]),
    "Direct Expenses"         : ("Expense Account","Expense",   ["Direct Expenses"]),
    "Indirect Expenses"       : ("Expense Account","Expense",   ["Indirect Expenses"]),
}

# ─────────────────────────────────────────────────────────────────────────────
# Stock Reconciliation difference account.
# ERPNext requires this to be an Asset or Expense type account.
# "Temporary Opening" is created automatically by ERPNext for this purpose.
# ─────────────────────────────────────────────────────────────────────────────
STOCK_DIFFERENCE_ACCOUNT_BASE = "Temporary Opening"
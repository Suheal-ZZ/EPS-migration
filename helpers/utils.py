import requests

from common.config import BASE_URL
from helpers.constants import HEADERS


def clean(val):
    """
    Normalize Tally JSON field values.
    Tally fields can be:
      - plain string  : "Alfa Provisions"
      - dict with '_' : {"_": "500 Pcs", "TYPE": "Quantity"}
      - None / missing
    Always returns a clean plain string.
    """
    if val is None:
        return ""
    if isinstance(val, dict):
        val = val.get("_", "")
    return str(val).strip().replace("\r", "").replace("\n", "")


def parse_rate(val):
    """
    Tally stores rate as '57.00/Pcs'. Extract just the number.
    Returns 0.0 if not parseable.
    """
    val = clean(val)
    if not val:
        return 0.0
    part = val.split("/")[0].strip()
    try:
        return float(part)
    except ValueError:
        return 0.0


def parse_qty(val):
    """
    Tally OPENINGBALANCE is a dict: {'_': '500 Pcs', 'TYPE': 'Quantity'}.
    clean() extracts the '_' value first, then we strip units and parse.
    Returns 0.0 if not parseable or zero.
    """
    text = clean(val).replace("Pcs", "").replace("Nos", "").strip()
    try:
        return abs(float(text))
    except ValueError:   # explicit — don't swallow KeyboardInterrupt etc.
        return 0.0


def parse_amount(val):
    """
    Parse a monetary string like '-4560.00' → 4560.0 (always positive).
    Sign is handled separately by the caller.
    """
    try:
        return abs(float(clean(val)))
    except ValueError:
        return 0.0


def get_error(resp):
    """
    Extract a readable error message from an ERPNext JSON response.
    Truncates at 200 chars to keep logs clean.
    """
    if isinstance(resp, dict):
        return str(resp.get("exception", resp.get("message", str(resp))))[:200]
    return str(resp)[:200]


def is_duplicate_error(error_str):
    """Return True if the ERPNext error means the record already exists."""
    e = error_str.lower()
    return "already exists" in e or "duplicateentryerror" in e


def check_connection():
    """Ping ERPNext API. Returns True if reachable and authenticated."""
    print("Checking ERPNext connection...")
    try:
        r = requests.get(
            f"{BASE_URL}/api/resource/Item?limit=1",
            headers=HEADERS,
            timeout=10,
        )
        if r.status_code == 200:
            print("✓ Connected to ERPNext successfully")
            return True
        if r.status_code == 401:
            print("✗ Auth failed — check ERP_API_KEY and ERP_API_SECRET in .env")
        else:
            print(f"✗ Unexpected status: {r.status_code}")
        return False
    except requests.exceptions.ConnectionError:
        print(f"✗ Cannot connect to {BASE_URL} — is ERPNext Docker running?")
        return False
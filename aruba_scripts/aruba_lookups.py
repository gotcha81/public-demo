"""
Read-only lookups in Aruba Central (Classic). Nothing in this file changes anything.

The API paths and field names are collected at the top, so they are easy to check
against the Aruba Central API reference if Aruba changes something.
"""

from urllib.parse import quote

# MSP portal APIs (used with the MSP's own login)
INVENTORY_PATH = "/platform/device_inventory/v1/devices"
CUSTOMERS_PATH = "/msp_api/v1/customers"
SUBSCRIPTIONS_PATH = "/platform/licensing/v1/subscriptions"

# Tenant APIs (used with the tenant's login)
SITES_PATH = "/central/v2/sites"
GROUP_TEMPLATES_PATH = "/configuration/v1/groups/{group}/templates"

# Field in a subscription that holds the service to assign, e.g. "foundation_ap".
# If the first dry run says "has no license_type", look at the subscription in the
# DEBUG lines of the log file and change this to the right field name.
SUBSCRIPTION_SERVICE_FIELD = "license_type"


def get_customers(msp):
    """Return ({tenant_name: customer_id}, msp_id) for every tenant in the MSP portal."""
    customers = msp.get_all(CUSTOMERS_PATH, "customers", page_size=50)
    by_name = {c["customer_name"]: c["customer_id"] for c in customers}
    msp_id = next((c["msp_id"] for c in customers if c.get("msp_id")), None)
    return by_name, msp_id


def find_customer_id(customers_by_name, tenant):
    """The tenant column may hold the tenant's name or its customer ID."""
    if tenant in customers_by_name:
        return customers_by_name[tenant]
    if tenant in customers_by_name.values():
        return tenant
    return None


def get_inventory(msp):
    """Return {serial: device} for every device in the MSP portal's inventory."""
    devices = msp.get_all(INVENTORY_PATH, "devices", page_size=50, params={"sku_type": "all"})
    inventory = {d["serial"].upper(): d for d in devices if d.get("serial")}
    return inventory


def find_subscription(msp, cache, code):
    """Return the subscription with this code, or None. Each code is only looked up once."""
    if code not in cache:
        answer = msp.request("GET", SUBSCRIPTIONS_PATH, params={"subscription_key": code, "limit": 50})
        matches = [s for s in answer.get("subscriptions") or []
                   if str(s.get("subscription_key", "")).upper() == code.upper()]
        cache[code] = matches[0] if matches else None
    return cache[code]


def get_sites(tenant_connection):
    """Return {site_name: site_id} for every site in the tenant."""
    sites = tenant_connection.get_all(SITES_PATH, "sites", page_size=1000)
    by_name = {s["site_name"]: s["site_id"] for s in sites}
    return by_name


def get_group_templates(tenant_connection, group):
    """Return the template names in a template group. Raises ArubaAPIError if the group doesn't exist."""
    path = GROUP_TEMPLATES_PATH.format(group=quote(group, safe=""))
    templates = tenant_connection.get_all(path, "data", page_size=20)
    names = [t["name"] for t in templates]
    return names

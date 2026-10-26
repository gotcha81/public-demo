#!/usr/bin/env python3
"""
Claim Aruba devices from a CSV into Classic Aruba Central (MSP mode) and set them up:

    1. add the device to the MSP portal's inventory (serial + MAC)
    2. assign it to the tenant, in the group
    3. assign the license from the subscription code
    4. put it in the site
    (the template comes from the group automatically, so it is only checked)

CSV columns (header row required):
    serial number, mac address, subscription code, msp portal, tenant   <- mandatory
    site, group, template                                               <- optional
An empty site, group or template gets the placeholder in DEFAULTS below.
See aruba_scripts/examples/aruba_devices_example.csv.

Every row is checked first (file AND Aruba Central) before anything is changed.
A bad row is skipped and reported; the other rows still run.

Nothing is changed unless you add --apply (it's a dry run by default).

Usage:
    python aruba_scripts/claim_aruba_devices.py devices.csv            # dry run: check + show plan
    python aruba_scripts/claim_aruba_devices.py devices.csv --apply    # really do it

Credentials: aruba_msp_portals in resource_root/credentials/my_creds.py,
see resource_root/shared/aruba_central.py for the format. Per account, either
    "login": "api"       API token, never asks anything (use this for unattended runs)
    "login": "password"  username + password; the script asks for the Aruba Central OTP
                         in step 2 when it has no working saved token

How to debug it:
    1. Check DEBUG_ARGS and DEFAULTS below
    2. Put a breakpoint on the first line inside run() (step 1),
       or inside check_row() to follow a single device
    3. Right-click this file -> Debug, or use the "claim_aruba_devices (debug, dry-run)" run config
    4. F8 = run the line, F7 = go into one of the functions below
"""

import argparse
import logging
import sys
from pathlib import Path

from aruba_device_csv import normalise_mac, read_device_rows
from aruba_lookups import (
    INVENTORY_PATH,
    SUBSCRIPTION_SERVICE_FIELD,
    find_customer_id,
    find_subscription,
    get_customers,
    get_group_templates,
    get_inventory,
    get_sites,
)
from resource_root.shared.aruba_central import ArubaAPIError, connect, get_portal_settings
from resource_root.shared.meraki_helpers import ScriptStopped, run_script, setup_logging

# --------------------------------------------------------------------------
# Settings - change these
# --------------------------------------------------------------------------

# Arguments used when the script is started without any (e.g. Debug in PyCharm).
# Without --apply it's always a dry run.
DEBUG_ARGS = [str(Path(__file__).parent / "examples" / "aruba_devices_example.csv")]

# Used when site, group or template is empty in the CSV. They must already exist in the tenant.
# Set a value to "" to skip it (e.g. "template": "" = don't check any template).
DEFAULTS = {
    "site": "PLACEHOLDER-SITE",
    "group": "PLACEHOLDER-GROUP",
    "template": "PLACEHOLDER-TEMPLATE",
}

# Inventory device type -> device type the site API wants.
# Aruba switches are "CX" or "ArubaSwitch" (older AOS-S), change "switch" if you use AOS-S.
SITE_DEVICE_TYPES = {
    "iap": "IAP",
    "ap": "IAP",
    "switch": "CX",
    "gateway": "MobilityController",
    "controller": "MobilityController",
}

# API calls that change something (the read-only ones are in aruba_lookups.py)
TENANT_DEVICES_PATH = "/msp_api/v1/customers/{customer_id}/devices"   # MSP login
ASSIGN_LICENSE_PATH = "/platform/licensing/v1/subscriptions/assign"   # MSP login
MOVE_TO_GROUP_PATH = "/configuration/v1/devices/move"                 # tenant login
SITE_ASSOCIATIONS_PATH = "/central/v2/sites/associations"             # tenant login

LOG_DIR = Path(__file__).parent / "logs"

# Same name as in setup_logging() in main(), so the functions below write to that log
log = logging.getLogger("claim_aruba_devices")


class RowFailed(Exception):
    """One device can't continue. The message explains why."""


def parse_arguments():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("csv_file", help="CSV with the devices")
    parser.add_argument("--apply", action="store_true", help="Really make the changes (default: dry run)")
    parser.add_argument("--log-file", help="Log file path (default: aruba_scripts/logs/claim_aruba_devices_<time>.log)")

    command_line = sys.argv[1:] or DEBUG_ARGS
    args = parser.parse_args(command_line)
    return args


# --------------------------------------------------------------------------
# Step 1-3: read the CSV, log in, read what's already in Aruba Central
# --------------------------------------------------------------------------
def load_rows(path):
    try:
        rows, bad_rows = read_device_rows(path, DEFAULTS, log)
    except (OSError, ValueError) as exc:
        raise ScriptStopped(f"Could not read CSV {path}: {exc}") from exc
    return rows, bad_rows


def tenant_settings_for(portal_settings, tenant_settings):
    """Login settings for a tenant.

    "password" login: the MSP user logs in to the tenant, so username, password, client_id and
    client_secret come from the MSP portal. The tenant only needs its own customer_id.
    "api" login: the tenant needs its own token, so nothing is copied (never the MSP's refresh_token).
    """
    if portal_settings.get("login") != "password":
        return tenant_settings
    shared = {key: portal_settings[key] for key in ("login", "username", "password", "client_id", "client_secret")
              if key in portal_settings}
    combined = dict(shared, **tenant_settings)  # the tenant's own values win
    return combined


def connect_portals(rows):
    """Log in to every MSP portal in the CSV, and to its tenants that have credentials.

    Returns {portal_name: portal}, where portal is a dict with the connections and (later) the data read.
    A portal that isn't in my_creds.py is left out; its rows fail in check_row() with a clear reason.
    """
    portal_settings = get_portal_settings()
    portals = {}

    for portal_name in sorted({row["msp_portal"] for row in rows}):
        settings = portal_settings.get(portal_name)
        if settings is None:
            log.warning("MSP portal '%s' is not in aruba_msp_portals in my_creds.py", portal_name)
            continue
        msp = connect(portal_name, settings)

        tenants = {}
        tenant_settings = settings.get("tenants") or {}
        tenant_names = sorted({row["tenant"] for row in rows if row["msp_portal"] == portal_name})
        for tenant_name in tenant_names:
            if tenant_name not in tenant_settings:
                log.warning("No API credentials for tenant '%s' under '%s' in my_creds.py", tenant_name, portal_name)
                continue
            one_tenant = tenant_settings_for(settings, tenant_settings[tenant_name])
            connection = connect(f"{portal_name} / {tenant_name}", one_tenant,
                                 base_url=one_tenant.get("base_url") or settings["base_url"])
            tenants[tenant_name] = {"connection": connection, "sites": None, "templates": {}}

        portals[portal_name] = {
            "msp": msp,
            "tenants": tenants,
            "customers": {},       # {tenant_name: customer_id}, filled in read_portal_data()
            "msp_id": None,        # the MSP's own customer ID
            "inventory": {},       # {serial: device}, filled in read_portal_data()
            "subscriptions": {},   # cache for find_subscription()
        }
    return portals


def read_portal_data(portals):
    """Read tenants and inventory from every MSP portal."""
    for portal_name, portal in portals.items():
        customers, msp_id = get_customers(portal["msp"])
        inventory = get_inventory(portal["msp"])
        portal["customers"] = customers
        portal["msp_id"] = msp_id
        portal["inventory"] = inventory
        log.info("        '%s': %d tenant(s), %d device(s) in inventory", portal_name, len(customers), len(inventory))


# --------------------------------------------------------------------------
# Step 4: check one row against Aruba Central (nothing is changed)
# --------------------------------------------------------------------------
def placeholder_hint(row, name):
    if name in row["defaults_used"]:
        return " (empty in the CSV, so the placeholder from DEFAULTS was used)"
    return ""


def check_row(row, portals):
    """Check everything for one device. Returns (plan, None), or (None, reason) if it can't be done."""
    serial = row["serial"]

    # MSP portal and tenant
    portal = portals.get(row["msp_portal"])
    if portal is None:
        return None, f"MSP portal '{row['msp_portal']}' is not in aruba_msp_portals in my_creds.py"
    customer_id = find_customer_id(portal["customers"], row["tenant"])
    if not customer_id:
        return None, f"tenant '{row['tenant']}' not found in MSP portal '{row['msp_portal']}'"
    tenant = portal["tenants"].get(row["tenant"])
    if tenant is None:
        return None, (f"no API credentials for tenant '{row['tenant']}' under '{row['msp_portal']}' "
                      f"in my_creds.py (needed for group, template and site)")

    # Is the device already in the inventory?
    device = portal["inventory"].get(serial)
    if device:
        known_mac = normalise_mac(device.get("macaddr") or "")
        if known_mac and known_mac != row["mac"]:
            return None, f"already in the inventory with another MAC ({known_mac}), check the CSV"
        current_customer = device.get("customer_id")
        if current_customer and current_customer not in (customer_id, portal["msp_id"]):
            return None, (f"already belongs to another tenant ('{device.get('customer_name')}'), "
                          f"move it back to the MSP in Central first")

    # License
    subscription = find_subscription(portal["msp"], portal["subscriptions"], row["subscription"])
    if not subscription:
        return None, f"subscription code '{row['subscription']}' not found in MSP portal '{row['msp_portal']}'"
    service = subscription.get(SUBSCRIPTION_SERVICE_FIELD)
    if not service:
        return None, (f"subscription '{row['subscription']}' has no '{SUBSCRIPTION_SERVICE_FIELD}' "
                      f"(check SUBSCRIPTION_SERVICE_FIELD in aruba_lookups.py)")

    # Site (sites are read once per tenant)
    site_id = None
    if row["site"]:
        if tenant["sites"] is None:
            tenant["sites"] = get_sites(tenant["connection"])
        site_id = tenant["sites"].get(row["site"])
        if site_id is None:
            return None, f"site '{row['site']}' not found in tenant '{row['tenant']}'{placeholder_hint(row, 'site')}"

    # Group + template (templates are read once per group)
    if row["template"]:
        group = row["group"]
        if group not in tenant["templates"]:
            try:
                tenant["templates"][group] = get_group_templates(tenant["connection"], group)
            except ArubaAPIError as exc:
                tenant["templates"][group] = exc  # remember the error, so the next row doesn't ask again
        templates = tenant["templates"][group]
        if isinstance(templates, ArubaAPIError):
            return None, (f"could not read templates in group '{group}'{placeholder_hint(row, 'group')}: "
                          f"{templates.message} (does the group exist, and is it a template group?)")
        if row["template"] not in templates:
            return None, (f"template '{row['template']}' not found in group '{group}'"
                          f"{placeholder_hint(row, 'template')}. Templates there: {templates}")

    services_now = []
    if device:
        services_now = device.get("services") or []
    plan = {
        "row": row,
        "msp": portal["msp"],
        "tenant_connection": tenant["connection"],
        "customer_id": customer_id,
        "service": service,
        "site_id": site_id,
        "device_type": device.get("device_type") if device else None,
        "needs_claim": device is None,
        "already_in_tenant": bool(device) and device.get("customer_id") == customer_id,
        "needs_license": service not in services_now,
    }
    return plan, None


def describe_plan(plan):
    row = plan["row"]
    parts = [
        "claim" if plan["needs_claim"] else "already in inventory",
        f"tenant '{row['tenant']}' group '{row['group']}'",
        f"license {plan['service']}" if plan["needs_license"] else "license already assigned",
        f"site '{row['site']}'" if row["site"] else "no site",
    ]
    if row["template"]:
        parts.append(f"template '{row['template']}' OK")
    if row["defaults_used"]:
        parts.append(f"placeholders used for {row['defaults_used']}")
    return "; ".join(parts)


# --------------------------------------------------------------------------
# Step 5-8: make the changes, one kind at a time
# --------------------------------------------------------------------------
def claim_device(plan, dry_run):
    """Add the device to the MSP portal's inventory."""
    row = plan["row"]
    if not plan["needs_claim"]:
        log.info("%s: already in the inventory, not claimed again", row["serial"])
        return
    if dry_run:
        log.info("[DRY-RUN] %s: would add to the inventory with MAC %s", row["serial"], row["mac"])
        return

    answer = plan["msp"].request("POST", INVENTORY_PATH, body=[{"serial": row["serial"], "mac": row["mac"]}])
    # HTTP 200 can still contain devices that were refused, so look in the answer
    refused = answer.get("extra", {}).get("message", {}) if isinstance(answer.get("extra"), dict) else {}
    for kind in ("blocked_device", "invalid_device"):
        for refused_device in refused.get(kind) or []:
            refused_serial = refused_device.get("serial") or refused_device.get("serial_number")
            if str(refused_serial).upper() == row["serial"]:
                raise RowFailed(f"Aruba refused it ({kind}): {refused_device.get('ccs_message', refused_device)}")
    log.info("%s: OK added to the inventory", row["serial"])


def assign_to_tenant(plan, dry_run):
    """Put the device in the tenant and group. A device already in the tenant is only moved to the group."""
    row = plan["row"]
    if plan["already_in_tenant"]:
        if dry_run:
            log.info("[DRY-RUN] %s: already in tenant '%s', would move to group '%s'",
                     row["serial"], row["tenant"], row["group"])
            return
        plan["tenant_connection"].request("POST", MOVE_TO_GROUP_PATH,
                                          body={"group": row["group"], "serials": [row["serial"]]})
        log.info("%s: OK moved to group '%s'", row["serial"], row["group"])
        return

    if dry_run:
        log.info("[DRY-RUN] %s: would assign to tenant '%s' in group '%s'", row["serial"], row["tenant"], row["group"])
        return
    path = TENANT_DEVICES_PATH.format(customer_id=plan["customer_id"])
    body = {"devices": [{"serial": row["serial"], "mac": row["mac"]}]}
    if row["group"]:
        body["group"] = row["group"]
    answer = plan["msp"].request("PUT", path, body=body)
    # This API answers HTTP 200 with its real result in "status_code"
    inner_status = answer.get("status_code")
    if inner_status not in (None, 200):
        raise RowFailed(f"tenant assignment answered status {inner_status}: {answer}")
    log.info("%s: OK assigned to tenant '%s' in group '%s'", row["serial"], row["tenant"], row["group"])


def assign_license(plan, dry_run):
    """Assign the service from the subscription code (e.g. foundation_ap) to the device."""
    row = plan["row"]
    if not plan["needs_license"]:
        log.info("%s: license %s already assigned", row["serial"], plan["service"])
        return
    if dry_run:
        log.info("[DRY-RUN] %s: would assign license %s (subscription %s)",
                 row["serial"], plan["service"], row["subscription"])
        return
    plan["msp"].request("POST", ASSIGN_LICENSE_PATH, body={"serials": [row["serial"]], "services": [plan["service"]]})
    log.info("%s: OK license %s assigned", row["serial"], plan["service"])


def assign_site(plan, dry_run):
    """Put the device in the site (uses the tenant's login)."""
    row = plan["row"]
    if plan["site_id"] is None:
        log.info("%s: no site to set", row["serial"])
        return
    if dry_run:
        log.info("[DRY-RUN] %s: would put in site '%s' (id %s)", row["serial"], row["site"], plan["site_id"])
        return

    site_device_type = SITE_DEVICE_TYPES.get(str(plan["device_type"]).lower())
    if not site_device_type:
        raise RowFailed(f"don't know the site device type for '{plan['device_type']}', add it to SITE_DEVICE_TYPES")
    body = {"site_id": plan["site_id"], "device_type": site_device_type, "device_ids": [row["serial"]]}
    try:
        plan["tenant_connection"].request("POST", SITE_ASSOCIATIONS_PATH, body=body)
    except ArubaAPIError as exc:
        if "already" in exc.message.lower():
            log.info("%s: already in site '%s'", row["serial"], row["site"])
            return
        raise RowFailed(f"{exc}. A new device can take a few minutes to show up in the tenant: "
                        f"run the script again later, finished steps are skipped") from exc
    log.info("%s: OK put in site '%s'", row["serial"], row["site"])


def refresh_device_types(plans, portals):
    """After claiming, read the inventory again to learn each new device's type (needed for the site)."""
    for portal_name, portal in portals.items():
        if any(plan["msp"] is portal["msp"] for plan in plans):
            portal["inventory"] = get_inventory(portal["msp"])
    for plan in plans:
        portal = portals[plan["row"]["msp_portal"]]
        device = portal["inventory"].get(plan["row"]["serial"]) or {}
        plan["device_type"] = device.get("device_type", plan["device_type"])


def apply_to_each(plans, failed, step_name, action, dry_run):
    """Run action(plan, dry_run) for every plan. A plan that fails is reported and left out of later steps."""
    still_ok = []
    for plan in plans:
        serial = plan["row"]["serial"]
        try:
            action(plan, dry_run)
        except (ArubaAPIError, RowFailed) as exc:
            log.error("%s: FAILED at %s step: %s", serial, step_name, exc)
            failed.append((serial, f"failed at {step_name} step: {exc}"))
            continue
        still_ok.append(plan)
    return still_ok


# --------------------------------------------------------------------------
# Main flow
# --------------------------------------------------------------------------
def run(args):
    dry_run = not args.apply
    failed = []  # (serial, reason) for every device that didn't make it

    # Step 1: read and check the CSV file
    rows, bad_rows = load_rows(args.csv_file)
    log.info("Step 1: %d usable row(s), %d bad row(s) in %s", len(rows), len(bad_rows), args.csv_file)
    for line, serial, reason in bad_rows:
        log.error("        CSV line %d (%s): SKIPPED - %s", line, serial, reason)
        failed.append((serial, f"CSV line {line}: {reason}"))
    if not rows:
        raise ScriptStopped("No usable rows in the CSV")

    # Step 2: log in to the MSP portals and tenants
    log.info("Step 2: Logging in to Aruba Central...")
    portals = connect_portals(rows)

    # Step 3: read tenants and inventory
    log.info("Step 3: Reading tenants and inventory...")
    read_portal_data(portals)

    # Step 4: check every row before anything is changed
    log.info("Step 4: Checking each device...")
    plans = []
    for row in rows:
        try:
            plan, reason = check_row(row, portals)
        except ArubaAPIError as exc:
            plan, reason = None, f"could not check it: {exc}"
        if reason:
            log.error("        %s (line %d): SKIPPED - %s", row["serial"], row["line"], reason)
            failed.append((row["serial"], reason))
            continue
        log.info("        %s: %s", row["serial"], describe_plan(plan))
        plans.append(plan)

    # Step 5-8: make the changes. A device that fails one step is skipped in the next ones.
    log.info("Step 5: Claiming devices...")
    plans = apply_to_each(plans, failed, "claim", claim_device, dry_run)

    log.info("Step 6: Assigning to tenant and group...")
    plans = apply_to_each(plans, failed, "tenant/group", assign_to_tenant, dry_run)

    log.info("Step 7: Assigning licenses...")
    plans = apply_to_each(plans, failed, "license", assign_license, dry_run)

    log.info("Step 8: Putting devices in sites...")
    if not dry_run:
        refresh_device_types(plans, portals)
    plans = apply_to_each(plans, failed, "site", assign_site, dry_run)

    # Step 9: summary
    log.info("Step 9: Done: %d OK, %d with problems%s", len(plans), len(failed),
             " (DRY RUN - nothing was changed, add --apply to do it)" if dry_run else "")
    for serial, reason in failed:
        log.error("PROBLEM SUMMARY %s: %s", serial, reason)
    return 1 if failed else 0


def main():
    """Read the arguments, set up logging and run the steps."""
    args = parse_arguments()
    _, log_file = setup_logging("claim_aruba_devices", args.log_file, LOG_DIR)

    # Let the shared Aruba client write to the same console + log file
    aruba_log = logging.getLogger("aruba_central")
    aruba_log.setLevel(logging.DEBUG)
    aruba_log.handlers = log.handlers

    log.info("=== Claim Aruba devices started%s ===", "" if args.apply else " (DRY RUN)")
    if not sys.argv[1:]:
        log.info("No arguments given, using DEBUG_ARGS from the top of this file: %s", DEBUG_ARGS)

    exit_code = run_script(log, log_file, run, args)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())

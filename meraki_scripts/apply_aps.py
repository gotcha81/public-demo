#!/usr/bin/env python3
"""
Apply access point settings from a CSV: put each AP in the right network,
set its hostname and assign an RF profile.

CSV format (header row required, rf_profile_name may be left empty):
    serial,hostname,network_name,rf_profile_name
    Q2XX-XXXX-XXXX,AP-Lobby-01,Stockholm Office,Custom Indoor
    Q2YY-YYYY-YYYY,AP-Office-02,Stockholm Office,

For each row:
  - the AP must already be in the organization's inventory
  - not in a network yet          -> claimed into network_name
  - in another network            -> skipped, unless --move is given
  - hostname is set if it differs
  - rf_profile_name is looked up in network_name and assigned if it differs
    (assigning a profile clears manual channel/power overrides on the AP)

Everything for a row is validated before anything is changed, so a bad row
does not leave an AP half configured.

Usage:
    python meraki_scripts/apply_aps.py aps.csv --dry-run    # show what would change
    python meraki_scripts/apply_aps.py aps.csv              # apply
    python meraki_scripts/apply_aps.py aps.csv --move       # also move APs from other networks
    python meraki_scripts/apply_aps.py aps.csv --org "My Org"

How to debug it:
    1. Check DEBUG_ARGS below (used when the script is started without arguments)
    2. Put a breakpoint on the first line inside run() (step 1),
       or inside process_row() to follow a single access point
    3. Right-click this file -> Debug, or use the "apply_aps (debug, dry-run)" run config
    4. F8 = run the line, F7 = go into one of the functions below
"""

import argparse
import logging
import sys
from pathlib import Path

import meraki

from resource_root.shared.meraki_helpers import (
    ScriptStopped,
    api_error_text,
    get_dashboard,
    get_networks_by_name,
    norm_serial,
    read_csv_rows,
    resolve_org_id,
    run_script,
    setup_logging,
)

# --------------------------------------------------------------------------
# Settings - change these
# --------------------------------------------------------------------------

# Arguments used when the script is started without any (e.g. Debug in PyCharm).
# Keep --dry-run here so debugging never changes anything by accident.
# The example CSV has made-up serials, so expect "not in the organization inventory".
DEBUG_ARGS = ["meraki_scripts/examples/aps_example.csv", "--dry-run"]

LOG_DIR = Path(__file__).parent / "logs"
REQUIRED_COLUMNS = ["serial", "hostname", "network_name", "rf_profile_name"]

# Same name as in setup_logging() in main(), so the functions below write to that log
log = logging.getLogger("apply_aps")


def parse_arguments():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("csv_file", help="CSV with serial,hostname,network_name,rf_profile_name")
    parser.add_argument("--org", help="Organization ID or name (default: MERAKI_ORG_ID or the only org)")
    parser.add_argument("--move", action="store_true", help="Move APs that are currently in another network")
    parser.add_argument("--dry-run", action="store_true", help="Show changes without applying them")
    parser.add_argument("--log-file", help="Log file path (default: meraki_scripts/logs/apply_aps_<timestamp>.log)")

    command_line = sys.argv[1:] or DEBUG_ARGS
    args = parser.parse_args(command_line)
    return args


# --------------------------------------------------------------------------
# Reading data
# --------------------------------------------------------------------------
def load_rows(path):
    """Read and validate the CSV. Returns a list of row dicts, last entry wins on duplicate serials."""
    try:
        csv_rows = read_csv_rows(path, REQUIRED_COLUMNS)
    except (OSError, ValueError) as exc:
        raise ScriptStopped(f"Could not read CSV {path}: {exc}") from exc

    rows = {}
    for line, row in csv_rows:
        if not (row["serial"] and row["hostname"] and row["network_name"]):
            log.warning("CSV line %d ignored (serial, hostname and network_name are required): %s", line, row)
            continue
        key = norm_serial(row["serial"])
        if key in rows:
            log.warning("CSV line %d: duplicate serial %s, using this entry instead of the earlier one",
                        line, row["serial"])
        rows[key] = row

    if not rows:
        raise ScriptStopped(f"No valid rows found in {path}")
    return list(rows.values())


def get_inventory(dashboard, org_id):
    """Return {normalised_serial: device} for every device in the organization's inventory."""
    devices = dashboard.organizations.getOrganizationInventoryDevices(org_id, total_pages="all")
    inventory = {norm_serial(d["serial"]): d for d in devices}
    return inventory


def get_rf_profile_id(dashboard, rf_profile_cache, network_id, name):
    """Look up an RF profile ID by name. Each network's profiles are fetched only once."""
    if network_id not in rf_profile_cache:
        profiles = dashboard.wireless.getNetworkWirelessRfProfiles(network_id, includeTemplateProfiles=True)
        rf_profile_cache[network_id] = {p["name"]: p["id"] for p in profiles}
    profile_id = rf_profile_cache[network_id].get(name)
    return profile_id


# --------------------------------------------------------------------------
# One access point: check -> plan -> apply
# --------------------------------------------------------------------------
def check_row(row, inventory, networks, args):
    """Validate one row before anything is changed.

    Returns (device, network, None) if it's OK, or (None, None, reason) if not.
    """
    dev = inventory.get(norm_serial(row["serial"]))
    if not dev:
        return None, None, "not in the organization inventory (claim it into the org first)"
    if dev.get("productType") != "wireless":
        return None, None, f"not an access point (model {dev.get('model')})"

    net_name = row["network_name"]
    net = networks.get(net_name)
    if not net:
        return None, None, f"network '{net_name}' not found"
    if "wireless" not in net.get("productTypes", []):
        return None, None, f"network '{net_name}' has no wireless product type"

    current_net = dev.get("networkId")
    if current_net and current_net != net["id"] and not args.move:
        return None, None, f"is in another network ({current_net}), use --move to move it"
    return dev, net, None


def plan_changes(dashboard, row, dev, net, rf_profile_cache):
    """Work out what needs to change. Returns (plan, None), or (None, reason) if it can't be done.

    plan is a dict that apply_changes() uses. plan["actions"] is a readable list (empty = nothing to do).
    """
    serial = dev["serial"]  # use the dashboard's own formatting from here on
    hostname, rf_name = row["hostname"], row["rf_profile_name"]
    current_net = dev.get("networkId")
    already_in_net = current_net == net["id"]
    old_name = dev.get("name") or ""

    plan = {
        "serial": serial,
        "move_from": None,       # network ID to remove the AP from first
        "claim_into": None,      # network ID to claim the AP into
        "new_name": None,        # hostname to set
        "rf_profile_id": None,   # RF profile to assign
        "actions": [],
    }

    if not already_in_net:
        plan["claim_into"] = net["id"]
        if current_net:
            plan["move_from"] = current_net
            plan["actions"].append(f"move from {current_net} to '{net['name']}'")
        else:
            plan["actions"].append(f"claim into '{net['name']}'")

    if not already_in_net or old_name != hostname:
        plan["new_name"] = hostname
        plan["actions"].append(f"name '{old_name}' -> '{hostname}'")

    if rf_name:
        rf_id = get_rf_profile_id(dashboard, rf_profile_cache, net["id"], rf_name)
        if not rf_id:
            return None, f"RF profile '{rf_name}' not found in network '{net['name']}' (upload it first)"
        current_rf = None
        if already_in_net:
            radio_settings = dashboard.wireless.getDeviceWirelessRadioSettings(serial)
            current_rf = radio_settings.get("rfProfileId")
        # Only assign if it differs (reassigning would clear manual channel/power overrides)
        if str(current_rf) != str(rf_id):
            plan["rf_profile_id"] = rf_id
            plan["actions"].append(f"RF profile -> '{rf_name}'")

    return plan, None


def apply_changes(dashboard, plan, row):
    """Send the planned changes to Meraki. Returns None on success, or a failure reason."""
    serial = plan["serial"]
    step = "network"
    try:
        if plan["move_from"]:
            dashboard.networks.removeNetworkDevices(plan["move_from"], serial)
            log.info("%s: OK removed from network %s", serial, plan["move_from"])
        if plan["claim_into"]:
            dashboard.networks.claimNetworkDevices(plan["claim_into"], serials=[serial])
            log.info("%s: OK claimed into '%s'", serial, row["network_name"])

        step = "hostname"
        if plan["new_name"]:
            dashboard.devices.updateDevice(serial, name=plan["new_name"])
            log.info("%s: OK name set to '%s'", serial, plan["new_name"])

        step = "RF profile"
        if plan["rf_profile_id"]:
            dashboard.wireless.updateDeviceWirelessRadioSettings(serial, rfProfileId=plan["rf_profile_id"])
            log.info("%s: OK RF profile '%s' assigned", serial, row["rf_profile_name"])
    except meraki.APIError as exc:
        return f"failed at {step} step: {api_error_text(exc)}"
    return None


def process_row(dashboard, row, inventory, networks, rf_profile_cache, args):
    """Check, plan and apply one CSV row. Returns None on success, or a failure reason."""
    dev, net, reason = check_row(row, inventory, networks, args)
    if reason:
        return reason

    try:
        plan, reason = plan_changes(dashboard, row, dev, net, rf_profile_cache)
    except meraki.APIError as exc:
        return f"could not read current settings: {api_error_text(exc)}"
    if reason:
        return reason

    serial = plan["serial"]
    if not plan["actions"]:
        log.info("%s (%s): already correct, nothing to do", serial, row["hostname"])
        return None
    prefix = "[DRY-RUN] " if args.dry_run else ""
    log.info("%s%s (%s): %s", prefix, serial, dev.get("model"), "; ".join(plan["actions"]))
    if args.dry_run:
        return None

    reason = apply_changes(dashboard, plan, row)
    return reason


# --------------------------------------------------------------------------
# Main flow
# --------------------------------------------------------------------------
def run(args):
    # Step 1: read the CSV
    rows = load_rows(args.csv_file)
    log.info("Step 1: Loaded %d access point(s) from %s", len(rows), args.csv_file)

    # Step 2: connect and find the organization
    log.info("Step 2: Connecting to Meraki...")
    dashboard = get_dashboard()
    org_id = resolve_org_id(dashboard, args.org)
    log.info("Step 2: Organization %s", org_id)

    # Step 3: get networks and inventory
    networks = get_networks_by_name(dashboard, org_id)
    inventory = get_inventory(dashboard, org_id)
    log.info("Step 3: %d networks, %d devices in inventory", len(networks), len(inventory))

    # Step 4: handle each access point (one failure does not stop the others)
    log.info("Step 4: Processing access points...")
    rf_profile_cache = {}
    ok, failed = 0, []
    for row in rows:
        try:
            reason = process_row(dashboard, row, inventory, networks, rf_profile_cache, args)
        except Exception as exc:  # one bad AP must not stop the run
            log.exception("%s: UNEXPECTED ERROR", row["serial"])
            reason = f"unexpected error: {exc}"
        if reason:
            log.error("%s: FAILED - %s", row["serial"], reason)
            failed.append((row["serial"], reason))
        else:
            ok += 1

    # Step 5: summary
    log.info("Step 5: Done: %d OK, %d with problems", ok, len(failed))
    for serial, reason in failed:
        log.error("PROBLEM SUMMARY %s: %s", serial, reason)
    return 1 if failed else 0


def main():
    """Read the arguments, set up logging and run the steps."""
    args = parse_arguments()
    _, log_file = setup_logging("apply_aps", args.log_file, LOG_DIR)
    log.info("=== Apply APs started%s ===", " (DRY RUN)" if args.dry_run else "")
    if not sys.argv[1:]:
        log.info("No arguments given, using DEBUG_ARGS from the top of this file: %s", DEBUG_ARGS)

    exit_code = run_script(log, log_file, run, args)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())

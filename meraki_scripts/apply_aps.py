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
"""

import argparse
import sys
from pathlib import Path

import meraki

from resource_root.shared.meraki_helpers import (
    api_error_text,
    get_dashboard,
    get_networks_by_name,
    norm_serial,
    read_csv_rows,
    resolve_org_id,
    setup_logging,
)

LOG_DIR = Path(__file__).parent / "logs"
REQUIRED_COLUMNS = ["serial", "hostname", "network_name", "rf_profile_name"]


def load_rows(path, log):
    """Read and validate the CSV. Returns a list of row dicts, last entry wins on duplicate serials."""
    rows = {}
    for line, row in read_csv_rows(path, REQUIRED_COLUMNS):
        if not (row["serial"] and row["hostname"] and row["network_name"]):
            log.warning("CSV line %d ignored (serial, hostname and network_name are required): %s", line, row)
            continue
        key = norm_serial(row["serial"])
        if key in rows:
            log.warning("CSV line %d: duplicate serial %s, using this entry instead of the earlier one",
                        line, row["serial"])
        rows[key] = row
    return list(rows.values())


class RfProfiles:
    """Per-network cache of {rf_profile_name: rf_profile_id}."""

    def __init__(self, dashboard):
        self.dashboard = dashboard
        self._cache = {}

    def get_id(self, network_id, name):
        if network_id not in self._cache:
            profiles = self.dashboard.wireless.getNetworkWirelessRfProfiles(
                network_id, includeTemplateProfiles=True)
            self._cache[network_id] = {p["name"]: p["id"] for p in profiles}
        return self._cache[network_id].get(name)


def process_row(row, ctx, args, log):
    """Apply one CSV row. Returns None on success, or a failure reason string."""
    dashboard, inventory, networks, rf_profiles = ctx
    serial, hostname = row["serial"], row["hostname"]
    net_name, rf_name = row["network_name"], row["rf_profile_name"]

    # ---- validate everything first ----
    dev = inventory.get(norm_serial(serial))
    if not dev:
        return "not in the organization inventory (claim it into the org first)"
    serial = dev["serial"]  # use the dashboard's own formatting from here on
    if dev.get("productType") != "wireless":
        return f"not an access point (model {dev.get('model')})"

    net = networks.get(net_name)
    if not net:
        return f"network '{net_name}' not found"
    if "wireless" not in net.get("productTypes", []):
        return f"network '{net_name}' has no wireless product type"
    net_id = net["id"]

    current_net = dev.get("networkId")
    if current_net and current_net != net_id and not args.move:
        return f"is in another network ({current_net}), use --move to move it"

    rf_id = None
    if rf_name:
        rf_id = rf_profiles.get_id(net_id, rf_name)
        if not rf_id:
            return f"RF profile '{rf_name}' not found in network '{net_name}' (upload it first)"

    # ---- work out what needs to change ----
    already_in_net = current_net == net_id
    actions = []
    if not already_in_net:
        actions.append(f"move from {current_net} to '{net_name}'" if current_net else f"claim into '{net_name}'")
    old_name = dev.get("name") or ""
    if not already_in_net or old_name != hostname:
        actions.append(f"name '{old_name}' -> '{hostname}'")
    if rf_id:
        current_rf = None
        if already_in_net:
            current_rf = dashboard.wireless.getDeviceWirelessRadioSettings(serial).get("rfProfileId")
        if str(current_rf) != str(rf_id):
            actions.append(f"RF profile -> '{rf_name}'")
        else:
            rf_id = None  # already correct, don't reassign (that would clear manual overrides)

    if not actions:
        log.info("%s (%s): already correct, nothing to do", serial, hostname)
        return None
    log.info("%s%s (%s): %s", "[DRY-RUN] " if args.dry_run else "", serial, dev.get("model"), "; ".join(actions))
    if args.dry_run:
        return None

    # ---- apply ----
    step = "network"
    try:
        if not already_in_net:
            if current_net:
                dashboard.networks.removeNetworkDevices(current_net, serial)
                log.info("%s: OK removed from network %s", serial, current_net)
            dashboard.networks.claimNetworkDevices(net_id, serials=[serial])
            log.info("%s: OK claimed into '%s'", serial, net_name)
        step = "hostname"
        if not already_in_net or old_name != hostname:
            dashboard.devices.updateDevice(serial, name=hostname)
            log.info("%s: OK name set to '%s'", serial, hostname)
        step = "RF profile"
        if rf_id:
            dashboard.wireless.updateDeviceWirelessRadioSettings(serial, rfProfileId=rf_id)
            log.info("%s: OK RF profile '%s' assigned", serial, rf_name)
    except meraki.APIError as exc:
        log.error("%s: FAILED at %s step: %s", serial, step, api_error_text(exc))
        return f"failed at {step} step: {api_error_text(exc)}"
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("csv_file", help="CSV with serial,hostname,network_name,rf_profile_name")
    parser.add_argument("--org", help="Organization ID or name (default: MERAKI_ORG_ID or the only org)")
    parser.add_argument("--move", action="store_true", help="Move APs that are currently in another network")
    parser.add_argument("--dry-run", action="store_true", help="Show changes without applying them")
    parser.add_argument("--log-file", help="Log file path (default: meraki_scripts/logs/apply_aps_<timestamp>.log)")
    args = parser.parse_args()

    log, log_file = setup_logging("apply_aps", args.log_file, LOG_DIR)
    log.info("=== Apply APs started%s ===", " (DRY RUN)" if args.dry_run else "")

    try:
        rows = load_rows(args.csv_file, log)
    except (OSError, ValueError) as exc:
        log.critical("Could not read CSV: %s", exc)
        sys.exit(1)
    if not rows:
        log.critical("No valid rows found in the CSV")
        sys.exit(1)
    log.info("Loaded %d access points from %s", len(rows), args.csv_file)

    try:
        dashboard = get_dashboard()
        org_id = resolve_org_id(dashboard, args.org)
        log.info("Organization %s: fetching networks and inventory...", org_id)
        networks = get_networks_by_name(dashboard, org_id)
        inventory = {norm_serial(d["serial"]): d for d in
                     dashboard.organizations.getOrganizationInventoryDevices(org_id, total_pages="all")}
        log.info("%d networks, %d devices in inventory", len(networks), len(inventory))
    except meraki.APIError as exc:
        log.critical("Could not read organization data: %s", api_error_text(exc))
        sys.exit(1)

    ctx = (dashboard, inventory, networks, RfProfiles(dashboard))
    ok, failed = 0, []
    for row in rows:
        try:
            reason = process_row(row, ctx, args, log)
        except Exception as exc:  # one bad AP must not stop the run
            log.exception("%s: UNEXPECTED ERROR", row["serial"])
            reason = f"unexpected error: {exc}"
        if reason:
            if not reason.startswith("failed at"):  # API failures are already logged
                log.error("%s: SKIPPED - %s", row["serial"], reason)
            failed.append((row["serial"], reason))
        else:
            ok += 1

    log.info("=== Done: %d OK, %d with problems ===", ok, len(failed))
    for serial, reason in failed:
        log.error("PROBLEM SUMMARY %s: %s", serial, reason)
    log.info("Full log written to %s", log_file)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()

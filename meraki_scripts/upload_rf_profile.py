#!/usr/bin/env python3
"""
Upload an RF profile from a JSON file to one or more wireless networks.

The JSON body uses the same fields as the Meraki API (see
https://developer.cisco.com/meraki/api-v1/#!create-network-wireless-rf-profile).
Keys starting with "_" are treated as comments and ignored. Start from
meraki_scripts/rf_profiles/basic_indoor.json, or export an existing profile
with export_rf_profile.py.

If a profile with the same name already exists in a network it is skipped,
unless --update is given, in which case it is overwritten with the JSON settings.

Usage:
    python meraki_scripts/upload_rf_profile.py meraki_scripts/rf_profiles/basic_indoor.json --network "Stockholm Office"
    python meraki_scripts/upload_rf_profile.py profile.json --csv aps.csv          # networks named in an AP CSV
    python meraki_scripts/upload_rf_profile.py profile.json --all-networks --dry-run
    python meraki_scripts/upload_rf_profile.py profile.json --network "HQ" --name "HQ-Indoor" --update

How to debug it:
    1. Check DEBUG_ARGS below (used when the script is started without arguments)
    2. Put a breakpoint on the first line inside run() (step 1)
    3. Right-click this file -> Debug, or use the "upload_rf_profile (debug, dry-run)" run config
    4. F8 = run the line, F7 = go into one of the functions below
"""

import argparse
import json
import logging
import sys
from pathlib import Path

import meraki

from resource_root.shared.meraki_helpers import (
    RF_PROFILE_READONLY_KEYS,
    ScriptStopped,
    api_error_text,
    get_dashboard,
    get_networks_by_name,
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
DEBUG_ARGS = [
    "meraki_scripts/rf_profiles/basic_indoor.json",
    "--network", "LAB 1",
    "--name", "RF-PROF-DEBUG",
    "--dry-run",
]

LOG_DIR = Path(__file__).parent / "logs"

# Same name as in setup_logging() in main(), so the functions below write to that log
log = logging.getLogger("upload_rf_profile")


def parse_arguments():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("profile_json", help="JSON file with the RF profile settings")
    target = parser.add_argument_group("target networks (pick at least one)")
    target.add_argument("--network", action="append", help="Network name (can be repeated)")
    target.add_argument("--csv", action="append", help="AP CSV file, uses its network_name column (can be repeated)")
    target.add_argument("--all-networks", action="store_true", help="Every network with wireless in the org")
    parser.add_argument("--name", help="Profile name, overrides \"name\" in the JSON")
    parser.add_argument("--update", action="store_true", help="Overwrite a profile that already exists")
    parser.add_argument("--org", help="Organization ID or name (default: MERAKI_ORG_ID or the only org)")
    parser.add_argument("--dry-run", action="store_true", help="Show changes without applying them")
    parser.add_argument("--log-file", help="Log file path (default: meraki_scripts/logs/upload_rf_profile_<timestamp>.log)")

    command_line = sys.argv[1:] or DEBUG_ARGS
    args = parser.parse_args(command_line)
    if not (args.network or args.csv or args.all_networks):
        parser.error("give --network, --csv or --all-networks")
    return args


def load_profile(path, name_override):
    """Read the JSON file and return the body to send to Meraki."""
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError) as exc:  # ValueError = the file is not valid JSON
        raise ScriptStopped(f"Could not load profile {path}: {exc}") from exc

    # Drop "_comment" keys and fields Meraki doesn't accept on create/update
    body = {k: v for k, v in raw.items() if not k.startswith("_") and k not in RF_PROFILE_READONLY_KEYS}
    if name_override:
        body["name"] = name_override
    if not body.get("name"):
        raise ScriptStopped(f"Profile {path} has no name (set \"name\" in the JSON or use --name)")
    body.setdefault("bandSelectionType", "ap")
    return body


def target_network_names(args, networks):
    """The networks to upload to, from --all-networks, --network and --csv."""
    if args.all_networks:
        wireless_names = sorted(n for n, net in networks.items() if "wireless" in net.get("productTypes", []))
        return wireless_names

    names = list(args.network or [])
    for csv_file in args.csv or []:
        try:
            csv_rows = read_csv_rows(csv_file, ["network_name"])
        except (OSError, ValueError) as exc:
            raise ScriptStopped(f"Could not read CSV {csv_file}: {exc}") from exc
        names += [row["network_name"] for _, row in csv_rows if row["network_name"]]
    unique_names = list(dict.fromkeys(names))  # de-duplicate, keep order
    return unique_names


def upload(dashboard, net, body, args):
    """Create or update the profile in one network. Returns None on success, or a failure reason."""
    name = body["name"]
    prefix = "[DRY-RUN] " if args.dry_run else ""

    if "wireless" not in net.get("productTypes", []):
        return "network has no wireless product type"

    try:
        profiles = dashboard.wireless.getNetworkWirelessRfProfiles(net["id"])
        existing = {p["name"]: p for p in profiles}

        if name in existing:
            if not args.update:
                log.info("'%s': profile '%s' already exists, skipped (use --update to overwrite)", net["name"], name)
                return None
            log.info("%s'%s': updating profile '%s'", prefix, net["name"], name)
            if not args.dry_run:
                dashboard.wireless.updateNetworkWirelessRfProfile(net["id"], existing[name]["id"], **body)
        else:
            log.info("%s'%s': creating profile '%s'", prefix, net["name"], name)
            if not args.dry_run:
                dashboard.wireless.createNetworkWirelessRfProfile(net["id"], **body)
    except meraki.APIError as exc:
        return api_error_text(exc)

    if not args.dry_run:
        log.info("'%s': OK", net["name"])
    return None


def run(args):
    # Step 1: read the profile JSON
    body = load_profile(args.profile_json, args.name)
    log.info("Step 1: Profile '%s' loaded from %s", body["name"], args.profile_json)
    log.debug("Profile body: %s", json.dumps(body))

    # Step 2: connect and find the organization
    log.info("Step 2: Connecting to Meraki...")
    dashboard = get_dashboard()
    org_id = resolve_org_id(dashboard, args.org)
    networks = get_networks_by_name(dashboard, org_id)
    log.info("Step 2: Organization %s has %d networks", org_id, len(networks))

    # Step 3: which networks should get the profile?
    names = target_network_names(args, networks)
    log.info("Step 3: %d target network(s): %s", len(names), names)

    # Step 4: upload to each network (one failure does not stop the others)
    log.info("Step 4: Uploading...")
    ok, failed = 0, []
    for name in names:
        net = networks.get(name)
        if not net:
            reason = "network not found"
        else:
            try:
                reason = upload(dashboard, net, body, args)
            except Exception as exc:  # one bad network must not stop the run
                log.exception("'%s': UNEXPECTED ERROR", name)
                reason = f"unexpected error: {exc}"
        if reason:
            log.error("'%s': FAILED - %s", name, reason)
            failed.append((name, reason))
        else:
            ok += 1

    # Step 5: summary
    log.info("Step 5: Done: %d OK, %d with problems", ok, len(failed))
    for name, reason in failed:
        log.error("PROBLEM SUMMARY '%s': %s", name, reason)
    return 1 if failed else 0


def main():
    """Read the arguments, set up logging and run the steps."""
    args = parse_arguments()
    _, log_file = setup_logging("upload_rf_profile", args.log_file, LOG_DIR)
    log.info("=== Upload RF profile started%s ===", " (DRY RUN)" if args.dry_run else "")
    if not sys.argv[1:]:
        log.info("No arguments given, using DEBUG_ARGS from the top of this file: %s", DEBUG_ARGS)

    exit_code = run_script(log, log_file, run, args)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())

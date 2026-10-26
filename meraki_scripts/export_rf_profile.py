#!/usr/bin/env python3
"""
Export an RF profile from a network to a JSON file that upload_rf_profile.py
can upload. Useful for getting Meraki's exact "Basic Indoor Profile" settings
as a starting point, or for copying a profile you tuned in the dashboard.

This script only reads from Meraki, it never changes anything there.

Usage:
    python meraki_scripts/export_rf_profile.py --network "Stockholm Office"
    python meraki_scripts/export_rf_profile.py --network "HQ" --profile "HQ-Indoor" -o hq_indoor.json
    python meraki_scripts/export_rf_profile.py --network "HQ" --new-name "Custom Indoor"
    python meraki_scripts/export_rf_profile.py --network "HQ" --all   # every profile, to rf_profiles/hq/

How to debug it:
    1. Check DEBUG_ARGS below (used when the script is started without arguments)
    2. Put a breakpoint on the first line inside run() (step 1)
    3. Right-click this file -> Debug, or use the "export_rf_profile (debug)" run config
    4. F8 = run the line, F7 = go into one of the functions below
"""

import argparse
import json
import logging
import re
import sys
from pathlib import Path

import meraki

from resource_root.shared.meraki_helpers import (
    RF_PROFILE_READONLY_KEYS,
    ScriptStopped,
    api_error_text,
    get_dashboard,
    get_networks_by_name,
    resolve_org_id,
    run_script,
    setup_logging,
)

# --------------------------------------------------------------------------
# Settings - change these
# --------------------------------------------------------------------------

# Arguments used when the script is started without any (e.g. Debug in PyCharm).
# --all saves to rf_profiles/<network>/, which is kept out of git (.gitignore).
DEBUG_ARGS = ["--network", "LAB 1", "--all"]

PROFILE_DIR = Path(__file__).parent / "rf_profiles"
LOG_DIR = Path(__file__).parent / "logs"

# Built-in profiles are not in the profile list, but can be fetched by these IDs
BASIC_PROFILE_IDS = {"basic indoor profile": "indoor", "basic outdoor profile": "outdoor"}

# Same name as in setup_logging() in main(), so the functions below write to that log
log = logging.getLogger("export_rf_profile")


def parse_arguments():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--network", required=True, help="Network to export from")
    parser.add_argument("--profile", default="Basic Indoor Profile", help="Profile name (default: %(default)s)")
    parser.add_argument("--all", action="store_true",
                        help="Export every profile in the network to rf_profiles/<network>/ (or -o as a folder)")
    parser.add_argument("--new-name", help="Name to store in the JSON (default: the profile's own name, "
                                           "or 'Custom <name>' for the built-in basic profiles)")
    parser.add_argument("-o", "--output", help="Output file, or folder with --all "
                                                "(default: meraki_scripts/rf_profiles/<name>.json)")
    parser.add_argument("--org", help="Organization ID or name (default: MERAKI_ORG_ID or the only org)")
    parser.add_argument("--log-file", help="Log file path (default: meraki_scripts/logs/export_rf_profile_<timestamp>.log)")

    command_line = sys.argv[1:] or DEBUG_ARGS
    args = parser.parse_args(command_line)
    return args


def slug(name):
    """Turn a name into something safe for a file name: 'LAB 1' -> 'lab_1'."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def is_basic_profile(name):
    return name.lower() in BASIC_PROFILE_IDS


def find_network(dashboard, org_id, network_name):
    networks = get_networks_by_name(dashboard, org_id)
    net = networks.get(network_name)
    if not net:
        raise ScriptStopped(f"Network '{network_name}' not found")
    return net


def find_profile(dashboard, network_id, name):
    """Return the profile called `name`, or None. Also finds the built-in basic profiles."""
    profiles = dashboard.wireless.getNetworkWirelessRfProfiles(network_id, includeTemplateProfiles=True)
    for p in profiles:
        if p["name"] == name:
            return p

    basic_id = BASIC_PROFILE_IDS.get(name.lower())
    if basic_id:
        basic_profile = dashboard.wireless.getNetworkWirelessRfProfile(network_id, basic_id)
        return basic_profile
    return None


def all_profiles(dashboard, network_id):
    """Every profile in the network, including the built-in basic ones."""
    profiles = dashboard.wireless.getNetworkWirelessRfProfiles(network_id, includeTemplateProfiles=True)
    for basic_name, basic_id in BASIC_PROFILE_IDS.items():
        already_listed = any(p["name"].lower() == basic_name for p in profiles)
        if already_listed:
            continue
        try:
            basic_profile = dashboard.wireless.getNetworkWirelessRfProfile(network_id, basic_id)
        except meraki.APIError as exc:
            log.warning("Could not fetch built-in profile '%s': %s", basic_id, api_error_text(exc))
            continue
        profiles.append(basic_profile)
    return profiles


def file_name_for(profile, new_name):
    """The name stored in the JSON. A built-in profile's name is reserved, so a copy gets 'Custom <name>'."""
    if new_name:
        return new_name
    if is_basic_profile(profile["name"]):
        return f"Custom {profile['name']}"
    return profile["name"]


def save_profile(profile, new_name, output):
    """Write the profile to `output`, without the read-only fields."""
    body = {k: v for k, v in profile.items() if k not in RF_PROFILE_READONLY_KEYS}
    body["name"] = file_name_for(profile, new_name)
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        raise ScriptStopped(f"Could not write {output}: {exc}") from exc
    log.info("Exported '%s' to %s (name in file: '%s')", profile["name"], output, body["name"])


def export_all(dashboard, net, args):
    profiles = all_profiles(dashboard, net["id"])
    log.info("Step 3: Found %d profile(s) in '%s'", len(profiles), net["name"])

    out_dir = Path(args.output) if args.output else PROFILE_DIR / slug(net["name"])
    for profile in profiles:
        output = out_dir / f"{slug(profile['name'])}.json"
        save_profile(profile, None, output)
    log.info("Step 4: %d profile(s) saved in %s", len(profiles), out_dir)


def export_one(dashboard, net, args):
    profile = find_profile(dashboard, net["id"], args.profile)
    if not profile:
        raise ScriptStopped(f"RF profile '{args.profile}' not found in network '{net['name']}'")
    log.info("Step 3: Found profile '%s' (id %s)", profile["name"], profile.get("id"))

    name_in_file = file_name_for(profile, args.new_name)
    output = Path(args.output) if args.output else PROFILE_DIR / f"{slug(name_in_file)}.json"
    save_profile(profile, args.new_name, output)
    log.info("Step 4: Saved")


def run(args):
    # Step 1: connect and find the organization
    log.info("Step 1: Connecting to Meraki...")
    dashboard = get_dashboard()
    org_id = resolve_org_id(dashboard, args.org)
    log.info("Step 1: Organization %s", org_id)

    # Step 2: find the network
    net = find_network(dashboard, org_id, args.network)
    log.info("Step 2: Network '%s' (id %s)", net["name"], net["id"])

    # Step 3 + 4: fetch the profile(s) and save them as JSON
    if args.all:
        export_all(dashboard, net, args)
    else:
        export_one(dashboard, net, args)
    return 0


def main():
    """Read the arguments, set up logging and run the steps."""
    args = parse_arguments()
    _, log_file = setup_logging("export_rf_profile", args.log_file, LOG_DIR)
    log.info("=== Export RF profile started ===")
    if not sys.argv[1:]:
        log.info("No arguments given, using DEBUG_ARGS from the top of this file: %s", DEBUG_ARGS)

    exit_code = run_script(log, log_file, run, args)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())

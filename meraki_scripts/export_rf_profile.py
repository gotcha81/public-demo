#!/usr/bin/env python3
"""
Export an RF profile from a network to a JSON file that upload_rf_profile.py
can upload. Useful for getting Meraki's exact "Basic Indoor Profile" settings
as a starting point, or for copying a profile you tuned in the dashboard.

Usage:
    python meraki_scripts/export_rf_profile.py --network "Stockholm Office"
    python meraki_scripts/export_rf_profile.py --network "HQ" --profile "HQ-Indoor" -o hq_indoor.json
    python meraki_scripts/export_rf_profile.py --network "HQ" --new-name "Custom Indoor"
    python meraki_scripts/export_rf_profile.py --network "HQ" --all   # every profile, to rf_profiles/hq/
"""

import argparse
import json
import re
import sys
from pathlib import Path

import meraki

from resource_root.shared.meraki_helpers import (
    RF_PROFILE_READONLY_KEYS,
    api_error_text,
    get_dashboard,
    get_networks_by_name,
    resolve_org_id,
)

PROFILE_DIR = Path(__file__).parent / "rf_profiles"
# Built-in profiles are not in the profile list, but can be fetched by these IDs
BASIC_PROFILE_IDS = {"basic indoor profile": "indoor", "basic outdoor profile": "outdoor"}


def find_profile(dashboard, network_id, name):
    for p in dashboard.wireless.getNetworkWirelessRfProfiles(network_id, includeTemplateProfiles=True):
        if p["name"] == name:
            return p
    basic_id = BASIC_PROFILE_IDS.get(name.lower())
    if basic_id:
        return dashboard.wireless.getNetworkWirelessRfProfile(network_id, basic_id)
    return None


def slug(name):
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def all_profiles(dashboard, network_id):
    """Every profile in the network, including the built-in basic ones."""
    profiles = dashboard.wireless.getNetworkWirelessRfProfiles(network_id, includeTemplateProfiles=True)
    for basic_name, basic_id in BASIC_PROFILE_IDS.items():
        if not any(p["name"].lower() == basic_name for p in profiles):
            try:
                profiles.append(dashboard.wireless.getNetworkWirelessRfProfile(network_id, basic_id))
            except meraki.APIError as exc:
                print(f"Could not fetch built-in profile '{basic_id}': {api_error_text(exc)}")
    return profiles


def save_profile(profile, new_name, output):
    """Write the profile without read-only fields. A built-in profile's name is reserved,
    so a copy of one gets a different name."""
    body = {k: v for k, v in profile.items() if k not in RF_PROFILE_READONLY_KEYS}
    is_basic = profile["name"].lower() in BASIC_PROFILE_IDS
    body["name"] = new_name or (f"Custom {profile['name']}" if is_basic else profile["name"])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    print(f"Exported '{profile['name']}' to {output} (name in file: '{body['name']}')")


def main():
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
    args = parser.parse_args()

    try:
        dashboard = get_dashboard()
        org_id = resolve_org_id(dashboard, args.org)
        net = get_networks_by_name(dashboard, org_id).get(args.network)
        if not net:
            sys.exit(f"Network '{args.network}' not found")
        if args.all:
            profiles = all_profiles(dashboard, net["id"])
        else:
            profile = find_profile(dashboard, net["id"], args.profile)
    except meraki.APIError as exc:
        sys.exit(f"API error: {api_error_text(exc)}")

    if args.all:
        out_dir = Path(args.output) if args.output else PROFILE_DIR / slug(args.network)
        for p in profiles:
            save_profile(p, None, out_dir / f"{slug(p['name'])}.json")
        print(f"{len(profiles)} profile(s) from '{args.network}' saved in {out_dir}")
        return

    if not profile:
        sys.exit(f"RF profile '{args.profile}' not found in network '{args.network}'")
    is_basic = args.profile.lower() in BASIC_PROFILE_IDS
    name = args.new_name or (f"Custom {args.profile}" if is_basic else profile["name"])
    output = Path(args.output) if args.output else PROFILE_DIR / f"{slug(name)}.json"
    save_profile(profile, args.new_name, output)


if __name__ == "__main__":
    main()

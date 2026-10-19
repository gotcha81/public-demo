#!/usr/bin/env python3
"""
Export an RF profile from a network to a JSON file that upload_rf_profile.py
can upload. Useful for getting Meraki's exact "Basic Indoor Profile" settings
as a starting point, or for copying a profile you tuned in the dashboard.

Usage:
    python meraki_scripts/export_rf_profile.py --network "Stockholm Office"
    python meraki_scripts/export_rf_profile.py --network "HQ" --profile "HQ-Indoor" -o hq_indoor.json
    python meraki_scripts/export_rf_profile.py --network "HQ" --new-name "Custom Indoor"
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


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--network", required=True, help="Network to export from")
    parser.add_argument("--profile", default="Basic Indoor Profile", help="Profile name (default: %(default)s)")
    parser.add_argument("--new-name", help="Name to store in the JSON (default: the profile's own name, "
                                           "or 'Custom <name>' for the built-in basic profiles)")
    parser.add_argument("-o", "--output", help="Output file (default: meraki_scripts/rf_profiles/<name>.json)")
    parser.add_argument("--org", help="Organization ID or name (default: MERAKI_ORG_ID or the only org)")
    args = parser.parse_args()

    try:
        dashboard = get_dashboard()
        org_id = resolve_org_id(dashboard, args.org)
        net = get_networks_by_name(dashboard, org_id).get(args.network)
        if not net:
            sys.exit(f"Network '{args.network}' not found")
        profile = find_profile(dashboard, net["id"], args.profile)
    except meraki.APIError as exc:
        sys.exit(f"API error: {api_error_text(exc)}")
    if not profile:
        sys.exit(f"RF profile '{args.profile}' not found in network '{args.network}'")

    body = {k: v for k, v in profile.items() if k not in RF_PROFILE_READONLY_KEYS}
    # A built-in profile's name is reserved, so a copy needs a different one
    is_basic = args.profile.lower() in BASIC_PROFILE_IDS
    body["name"] = args.new_name or (f"Custom {args.profile}" if is_basic else profile["name"])

    output = Path(args.output) if args.output else \
        PROFILE_DIR / (re.sub(r"[^a-z0-9]+", "_", body["name"].lower()).strip("_") + ".json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    print(f"Exported '{args.profile}' from '{args.network}' to {output} (name in file: '{body['name']}')")


if __name__ == "__main__":
    main()

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
"""

import argparse
import json
import sys
from pathlib import Path

import meraki

from resource_root.shared.meraki_helpers import (
    RF_PROFILE_READONLY_KEYS,
    api_error_text,
    get_dashboard,
    get_networks_by_name,
    read_csv_rows,
    resolve_org_id,
    setup_logging,
)

LOG_DIR = Path(__file__).parent / "logs"


def load_profile(path, name_override):
    with open(path, encoding="utf-8") as f:
        body = json.load(f)
    body = {k: v for k, v in body.items() if not k.startswith("_") and k not in RF_PROFILE_READONLY_KEYS}
    if name_override:
        body["name"] = name_override
    if not body.get("name"):
        raise ValueError("profile has no name (set \"name\" in the JSON or use --name)")
    body.setdefault("bandSelectionType", "ap")
    return body


def target_network_names(args, networks):
    if args.all_networks:
        return sorted(n for n, net in networks.items() if "wireless" in net.get("productTypes", []))
    names = list(args.network or [])
    for csv_file in args.csv or []:
        names += [row["network_name"] for _, row in read_csv_rows(csv_file, ["network_name"]) if row["network_name"]]
    return list(dict.fromkeys(names))  # de-duplicate, keep order


def upload(dashboard, net, body, args, log):
    """Create or update the profile in one network. Returns None on success, or a failure reason."""
    name = body["name"]
    prefix = "[DRY-RUN] " if args.dry_run else ""
    existing = {p["name"]: p for p in dashboard.wireless.getNetworkWirelessRfProfiles(net["id"])}

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
    if not args.dry_run:
        log.info("'%s': OK", net["name"])
    return None


def main():
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
    args = parser.parse_args()
    if not (args.network or args.csv or args.all_networks):
        parser.error("give --network, --csv or --all-networks")

    log, log_file = setup_logging("upload_rf_profile", args.log_file, LOG_DIR)
    log.info("=== Upload RF profile started%s ===", " (DRY RUN)" if args.dry_run else "")

    try:
        body = load_profile(args.profile_json, args.name)
    except (OSError, ValueError) as exc:
        log.critical("Could not load profile: %s", exc)
        sys.exit(1)
    log.info("Profile '%s' loaded from %s", body["name"], args.profile_json)
    log.debug("Profile body: %s", json.dumps(body))

    try:
        dashboard = get_dashboard()
        org_id = resolve_org_id(dashboard, args.org)
        networks = get_networks_by_name(dashboard, org_id)
        names = target_network_names(args, networks)
    except meraki.APIError as exc:
        log.critical("Could not read organization data: %s", api_error_text(exc))
        sys.exit(1)
    except (OSError, ValueError) as exc:
        log.critical("Could not read CSV: %s", exc)
        sys.exit(1)
    log.info("Organization %s: %d target network(s)", org_id, len(names))

    ok, failed = 0, []
    for name in names:
        net = networks.get(name)
        if not net:
            reason = "network not found"
        elif "wireless" not in net.get("productTypes", []):
            reason = "network has no wireless product type"
        else:
            try:
                reason = upload(dashboard, net, body, args, log)
            except meraki.APIError as exc:
                reason = api_error_text(exc)
            except Exception as exc:
                log.exception("'%s': UNEXPECTED ERROR", name)
                reason = f"unexpected error: {exc}"
        if reason:
            log.error("'%s': FAILED - %s", name, reason)
            failed.append((name, reason))
        else:
            ok += 1

    log.info("=== Done: %d OK, %d with problems ===", ok, len(failed))
    for name, reason in failed:
        log.error("PROBLEM SUMMARY '%s': %s", name, reason)
    log.info("Full log written to %s", log_file)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()

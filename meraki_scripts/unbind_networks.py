#!/usr/bin/env python3
"""
Unbind networks from their configuration template.

The template's current settings are kept as the network's own local settings
(retainConfigs), so the network keeps working exactly as before - it just no
longer follows the template. Use --no-retain to drop them instead (destructive).

Usage:
    python meraki_scripts/unbind_networks.py --all --dry-run      # every bound network in the org
    python meraki_scripts/unbind_networks.py --network "LAB 1" --network "LAB 2"
"""

import argparse
import sys
from pathlib import Path

import meraki

from resource_root.shared.meraki_helpers import (
    api_error_text,
    get_dashboard,
    get_networks_by_name,
    resolve_org_id,
    setup_logging,
)

LOG_DIR = Path(__file__).parent / "logs"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--network", action="append", help="Network name (can be repeated)")
    target.add_argument("--all", action="store_true", help="Every network in the org that is bound to a template")
    parser.add_argument("--no-retain", action="store_true",
                        help="Do NOT keep the template's settings on the network (they are lost)")
    parser.add_argument("--org", help="Organization ID or name (default: MERAKI_ORG_ID or the only org)")
    parser.add_argument("--dry-run", action="store_true", help="Show changes without applying them")
    parser.add_argument("--log-file", help="Log file path (default: meraki_scripts/logs/unbind_networks_<timestamp>.log)")
    args = parser.parse_args()

    log, log_file = setup_logging("unbind_networks", args.log_file, LOG_DIR)
    log.info("=== Unbind networks started%s ===", " (DRY RUN)" if args.dry_run else "")

    try:
        dashboard = get_dashboard()
        org_id = resolve_org_id(dashboard, args.org)
        networks = get_networks_by_name(dashboard, org_id)
        templates = {t["id"]: t["name"] for t in dashboard.organizations.getOrganizationConfigTemplates(org_id)}
    except meraki.APIError as exc:
        log.critical("Could not read organization data: %s", api_error_text(exc))
        sys.exit(1)

    names = sorted(n for n, net in networks.items() if net.get("isBoundToConfigTemplate")) if args.all else args.network
    retain = not args.no_retain
    prefix = "[DRY-RUN] " if args.dry_run else ""

    ok, failed = 0, []
    for name in names:
        net = networks.get(name)
        if not net:
            log.error("'%s': FAILED - network not found", name)
            failed.append((name, "network not found"))
            continue
        if not net.get("isBoundToConfigTemplate"):
            log.info("'%s': not bound to a template, nothing to do", name)
            ok += 1
            continue

        template = templates.get(net.get("configTemplateId"), net.get("configTemplateId"))
        log.info("%s'%s': unbinding from template '%s' (%s template settings)",
                 prefix, name, template, "keeping" if retain else "DROPPING")
        if args.dry_run:
            ok += 1
            continue
        try:
            dashboard.networks.unbindNetwork(net["id"], retainConfigs=retain)
            log.info("'%s': OK unbound", name)
            ok += 1
        except meraki.APIError as exc:
            log.error("'%s': FAILED - %s", name, api_error_text(exc))
            failed.append((name, api_error_text(exc)))

    log.info("=== Done: %d OK, %d with problems ===", ok, len(failed))
    for name, reason in failed:
        log.error("PROBLEM SUMMARY '%s': %s", name, reason)
    log.info("Full log written to %s", log_file)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Unbind networks from their configuration template.

The template's current settings are kept as the network's own local settings
(retainConfigs), so the network keeps working exactly as before - it just no
longer follows the template. Use --no-retain to drop them instead (destructive).

Usage:
    python meraki_scripts/unbind_networks.py --all --dry-run      # every bound network in the org
    python meraki_scripts/unbind_networks.py --network "LAB 1" --network "LAB 2"

How to debug it:
    1. Check DEBUG_ARGS below (used when the script is started without arguments)
    2. Put a breakpoint on the first line inside run() (step 1)
    3. Right-click this file -> Debug, or use the "unbind_networks (debug, dry-run)" run config
    4. F8 = run the line, F7 = go into one of the functions below
"""

import argparse
import logging
import sys
from pathlib import Path

import meraki

from resource_root.shared.meraki_helpers import (
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
# Keep --dry-run here so debugging never changes anything by accident.
DEBUG_ARGS = ["--all", "--dry-run"]

LOG_DIR = Path(__file__).parent / "logs"

# Same name as in setup_logging() in main(), so the functions below write to that log
log = logging.getLogger("unbind_networks")


def parse_arguments():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--network", action="append", help="Network name (can be repeated)")
    target.add_argument("--all", action="store_true", help="Every network in the org that is bound to a template")
    parser.add_argument("--no-retain", action="store_true",
                        help="Do NOT keep the template's settings on the network (they are lost)")
    parser.add_argument("--org", help="Organization ID or name (default: MERAKI_ORG_ID or the only org)")
    parser.add_argument("--dry-run", action="store_true", help="Show changes without applying them")
    parser.add_argument("--log-file", help="Log file path (default: meraki_scripts/logs/unbind_networks_<timestamp>.log)")

    command_line = sys.argv[1:] or DEBUG_ARGS
    args = parser.parse_args(command_line)
    return args


def get_template_names(dashboard, org_id):
    """Return {template_id: template_name} for every configuration template in the org."""
    templates = dashboard.organizations.getOrganizationConfigTemplates(org_id)
    template_names = {t["id"]: t["name"] for t in templates}
    return template_names


def pick_network_names(args, networks):
    """The networks to work on: the --network names, or every bound network with --all."""
    if args.all:
        bound_names = sorted(name for name, net in networks.items() if net.get("isBoundToConfigTemplate"))
        return bound_names
    return args.network


def unbind_one(dashboard, name, networks, template_names, args):
    """Unbind one network. Returns None on success, or a failure reason."""
    net = networks.get(name)
    if not net:
        return "network not found"
    if not net.get("isBoundToConfigTemplate"):
        log.info("'%s': not bound to a template, nothing to do", name)
        return None

    retain = not args.no_retain
    template_id = net.get("configTemplateId")
    template_name = template_names.get(template_id, template_id)
    prefix = "[DRY-RUN] " if args.dry_run else ""
    log.info("%s'%s': unbinding from template '%s' (%s template settings)",
             prefix, name, template_name, "keeping" if retain else "DROPPING")
    if args.dry_run:
        return None

    try:
        dashboard.networks.unbindNetwork(net["id"], retainConfigs=retain)
    except meraki.APIError as exc:
        return api_error_text(exc)
    log.info("'%s': OK unbound", name)
    return None


def run(args):
    # Step 1: connect and find the organization
    log.info("Step 1: Connecting to Meraki...")
    dashboard = get_dashboard()
    org_id = resolve_org_id(dashboard, args.org)
    log.info("Step 1: Organization %s", org_id)

    # Step 2: get networks and templates
    networks = get_networks_by_name(dashboard, org_id)
    template_names = get_template_names(dashboard, org_id)
    log.info("Step 2: %d networks, %d templates", len(networks), len(template_names))

    # Step 3: which networks should be unbound?
    names = pick_network_names(args, networks)
    log.info("Step 3: %d network(s) to handle: %s", len(names), names)

    # Step 4: unbind them one by one (one failure does not stop the others)
    log.info("Step 4: Unbinding...")
    ok, failed = 0, []
    for name in names:
        reason = unbind_one(dashboard, name, networks, template_names, args)
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
    _, log_file = setup_logging("unbind_networks", args.log_file, LOG_DIR)
    log.info("=== Unbind networks started%s ===", " (DRY RUN)" if args.dry_run else "")
    if not sys.argv[1:]:
        log.info("No arguments given, using DEBUG_ARGS from the top of this file: %s", DEBUG_ARGS)

    exit_code = run_script(log, log_file, run, args)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())

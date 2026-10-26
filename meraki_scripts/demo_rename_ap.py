#!/usr/bin/env python3
"""
DEMO: rename the first access point in the organization.

Made to be stepped through in the debugger, one step at a time.

How to debug it:
    1. Set NEW_AP_NAME below to the name you want
    2. Put a breakpoint on the first line inside run_demo() (step 1)
    3. Start the debugger on this file (no arguments needed)
    4. F8 = run the line, F7 = go into one of the functions below

Everything is logged to the console and to meraki_scripts/logs/demo_rename_ap_<time>.log.
If something goes wrong the script stops with a clear message instead of a long traceback.
The full traceback is still written to the log file.
"""

import logging
import sys
from pathlib import Path

import meraki

from resource_root.shared.meraki_helpers import ScriptStopped, api_error_text, get_dashboard, run_script, setup_logging

# --------------------------------------------------------------------------
# Settings - change these
# --------------------------------------------------------------------------

# The name you want the access point to have.
NEW_AP_NAME = "DEMO-AP001"

# True  = only SHOW what would be sent to Meraki (nothing is changed)
# False = really rename the access point
DRY_RUN = True

LOG_DIR = Path(__file__).parent / "logs"

# Same name as in setup_logging() in main(), so the functions below write to that log
log = logging.getLogger("demo_rename_ap")


def find_organization(dashboard):
    """Get the organizations the API key can see, and use the first one."""
    try:
        organizations = dashboard.organizations.getOrganizations()
    except meraki.APIError as exc:
        # 401 here almost always means a wrong or expired API key
        raise ScriptStopped(f"Could not list organizations: {api_error_text(exc)}") from exc

    if not organizations:
        raise ScriptStopped("The API key can't see any organizations")
    first_org = organizations[0]
    log.debug("Organizations visible to the API key: %s", [o["name"] for o in organizations])
    return first_org


def find_access_points(dashboard, org_id):
    """Get every wireless device (access point) in the organization."""
    try:
        access_points = dashboard.organizations.getOrganizationDevices(
            org_id, total_pages="all", productTypes=["wireless"])
    except meraki.APIError as exc:
        raise ScriptStopped(f"Could not list access points in org {org_id}: {api_error_text(exc)}") from exc
    return access_points


def rename_access_point(dashboard, serial, new_name):
    """Send the new name to Meraki. This is the only call that changes anything."""
    try:
        dashboard.devices.updateDevice(serial, name=new_name)
    except meraki.APIError as exc:
        raise ScriptStopped(f"Could not rename {serial} to '{new_name}': {api_error_text(exc)}") from exc


def run_demo():
    # Step 1: connect
    log.info("Step 1: Connecting to Meraki...")
    dashboard = get_dashboard()  # uses my_meraki_api_key from the credentials

    # Step 2: which organization are we working in?
    org = find_organization(dashboard)
    log.info("Step 2: Organization is '%s' (id %s)", org["name"], org["id"])

    # Step 3: get all access points
    access_points = find_access_points(dashboard, org["id"])
    log.info("Step 3: Found %d access point(s)", len(access_points))
    if not access_points:
        log.warning("        No access points in this organization, nothing to do.")
        return

    # Step 4: pick the first access point and store its current name
    ap = access_points[0]
    serial = ap["serial"]
    current_ap_name = ap.get("name") or ""  # an AP that was never named has no name
    log.info("Step 4: First access point is '%s' (serial %s, model %s)",
             current_ap_name, serial, ap.get("model", "?"))

    # Step 5: show the name we want (NEW_AP_NAME at the top of this file)
    log.info("Step 5: NEW_AP_NAME is '%s'", NEW_AP_NAME)
    if not NEW_AP_NAME.strip():
        raise ScriptStopped("NEW_AP_NAME is empty - set it at the top of this file")

    # Step 6: compare
    if NEW_AP_NAME == current_ap_name:
        log.info("Step 6: The name is already correct, nothing to change.")
        return
    log.info("Step 6: The name will change from '%s' to '%s'", current_ap_name, NEW_AP_NAME)

    # Step 7: rename (or only show what would happen)
    if DRY_RUN:
        log.info("Step 7: DRY RUN - this is what would be sent to Meraki:")
        log.info("        PUT /devices/%s  body={'name': '%s'}", serial, NEW_AP_NAME)
        log.info("        Nothing was changed. Set DRY_RUN = False at the top of this file to really do it.")
    else:
        rename_access_point(dashboard, serial, NEW_AP_NAME)
        log.info("Step 7: Done! '%s' is now called '%s'", current_ap_name, NEW_AP_NAME)


def main():
    """Set up logging, run the demo and turn errors into a clear message + exit code."""
    _, log_file = setup_logging("demo_rename_ap", log_dir=LOG_DIR)
    log.info("=== Rename AP demo started%s ===", " (DRY RUN)" if DRY_RUN else "")

    exit_code = run_script(log, log_file, run_demo)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())

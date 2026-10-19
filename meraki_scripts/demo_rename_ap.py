#!/usr/bin/env python3
"""
DEMO: rename the first access point in the organization.

Made to be stepped through in the debugger, one step at a time.

How to debug it:
    1. Set NEW_AP_NAME below to the name you want
    2. Put a breakpoint on the first line inside main() (step 1)
    3. Start the debugger on this file (no arguments needed)
    4. F8 = run the line, F7 = go into one of the functions below
"""

from resource_root.shared.meraki_helpers import get_dashboard

# --------------------------------------------------------------------------
# Settings - change these
# --------------------------------------------------------------------------

# The name you want the access point to have.
# It is set to the AP's name when this file was created, so nothing changes until you edit it.
NEW_AP_NAME = "DEMO-AP001"

# True  = only SHOW what would be sent to Meraki (nothing is changed)
# False = really rename the access point
DRY_RUN = True


def connect_to_meraki():
    """Log in to the Meraki API with my_meraki_api_key from the credentials."""
    dashboard = get_dashboard()
    return dashboard


def find_organization(dashboard):
    """Get the organizations the API key can see, and use the first one."""
    organizations = dashboard.organizations.getOrganizations()
    first_org = organizations[0]
    return first_org


def find_access_points(dashboard, org_id):
    """Get every wireless device (access point) in the organization."""
    access_points = dashboard.organizations.getOrganizationDevices(
        org_id, total_pages="all", productTypes=["wireless"])
    return access_points


def rename_access_point(dashboard, serial, new_name):
    """Send the new name to Meraki. This is the only line that changes anything."""
    dashboard.devices.updateDevice(serial, name=new_name)


def main():
    # Step 1: connect
    print("Step 1: Connecting to Meraki...")
    dashboard = connect_to_meraki()

    # Step 2: which organization are we working in?
    org = find_organization(dashboard)
    print(f"Step 2: Organization is '{org['name']}' (id {org['id']})")

    # Step 3: get all access points
    access_points = find_access_points(dashboard, org["id"])
    print(f"Step 3: Found {len(access_points)} access point(s)")
    if not access_points:
        print("        No access points in this organization, nothing to do.")
        return

    # Step 4: pick the first access point and store its current name
    ap = access_points[0]
    serial = ap["serial"]
    CURRENT_AP_NAME = ap["name"]
    print(f"Step 4: First access point is '{CURRENT_AP_NAME}' (serial {serial}, model {ap['model']})")

    # Step 5: show the name we want (NEW_AP_NAME at the top of this file)
    print(f"Step 5: NEW_AP_NAME is '{NEW_AP_NAME}'")

    # Step 6: compare
    if NEW_AP_NAME == CURRENT_AP_NAME:
        print("Step 6: The name is already correct, nothing to change.")
        return
    print(f"Step 6: The name will change from '{CURRENT_AP_NAME}' to '{NEW_AP_NAME}'")

    # Step 7: rename (or only show what would happen)
    if DRY_RUN:
        print("Step 7: DRY RUN - this is what would be sent to Meraki:")
        print(f"        PUT /devices/{serial}  body={{'name': '{NEW_AP_NAME}'}}")
        print("        Nothing was changed. Set DRY_RUN = False at the top of this file to really do it.")
    else:
        rename_access_point(dashboard, serial, NEW_AP_NAME)
        print(f"Step 7: Done! '{CURRENT_AP_NAME}' is now called '{NEW_AP_NAME}'")


if __name__ == "__main__":
    main()

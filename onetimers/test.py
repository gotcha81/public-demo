#!/usr/bin/env python3
"""
Rename Meraki access points from a CSV, tag them and assign an RF profile.
Everything that is done (and everything that fails) is written to a log file.

CSV format (header row optional):
    serial,name
    Q2XX-XXXX-XXXX,AP-Lobby-01
    Q2YY-YYYY-YYYY,AP-Office-02

Usage:
    python meraki_rename_aps.py aps.csv                     # apply changes
    python meraki_rename_aps.py aps.csv --dry-run           # show what would change
    python meraki_rename_aps.py aps.csv --log-file my.log   # custom log file name

Requires: pip install requests
"""

import argparse
import csv
import logging
import os
import sys
import time
from datetime import datetime

import requests
import meraki

import resource_root.credentials.my_creds as cred

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
API_KEY = cred.my_meraki_api_key
ORG_ID = os.getenv("MERAKI_ORG_ID", "YOUR_ORG_ID")  # or set directly: ORG_ID = "123456"

RF_PROFILE_NAME = "CWAP-PROF"
TAG = "CWAP"

BASE_URL = "https://api.meraki.com/api/v1"
MAX_RETRIES = 5

log = logging.getLogger("meraki_rename")

session = requests.Session()
session.headers.update(
    {
        "X-Cisco-Meraki-API-Key": API_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
)


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------
def setup_logging(log_file):
    """Log everything (DEBUG and up) to the file, INFO and up to the console."""
    log.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s [%(levelname)-7s] %(message)s", "%Y-%m-%d %H:%M:%S")

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    log.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    log.addHandler(ch)


# --------------------------------------------------------------------------
# API helpers
# --------------------------------------------------------------------------
def api(method, path, **kwargs):
    """Call the Meraki API with 429 / transient-error handling and logging.

    The API key is never written to the log.
    """
    url = path if path.startswith("http") else f"{BASE_URL}{path}"
    body = kwargs.get("json")
    resp = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.request(method, url, timeout=30, **kwargs)
        except requests.RequestException as exc:
            log.warning("API %s %s attempt %d/%d network error: %s", method, url, attempt, MAX_RETRIES, exc)
            if attempt == MAX_RETRIES:
                raise
            time.sleep(2 * attempt)
            continue

        log.debug("API %s %s body=%s -> HTTP %s", method, url, body, resp.status_code)

        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 2))
            log.warning("Rate limited (429) on %s %s, waiting %ds (attempt %d/%d)",
                        method, url, wait, attempt, MAX_RETRIES)
            time.sleep(wait)
            continue
        if resp.status_code >= 500 and attempt < MAX_RETRIES:
            log.warning("Server error %s on %s %s, retrying (attempt %d/%d)",
                        resp.status_code, method, url, attempt, MAX_RETRIES)
            time.sleep(2 * attempt)
            continue
        return resp
    return resp


def get_all(path, params=None):
    """GET with Link-header pagination."""
    results = []
    url = f"{BASE_URL}{path}"
    while url:
        resp = api("GET", url, params=params)
        resp.raise_for_status()
        results.extend(resp.json())
        url = resp.links.get("next", {}).get("url")
        params = None  # the next-link already contains the query string
    return results


def norm_serial(serial):
    """Normalise a serial for matching: upper case, no dashes or spaces."""
    return serial.replace("-", "").replace(" ", "").upper()


def get_org_devices():
    """Return {NORMALISED_SERIAL: device_dict} for all devices in the organization."""
    devices = get_all(f"/organizations/{ORG_ID}/devices", params={"perPage": 1000})
    return {norm_serial(d["serial"]): d for d in devices}


_rf_cache = {}


def get_rf_profile_id(network_id):
    """Find the RF profile ID by name in a network (cached per network)."""
    if network_id not in _rf_cache:
        resp = api("GET", f"/networks/{network_id}/wireless/rfProfiles")
        if resp.status_code != 200:
            log.error("Could not list RF profiles for network %s: HTTP %s %s",
                      network_id, resp.status_code, resp.text)
            _rf_cache[network_id] = None
        else:
            match = [p for p in resp.json() if p.get("name") == RF_PROFILE_NAME]
            if match:
                _rf_cache[network_id] = match[0]["id"]
                log.info("Network %s: found RF profile '%s' (id %s)",
                         network_id, RF_PROFILE_NAME, match[0]["id"])
            else:
                _rf_cache[network_id] = None
                log.warning("Network %s: RF profile '%s' not found", network_id, RF_PROFILE_NAME)
    return _rf_cache[network_id]


# --------------------------------------------------------------------------
# CSV
# --------------------------------------------------------------------------
def read_csv(path):
    rows = {}  # normalised serial -> (serial as written in CSV, name)
    with open(path, newline="", encoding="utf-8-sig") as f:
        for i, row in enumerate(csv.reader(f), start=1):
            row = [c.strip() for c in row]
            if len(row) < 2 or not row[0] or not row[1]:
                if any(row):
                    log.warning("CSV line %d ignored (needs serial and name): %s", i, row)
                continue
            if i == 1 and "serial" in row[0].lower():
                continue  # header
            key = norm_serial(row[0])
            if key in rows:
                log.warning("CSV line %d: duplicate serial %s (previously '%s'), using the later entry '%s'",
                            i, row[0], rows[key][1], row[1])
            rows[key] = (row[0].upper(), row[1])
    return list(rows.values())


# --------------------------------------------------------------------------
# Per-device work
# --------------------------------------------------------------------------
def process_device(serial, name, inventory, dry_run):
    """Process one AP. Returns None on success, or a failure reason string."""
    dev = inventory.get(norm_serial(serial))
    if not dev:
        log.error("%s: SKIPPED - serial not found in organization", serial)
        return "serial not found in organization"
    if dev.get("productType") != "wireless":
        log.error("%s: SKIPPED - not an access point (model %s)", serial, dev.get("model"))
        return f"not a wireless device ({dev.get('model')})"
    if not dev.get("networkId"):
        log.error("%s: SKIPPED - device is not assigned to a network", serial)
        return "device is not assigned to a network"

    serial = dev["serial"]  # use the dashboard's own serial format for API calls and logs
    network_id = dev["networkId"]
    old_name = dev.get("name") or ""
    old_tags = list(dev.get("tags") or [])
    new_tags = list(old_tags)
    if TAG not in new_tags:
        new_tags.append(TAG)  # keep existing tags, add ours

    rf_id = get_rf_profile_id(network_id)

    prefix = "[DRY-RUN] " if dry_run else ""
    log.info("%s%s (%s, network %s): name '%s' -> '%s', tags %s -> %s, RF profile -> %s",
             prefix, serial, dev.get("model"), network_id, old_name, name,
             old_tags, new_tags, RF_PROFILE_NAME if rf_id else "NOT FOUND")

    if dry_run:
        return None if rf_id else f"RF profile '{RF_PROFILE_NAME}' not found in network {network_id}"

    # 1) name + tags
    resp = api("PUT", f"/devices/{serial}", json={"name": name, "tags": new_tags})
    if resp.status_code != 200:
        log.error("%s: FAILED setting name/tags: HTTP %s %s", serial, resp.status_code, resp.text)
        return f"name/tags failed: HTTP {resp.status_code} {resp.text}"
    log.info("%s: OK name set to '%s', tags set to %s", serial, name, new_tags)

    # 2) RF profile
    if not rf_id:
        log.error("%s: FAILED RF profile '%s' not found in network %s (name/tags were applied)",
                  serial, RF_PROFILE_NAME, network_id)
        return f"RF profile '{RF_PROFILE_NAME}' not found in network {network_id}"

    resp = api("PUT", f"/devices/{serial}/wireless/radio/settings", json={"rfProfileId": rf_id})
    if resp.status_code != 200:
        log.error("%s: FAILED applying RF profile: HTTP %s %s (name/tags were applied)",
                  serial, resp.status_code, resp.text)
        return f"RF profile failed: HTTP {resp.status_code} {resp.text}"
    log.info("%s: OK RF profile '%s' applied (id %s)", serial, RF_PROFILE_NAME, rf_id)
    return None


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("csv_file", help="CSV with serial,name")
    parser.add_argument("--dry-run", action="store_true", help="Show changes without applying them")
    parser.add_argument("--log-file", help="Log file path (default: meraki_rename_<timestamp>.log)")
    args = parser.parse_args()

    log_file = args.log_file or f"meraki_rename_{datetime.now():%Y%m%d_%H%M%S}.log"
    setup_logging(log_file)

    log.info("=== Meraki AP rename started%s ===", " (DRY RUN)" if args.dry_run else "")
    log.info("CSV: %s | Org: %s | RF profile: %s | Tag: %s | Log file: %s",
             args.csv_file, ORG_ID, RF_PROFILE_NAME, TAG, log_file)

    if not API_KEY or ORG_ID == "YOUR_ORG_ID":
        log.critical("API_KEY (cred.cust_key0) or ORG_ID not set "
                     "(set ORG_ID in the script or via the MERAKI_ORG_ID environment variable)")
        sys.exit(1)

    try:
        rows = read_csv(args.csv_file)
    except OSError as exc:
        log.critical("Could not read CSV file: %s", exc)
        sys.exit(1)
    if not rows:
        log.critical("No valid rows found in the CSV")
        sys.exit(1)
    log.info("Loaded %d access points from CSV", len(rows))

    try:
        log.info("Fetching organization inventory...")
        inventory = get_org_devices()
        log.info("Inventory contains %d devices", len(inventory))
    except Exception:
        log.critical("Could not fetch organization inventory", exc_info=True)
        sys.exit(1)

    ok, failed = 0, []
    for serial, name in rows:
        try:
            reason = process_device(serial, name, inventory, args.dry_run)
        except Exception as exc:  # one bad device must not stop the run
            log.exception("%s: UNEXPECTED ERROR", serial)
            reason = f"unexpected error: {exc}"
        if reason:
            failed.append((serial, reason))
        else:
            ok += 1

    log.info("=== Done: %d succeeded, %d had problems ===", ok, len(failed))
    for serial, reason in failed:
        log.error("PROBLEM SUMMARY %s: %s", serial, reason)
    log.info("Full log written to %s", log_file)

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()

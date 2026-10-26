"""
Shared helpers for Meraki Dashboard scripts: API session, organization/network
lookup, CSV reading and logging.
"""

import csv
import getpass
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import meraki
import requests

import resource_root.credentials.my_creds as cred

# Every change (POST/PUT/PATCH/DELETE) any script makes is appended here
CHANGE_LOG = Path(__file__).resolve().parents[2] / "logs" / "meraki_api_changes.log"

# Fields the API returns for an RF profile that must not be sent back on create/update
RF_PROFILE_READONLY_KEYS = {"id", "networkId", "isIndoorDefault", "isOutdoorDefault"}


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------
def setup_logging(name, log_file=None, log_dir=None):
    """Log everything (DEBUG and up) to a file, INFO and up to the console.

    Returns (logger, log_file_path). Default file: <log_dir>/<name>_<timestamp>.log
    """
    if not log_file:
        log_dir = Path(log_dir or ".")
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"{name}_{datetime.now():%Y%m%d_%H%M%S}.log"

    log = logging.getLogger(name)
    log.setLevel(logging.DEBUG)
    log.handlers.clear()  # avoid duplicate output if called more than once
    fmt = logging.Formatter("%(asctime)s [%(levelname)-7s] %(message)s", "%Y-%m-%d %H:%M:%S")

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    log.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    log.addHandler(ch)
    return log, str(log_file)


# --------------------------------------------------------------------------
# Running a script
# --------------------------------------------------------------------------
class ScriptStopped(Exception):
    """The script can't continue. The message explains why (no traceback needed)."""


def run_script(log, log_file, run_function, *args):
    """Call run_function(*args) and turn errors into a clear message + exit code.

    Exit codes: 0 = OK, 1 = stopped or some items failed, 2 = unexpected error, 130 = Ctrl+C.
    run_function returns its own exit code (None counts as 0).
    """
    try:
        exit_code = run_function(*args) or 0
    except ScriptStopped as exc:
        # Expected problems: bad API key, missing network, unreadable file, ...
        log.error("STOPPED: %s", exc)
        exit_code = 1
    except meraki.APIError as exc:
        # An API call that the script didn't catch itself
        log.error("STOPPED: Meraki API error in %s: %s", exc.operation, api_error_text(exc))
        exit_code = 1
    except requests.exceptions.RequestException as exc:
        # No network, DNS, proxy or TLS problems (after the SDK's own retries)
        log.error("STOPPED: Could not reach the Meraki API: %s", exc)
        exit_code = 1
    except KeyboardInterrupt:
        log.warning("Stopped by user (Ctrl+C)")
        exit_code = 130
    except Exception:
        # Anything we didn't expect: full traceback goes to the log file and console
        log.exception("STOPPED: Unexpected error")
        exit_code = 2

    log.info("Full log written to %s", log_file)
    return exit_code


# --------------------------------------------------------------------------
# Dashboard API
# --------------------------------------------------------------------------
def _change_logger():
    """Logger that appends to the global change log, shared by all scripts."""
    log = logging.getLogger("meraki_api_changes")
    if not log.handlers:
        CHANGE_LOG.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(CHANGE_LOG, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S"))
        log.addHandler(fh)
        log.setLevel(logging.INFO)
        log.propagate = False  # keep these lines out of the scripts' own logs/console
    return log


def _log_changes(session):
    """Wrap the SDK session so every non-GET request is written to the change log."""
    change_log = _change_logger()
    script = Path(sys.argv[0]).name or "interactive"
    user = getpass.getuser()
    request = session.request

    def logged_request(metadata, method, url, **kwargs):
        if method == "GET":
            return request(metadata, method, url, **kwargs)
        body = json.dumps(kwargs.get("json"), ensure_ascii=False) if kwargs.get("json") is not None else "-"
        prefix = f"{script} ({user}) {metadata['operation']}: {method} {url} body={body}"
        try:
            response = request(metadata, method, url, **kwargs)
        except meraki.APIError as exc:
            change_log.info("%s -> FAILED %s", prefix, api_error_text(exc))
            raise
        except Exception as exc:
            change_log.info("%s -> FAILED %s", prefix, exc)
            raise
        status = response.status_code if response is not None else "simulated"
        change_log.info("%s -> OK %s", prefix, status)
        return response

    session.request = logged_request


def get_dashboard():
    """Return a DashboardAPI session. The SDK retries 429s and transient errors itself.

    All changes made through it are logged to CHANGE_LOG.
    """
    try:
        dashboard = meraki.DashboardAPI(
            cred.my_meraki_api_key,
            suppress_logging=True,  # our own logging covers it, and the SDK log can be noisy
            wait_on_rate_limit=True,
            maximum_retries=5,
        )
    except meraki.APIKeyError as exc:
        raise ScriptStopped(f"No Meraki API key found - check my_meraki_api_key in credentials ({exc})") from exc
    _log_changes(dashboard._session)
    return dashboard


def api_error_text(exc):
    """Readable one-line description of a meraki.APIError."""
    return f"HTTP {exc.status} {exc.reason}: {exc.message}"


def resolve_org_id(dashboard, org=None):
    """Find the organization ID to work in.

    Order: `org` argument (ID or name) -> MERAKI_ORG_ID env var -> `my_meraki_org_id`
    in credentials -> the only organization the API key can access.
    """
    org = org or os.getenv("MERAKI_ORG_ID") or getattr(cred, "my_meraki_org_id", None)
    orgs = dashboard.organizations.getOrganizations()

    if org:
        for o in orgs:
            if str(o["id"]) == str(org) or o["name"] == org:
                return o["id"]
        raise ScriptStopped(f"Organization '{org}' not found among the {len(orgs)} this API key can access")

    if len(orgs) == 1:
        return orgs[0]["id"]
    names = "\n".join(f"  {o['id']}  {o['name']}" for o in orgs)
    raise ScriptStopped(f"The API key has access to several organizations, pick one with --org:\n{names}")


def get_networks_by_name(dashboard, org_id):
    """Return {network_name: network_dict} for every network in the organization."""
    networks = dashboard.organizations.getOrganizationNetworks(org_id, total_pages="all")
    return {n["name"]: n for n in networks}


def norm_serial(serial):
    """Normalise a serial for matching: upper case, no dashes or spaces."""
    return serial.replace("-", "").replace(" ", "").upper()


# --------------------------------------------------------------------------
# CSV
# --------------------------------------------------------------------------
def read_csv_rows(path, required_columns):
    """Read a CSV with a header row. Returns a list of (line_number, row_dict).

    Header names are matched case-insensitively and values are stripped.
    Raises ValueError if a required column is missing from the header.
    """
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError("CSV file is empty")
        reader.fieldnames = [h.strip().lower() for h in reader.fieldnames]
        missing = [c for c in required_columns if c not in reader.fieldnames]
        if missing:
            raise ValueError(f"CSV is missing column(s) {missing}, header is {reader.fieldnames}")

        rows = []
        for row in reader:
            row = {k: (v or "").strip() for k, v in row.items() if k}
            if any(row.values()):
                rows.append((reader.line_num, row))
        return rows

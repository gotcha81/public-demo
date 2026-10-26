"""
Small client for the Classic Aruba Central REST API (the "API gateway").

One CentralConnection = one Aruba Central account (an MSP portal or a tenant).
It logs in, sends requests, and turns failed calls into ArubaAPIError with the
status code and Aruba's own error message.

Two ways to log in, chosen per account with "login":

    "api"       API token (refresh_token). Never asks anything, so it also works
                for scheduled / unattended runs.
    "password"  Aruba Central username + password + a one-time code (OTP) from your
                authenticator app. The script ASKS for the OTP when it's needed
                (and for the password, if it's not in my_creds.py).

Credentials go in resource_root/credentials/my_creds.py (never committed):

    aruba_msp_portals = {
        "MSP-EU": {                                   # the name used in the CSV's msp_portal column
            "base_url": "https://apigw-eucentral3.central.arubanetworks.com",
            "client_id": "...",                       # API Gateway > System Apps & Tokens
            "client_secret": "...",

            "login": "api",
            "refresh_token": "...",                   # "api" only

            # or instead:
            # "login": "password",
            # "username": "me@example.com",           # "password" only
            # "password": "...",                      # optional, asked for if left out
            # "customer_id": "...",                   # the MSP's customer ID (Account Home > Account Details)

            "tenants": {                              # needed for site + template (tenant-level APIs)
                # "api": each tenant needs its own client_id, client_secret and refresh_token
                "Customer A": {"client_id": "...", "client_secret": "...", "refresh_token": "..."},
                # "password": the MSP login is reused, only the tenant's customer_id is needed
                # "Customer B": {"customer_id": "..."},
            },
        },
    }

Aruba gives you a NEW refresh token every time you log in, and the old one stops
working. The newest one is saved in resource_root/credentials/aruba_tokens.json and
tried first, for both login types. So a "password" account only asks for an OTP
when that saved token no longer works (not used for 14 days, or the first run).
Passwords and OTP codes are never saved or logged.
"""

import getpass
import json
import logging
import re
import time
from pathlib import Path

import requests

import resource_root.credentials.my_creds as cred
from resource_root.shared.meraki_helpers import ScriptStopped

# Latest refresh token per account. Lives in credentials/, which is kept out of git.
TOKEN_CACHE = Path(cred.__file__).parent / "aruba_tokens.json"

LOGIN_TYPES = ("api", "password")

# Name of the OTP field in the username/password login call. Aruba's documentation only
# shows "username" and "password" for this call, so this name is NOT confirmed. If the
# "password" login fails with a valid OTP, check the request in your browser's developer
# tools when logging in to Aruba Central and change it here.
OTP_FIELD_NAME = "otp"

REQUEST_TIMEOUT = 30         # seconds to wait for Aruba Central to answer
MAX_RATE_LIMIT_RETRIES = 5   # how many times to wait and retry on HTTP 429 (too many requests)

log = logging.getLogger("aruba_central")


class ArubaAPIError(Exception):
    """An Aruba Central API call failed. str(exc) is a readable one-line description."""

    def __init__(self, method, path, status, message):
        self.method = method
        self.path = path
        self.status = status
        self.message = message
        super().__init__(f"{method} {path} -> HTTP {status}: {message}")


# --------------------------------------------------------------------------
# Credentials and tokens
# --------------------------------------------------------------------------
def get_portal_settings():
    """Return aruba_msp_portals from the credentials file, or stop with a clear message."""
    portals = getattr(cred, "aruba_msp_portals", None)
    if not portals:
        raise ScriptStopped("No aruba_msp_portals in resource_root/credentials/my_creds.py "
                            "(see resource_root/shared/aruba_central.py for the format)")
    return portals


def _load_token_cache():
    if not TOKEN_CACHE.exists():
        return {}
    try:
        return json.loads(TOKEN_CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("Could not read %s, ignoring it: %s", TOKEN_CACHE.name, exc)
        return {}


def _save_refresh_token(account_name, refresh_token):
    cache = _load_token_cache()
    cache[account_name] = {"refresh_token": refresh_token, "saved": time.strftime("%Y-%m-%d %H:%M:%S")}
    TOKEN_CACHE.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    TOKEN_CACHE.chmod(0o600)  # only your user can read it


def _error_text(response):
    """Pull Aruba's error message out of a response (the JSON format differs between APIs)."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:300] or response.reason
    if not isinstance(body, dict):
        return str(body)[:300]
    text = json.dumps(body)[:300]
    for key in ("description", "detail", "message", "error_description", "error"):
        if body.get(key):
            text = str(body[key])
            break
    if body.get("extra"):
        # e.g. adding a device: the reason per device is in "extra"
        text += f" {json.dumps(body['extra'])[:300]}"
    return text


# --------------------------------------------------------------------------
# Asking the user (only for "password" login)
# --------------------------------------------------------------------------
def _ask(prompt, hidden):
    """Ask on the keyboard. Stops clearly if nobody can answer (e.g. a scheduled run)."""
    try:
        answer = getpass.getpass(prompt) if hidden else input(prompt)
    except EOFError:
        raise ScriptStopped("Aruba Central needs an answer from the keyboard, but there is no keyboard input "
                            "(scheduled run?). Use \"login\": \"api\" for unattended runs.") from None
    return answer.strip()


def ask_for_password(account_name, username):
    log.info("Asking for the Aruba Central password for '%s'", account_name)
    return _ask(f"\nAruba Central password for {username} ({account_name}): ", hidden=True)


def ask_for_otp(account_name, username):
    """Ask for the one-time code. Makes it clear it's for Aruba Central, not Meraki or anything else."""
    log.info("Asking for the Aruba Central OTP for '%s'", account_name)
    print(f"\n=== ARUBA CENTRAL login: {account_name} ===")
    print(f"Aruba Central asks for a one-time code (OTP) for user {username}.")
    print("Open the authenticator app you use for ARUBA CENTRAL and type the current code.")
    for _ in range(3):
        otp = _ask("Aruba Central OTP: ", hidden=False)
        if re.fullmatch(r"\d{6,8}", otp):
            return otp
        print("An Aruba Central OTP is 6-8 digits, try again.")
    raise ScriptStopped(f"No valid Aruba Central OTP given for '{account_name}'")


# --------------------------------------------------------------------------
# Connection
# --------------------------------------------------------------------------
class CentralConnection:
    """A logged-in session to one Aruba Central account."""

    def __init__(self, account_name, base_url, settings):
        self.account_name = account_name      # e.g. "MSP-EU" or "MSP-EU / Customer A", used in logs
        self.base_url = base_url.rstrip("/")
        self.login_type = settings.get("login", "api")
        self._settings = settings             # credentials for this account (never logged)
        self._access_token = None
        self.session = requests.Session()

    # ---- logging in ----
    def login(self):
        """Get a fresh access token.

        1. the refresh token saved from the last run (works for both login types, asks nothing)
        2. "api":      the refresh_token in my_creds.py
           "password": username + password + OTP (asks for the OTP)
        """
        saved = _load_token_cache().get(self.account_name, {}).get("refresh_token")
        problem = None
        if saved:
            problem = self._login_with_refresh_token(saved)
            if not problem:
                return
            log.info("Saved token for '%s' didn't work (%s)", self.account_name, problem)

        if self.login_type == "api":
            from_creds = self._settings.get("refresh_token")
            if not from_creds:
                raise ScriptStopped(f"No refresh_token for '{self.account_name}' in my_creds.py")
            if from_creds != saved:  # no point trying the same token twice
                problem = self._login_with_refresh_token(from_creds)
            if problem:
                raise ScriptStopped(f"Could not log in to '{self.account_name}' ({problem}). Create a new token "
                                    f"in API Gateway > System Apps & Tokens and put it in my_creds.py")
        else:
            self._login_with_password()

    def _get_tokens(self, params):
        """POST /oauth2/token. Returns (tokens, None) or (None, problem)."""
        params = dict(params, client_id=self._settings["client_id"], client_secret=self._settings["client_secret"])
        try:
            response = self.session.post(f"{self.base_url}/oauth2/token", params=params, timeout=REQUEST_TIMEOUT)
        except requests.exceptions.RequestException as exc:
            # Don't log str(exc): it contains the URL, and the URL contains the client secret
            raise ScriptStopped(f"Could not reach {self.base_url} to log in '{self.account_name}' "
                                f"({type(exc).__name__})") from None
        if response.status_code != 200:
            return None, f"HTTP {response.status_code}: {_error_text(response)}"
        return response.json(), None

    def _use_tokens(self, tokens):
        self._access_token = tokens["access_token"]
        _save_refresh_token(self.account_name, tokens["refresh_token"])
        log.info("Logged in to Aruba Central '%s'", self.account_name)

    def _login_with_refresh_token(self, refresh_token):
        """Returns None if it worked, or a short description of the problem."""
        tokens, problem = self._get_tokens({"grant_type": "refresh_token", "refresh_token": refresh_token})
        if problem:
            return problem
        self._use_tokens(tokens)
        return None

    def _login_with_password(self):
        """Aruba's 3-step OAuth login: username/password(+OTP) -> auth code -> tokens."""
        username = self._settings["username"]
        password = self._settings.get("password") or ask_for_password(self.account_name, username)
        otp = ask_for_otp(self.account_name, username)
        client_id = self._settings["client_id"]

        # Step A: log in, Aruba answers with two cookies (csrftoken + session)
        login_body = {"username": username, "password": password, OTP_FIELD_NAME: otp}
        response = self._post_login_step("/oauth2/authorize/central/api/login", {"client_id": client_id},
                                         json_body=login_body)
        csrf_token = response.cookies.get("csrftoken")
        session_key = response.cookies.get("session")
        if not (csrf_token and session_key):
            raise ScriptStopped(f"Aruba Central login for '{self.account_name}' gave no session "
                                f"(wrong password or OTP?)")

        # Step B: ask for an authorization code for this customer (MSP or tenant)
        response = self._post_login_step(
            "/oauth2/authorize/central/api",
            {"client_id": client_id, "response_type": "code", "scope": "all"},
            json_body={"customer_id": self._settings["customer_id"]},
            headers={"X-CSRF-Token": csrf_token, "Cookie": f"session={session_key}"},
        )
        auth_code = response.json().get("auth_code")
        if not auth_code:
            raise ScriptStopped(f"Aruba Central gave no authorization code for '{self.account_name}' "
                                f"(check customer_id in my_creds.py)")

        # Step C: swap the code for tokens (the code is only valid for 5 minutes)
        tokens, problem = self._get_tokens({"grant_type": "authorization_code", "code": auth_code})
        if problem:
            raise ScriptStopped(f"Could not get tokens for '{self.account_name}': {problem}")
        self._use_tokens(tokens)

    def _post_login_step(self, path, params, json_body, headers=None):
        """One step of the password login. Stops with a clear message if Aruba says no."""
        try:
            response = self.session.post(self.base_url + path, params=params, json=json_body,
                                         headers=headers, timeout=REQUEST_TIMEOUT)
        except requests.exceptions.RequestException as exc:
            raise ScriptStopped(f"Could not reach {self.base_url} to log in '{self.account_name}' "
                                f"({type(exc).__name__})") from None
        if response.status_code != 200:
            raise ScriptStopped(f"Aruba Central login for '{self.account_name}' failed at {path}: "
                                f"HTTP {response.status_code}: {_error_text(response)}. Check the username, "
                                f"password and OTP (an OTP can only be used once - wait for the next code)")
        return response

    # ---- API calls ----
    def request(self, method, path, params=None, body=None):
        """Send one API call and return the JSON answer. Raises ArubaAPIError if it fails.

        Every change (anything but GET) is logged together with its body.
        """
        if method != "GET":
            log.info("SEND [%s] %s %s body=%s", self.account_name, method, path, json.dumps(body))
        retries_left = MAX_RATE_LIMIT_RETRIES
        logged_in_again = False
        while True:
            headers = {"Authorization": f"Bearer {self._access_token}", "Accept": "application/json"}
            response = self.session.request(method, self.base_url + path, params=params, json=body,
                                            headers=headers, timeout=REQUEST_TIMEOUT)

            if response.status_code == 401 and not logged_in_again:
                # Access tokens last 2 hours, so a long run can need a new one
                log.info("Access token for '%s' expired, logging in again", self.account_name)
                self.login()
                logged_in_again = True
                continue
            if response.status_code == 429 and retries_left > 0:
                log.debug("Rate limited on %s %s, waiting 2 seconds", method, path)
                time.sleep(2)
                retries_left -= 1
                continue
            break

        if not response.ok:
            raise ArubaAPIError(method, path, response.status_code, _error_text(response))
        if not response.content:
            return {}
        answer = response.json()
        log.debug("%s %s -> %s", method, path, json.dumps(answer)[:2000])
        return answer

    def get_all(self, path, list_key, page_size, params=None):
        """GET every page of a list. list_key is where the list is in the answer, e.g. "sites"."""
        items = []
        offset = 0
        while True:
            page_params = dict(params or {}, limit=page_size, offset=offset)
            answer = self.request("GET", path, params=page_params)
            page = answer.get(list_key) or []
            items += page
            if len(page) < page_size:
                return items
            offset += page_size


def connect(account_name, settings, base_url=None):
    """Create a CentralConnection from one entry in aruba_msp_portals, and log in."""
    login_type = settings.get("login", "api")
    if login_type not in LOGIN_TYPES:
        raise ScriptStopped(f"'{account_name}': \"login\" must be one of {LOGIN_TYPES}, not '{login_type}'")

    required = ["client_id", "client_secret"]
    if login_type == "password":
        required += ["username", "customer_id"]
    missing = [key for key in required if not settings.get(key)]
    base_url = base_url or settings.get("base_url")
    if not base_url:
        missing.append("base_url")
    if missing:
        raise ScriptStopped(f"'{account_name}' in aruba_msp_portals is missing {missing} "
                            f"(login type '{login_type}')")

    connection = CentralConnection(account_name, base_url, settings)
    connection.login()
    return connection

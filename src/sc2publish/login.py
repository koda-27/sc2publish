"""Headless, editor-free Battle.net password login for the SC2 web credential.

This reproduces, in pure Python and without Wine, CEF, or the game, the login
sequence the StarCraft II editor's embedded browser uses:

1. The game client's Front service issues a ``ChallengeExternalRequest``
   (``web_auth_url``) for ``https://<region>.account.battle.net/login/`` with
   ``externalChallenge=login`` and ``app=s2``.
2. The login page performs an SRP6a password exchange: ``POST /login/srp``
   returns the SRP parameters, and the form is submitted with ``publicA`` and
   ``clientEvidenceM1`` (the password itself is never sent).
3. On success the browser is redirected to ``http://localhost:0/?ST=<token>``;
   the ``ST`` value is the 45-byte (``EU-...``) SC2 web credential.
4. ``sc2publish`` consumes that credential as ``SC2PUBLISH_WEB_CREDENTIAL``;
   an authenticated Front connection can also call AuthenticationServer
   method 8 (``GenerateWebCredentials``) to renew it.

The SRP6a algorithm and request shapes were recovered offline from the
editor's login bundles and validated against their worker with Node.
This module never prints credential values.

The account-session layer uses these helpers when Battle.net challenges a
normal publishing connection. The public CLI is ``sc2publish auth``.
"""

import hashlib
import json
import re
import secrets
import sys
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit

from ._credentials import load_credentials
from .config import private_write
from .protocol.wire import field, parse

AUTH = 0x0DECFC01
CONNECTION = 0x65446991
AUTH_LISTENER = 1898188341
CHALLENGE_LISTENER = 3151632159
S2_PROGRAM = 0x5332
RESPONSE_SERVICE = 254

LOGIN_HOST = "https://{region}.account.battle.net"
LOGIN_PATH = "/login/"
LOGIN_QUERY = {"externalChallenge": "login", "app": "s2"}
SRP_PATH = "/login/srp?csrfToken=true"
DEFAULT_LOCALE = "enUS"
DEFAULT_TIMEOUT = 30.0
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) StarCraft II/4.13.2 Chrome/91.0.4472.124 Safari/537.36"
)


class LoginError(RuntimeError):
    """A sanitized login failure; never carries credential material."""


def padded_hex(value, chars):
    return format(value, "x").rjust(chars, "0")


def bigint_to_signed_bytes(value):
    """The bundle's BigInteger.toByteArray(): minimal two's-complement BE."""
    length = (value.bit_length() + 8) // 8
    return value.to_bytes(max(length, 1), "big")


def compute_x(username, password, salt_hex, version, iterations):
    """Password verifier x, matching the login bundle's worker exactly."""
    try:
        salt = bytes.fromhex(salt_hex)
    except ValueError:
        raise LoginError("The login server returned a malformed salt.") from None
    if version == 1:
        inner = hashlib.sha256(f"{username}:{password[:16].upper()}".encode()).digest()
        return int.from_bytes(hashlib.sha256(salt + inner).digest(), "little")
    if version == 2:
        secret = f"{username}:{password[:128]}".encode()
        rounds = int(iterations or 15000)
        if rounds < 1:
            raise LoginError("The login server returned invalid SRP iterations.")
        derived = hashlib.pbkdf2_hmac("sha512", secret, salt, rounds, 64)
        # jsbn's BigInteger(byteArray) is two's-complement signed, so the login
        # bundle's version-2 x is negative when the derived key's top bit is set.
        value = int.from_bytes(derived, "big")
        if derived[0] & 0x80:
            value -= 1 << (8 * len(derived))
        return value
    raise LoginError(f"Unsupported SRP password version {version}.")


def srp_client_proof(username, password, salt_hex, modulus_hex, generator_hex,
                     public_b_hex, version, iterations, private_a=None):
    """Return (publicA_hex, clientEvidenceM1_hex) for the login form."""
    try:
        modulus = int(modulus_hex, 16)
        generator = int(generator_hex, 16)
        public_b = int(public_b_hex, 16)
    except (TypeError, ValueError):
        raise LoginError("The login server returned malformed SRP parameters.") from None
    if modulus <= 2 or generator < 2 or generator >= modulus:
        raise LoginError("The login server returned an unusable SRP group.")
    if not 1 < public_b < modulus:
        raise LoginError("The login server returned an invalid public value.")
    chars = (modulus.bit_length() + 3) // 4
    if private_a is None:
        bits = min(256, modulus.bit_length() // 2)
        low = 1 << (bits - 1)
        private_a = low + secrets.randbelow(modulus - 1 - low)
    x = compute_x(username, password, salt_hex, version, iterations)
    if x == 0:
        raise LoginError("The SRP verifier is empty.")
    while True:
        public_a = pow(generator, private_a, modulus)
        if public_a not in (0, 1, modulus - 1):
            break
        private_a += 1
    u = int.from_bytes(
        hashlib.sha256(bytes.fromhex(
            padded_hex(public_a, chars) + padded_hex(public_b, chars))).digest(), "big")
    u &= (1 << 256) - 1
    k = int.from_bytes(
        hashlib.sha256(bytes.fromhex(
            padded_hex(modulus, chars) + padded_hex(generator, chars))).digest(), "big")
    k &= (1 << 256) - 1
    if u == 0:
        raise LoginError("The login server produced a zero scrambling parameter.")
    base = (public_b - k * pow(generator, x, modulus)) % modulus
    shared = pow(base, private_a + u * x, modulus)
    if shared == 0:
        raise LoginError("The SRP shared secret is zero.")
    evidence = hashlib.sha256(
        bigint_to_signed_bytes(public_a)
        + bigint_to_signed_bytes(public_b)
        + bigint_to_signed_bytes(shared)).digest()
    return format(public_a, "x"), format(int.from_bytes(evidence, "big"), "x")


class LoginPage(HTMLParser):
    """Collect form actions and input fields from a Battle.net login page."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms = []
        self.inputs = []
        self._current = None

    def handle_starttag(self, tag, attrs):
        attributes = {name.lower(): (value or "") for name, value in attrs}
        if tag == "form":
            self._current = {"attrs": attributes, "inputs": []}
            self.forms.append(self._current)
        elif tag in ("input", "button", "select", "textarea"):
            entry = dict(attributes)
            if tag in ("button", "select", "textarea"):
                entry.setdefault("type", tag)
            self.inputs.append(entry)
            if self._current is not None and tag == "input":
                self._current["inputs"].append(entry)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag == "form":
            self._current = None

    def value_for(self, identity):
        for entry in reversed(self.inputs):
            if identity in (entry.get("id"), entry.get("name")):
                return entry.get("value", "")
        return None

    def name_for(self, identity):
        for entry in self.inputs:
            if entry.get("id") == identity:
                return entry.get("name") or identity
        for entry in self.inputs:
            if entry.get("name") == identity:
                return identity
        return None

    def form_for(self, identity):
        for form in self.forms:
            if any(identity in (entry.get("id"), entry.get("name")) for entry in form["inputs"]):
                return form
        return None

    def form_fields(self, form):
        data = {}
        for entry in form["inputs"] if form else self.inputs:
            name = entry.get("name")
            if not name or (entry.get("type") or "text").lower() in ("button", "submit", "checkbox"):
                continue
            data[name] = entry.get("value", "")
        return data


def parse_login_page(html):
    page = LoginPage()
    page.feed(html)
    return page


def extract_st_token(text):
    """Pull the ST credential out of a callback URL, never exposing it."""
    if not text:
        return None
    for match in re.finditer(r"[?&#]ST=([^&#\s\"']{4,1024})", text):
        return match.group(1).encode()
    return None


def credential_shape(credential):
    prefix = credential[:3].decode("ascii", "replace") if credential.isascii() else "?"
    return {
        "length": len(credential),
        "ascii": credential.isascii(),
        "region_prefix": prefix,
        "printable": all(32 <= byte < 127 for byte in credential),
    }


_SENSITIVE_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_SENSITIVE_HEX = re.compile(r"\b[0-9a-fA-F]{16,}\b")
_SENSITIVE_B64 = re.compile(r"\b[A-Za-z0-9+/]{24,}={0,2}\b")
_SENSITIVE_CREDENTIAL = re.compile(r"\b[A-Z]{2}-[A-Za-z0-9_\-+/=]{8,}")
_SENSITIVE_QUERY = re.compile(r"(?i)([?&](?:ST|csrfToken|csrf_token|nonce|token)=)[^&\"'<>\s]+")
_SENSITIVE_FIELD = re.compile(
    r"(?i)((?:csrf[_-]?token|csrftoken|nonce|password|publicA|clientEvidenceM1)"
    r"\"?\s*[:=]\s*\"?)[^\"'&<>\s,}]+")


def redact_text(text, *, account=None):
    """Redact credential material and account identifiers from captured text."""
    if not text:
        return ""
    redacted = str(text)
    if account:
        redacted = redacted.replace(account, "[account]")
    redacted = _SENSITIVE_EMAIL.sub("[email]", redacted)
    redacted = _SENSITIVE_FIELD.sub(r"\1[redacted]", redacted)
    redacted = _SENSITIVE_QUERY.sub(r"\1[redacted]", redacted)
    redacted = _SENSITIVE_CREDENTIAL.sub("[credential]", redacted)
    redacted = _SENSITIVE_HEX.sub("[hex]", redacted)
    redacted = _SENSITIVE_B64.sub("[token]", redacted)
    return redacted


class LoginDiagnostics:
    """Collect a redacted record of a live login attempt for private inspection."""

    MAX_BODY = 128 * 1024

    def __init__(self, directory, *, account=None):
        self.directory = Path(directory)
        self.account = account
        self.records = []

    def record(self, stage, *, response=None, request_fields=None, method=None, url=None, note=None):
        entry = {"stage": stage}
        if note:
            entry["note"] = note
        if method or url:
            entry["request"] = {
                "method": method,
                "url": redact_text(url or "", account=self.account),
            }
        if request_fields is not None:
            entry["request_fields"] = {
                name: {"length": len(str(value)), "numeric": str(value).isdigit()}
                for name, value in sorted(request_fields.items())}
        if response is not None:
            headers = {}
            for name in ("Content-Type", "Location", "X-Frame-Options"):
                value = response.headers.get(name)
                if value is not None:
                    headers[name] = redact_text(value, account=self.account)
            try:
                cookie_names = sorted({cookie.name for cookie in response.cookies})
            except Exception:  # noqa: BLE001
                cookie_names = []
            body = response.text or ""
            entry["response"] = {
                "status": response.status_code,
                "url": redact_text(getattr(response, "url", "") or "", account=self.account),
                "headers": headers,
                "set_cookie_names": cookie_names,
                "body": redact_text(body[: self.MAX_BODY], account=self.account),
                "body_truncated": len(body) > self.MAX_BODY,
            }
        self.records.append(entry)
        return entry

    def record_srp_payload(self, payload):
        shape = {}
        for key, value in payload.items():
            if key in ("version", "iterations", "eligible_credential_upgrade", "hash_function"):
                shape[key] = value
            elif isinstance(value, str):
                shape[key] = {"length": len(value)}
            else:
                shape[key] = value
        self.records.append({"stage": "srp-response-shape", "payload": shape})

    def flush(self, error=None):
        if not self.records:
            return None
        self.directory.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        path = self.directory / f"login-diagnostics-{stamp}.json"
        document = {
            "error": redact_text(str(error), account=self.account) if error else None,
            "records": self.records,
        }
        private_write(path, json.dumps(document, indent=2))
        self.records = []
        return path


def default_login_url(region):
    query = urlencode(LOGIN_QUERY)
    return f"{LOGIN_HOST.format(region=region.lower())}{LOGIN_PATH}?{query}"


def validate_challenge_url(url):
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not (host == "battle.net" or host.endswith(".battle.net")):
        raise LoginError("Battle.net returned an unexpected authentication URL.")
    return url


def build_login_form(page, email, password, srp):
    form = page.form_for("password") or page.form_for("accountName")
    data = page.form_fields(form)
    account_name = page.name_for("accountName") or "accountName"
    data[account_name] = email
    if srp is None:
        password_name = page.name_for("password") or "password"
        data[password_name] = password
        return data
    csrf_name = page.name_for("csrftoken") or "csrftoken"
    data[csrf_name] = srp["csrf_token"]
    for identity, value in (("publicA", srp["public_a"]),
                            ("clientEvidenceM1", srp["client_evidence_m1"]),
                            ("useSrp", "true")):
        data[page.name_for(identity) or identity] = value
    password_name = page.name_for("password")
    if password_name:
        data[password_name] = "." * len(password)
    return data


def _raise_for_json(response, operation):
    if response.status_code != 200:
        raise LoginError(f"Battle.net {operation} failed with HTTP {response.status_code}.")
    try:
        return response.json()
    except ValueError:
        raise LoginError(f"Battle.net {operation} returned a non-JSON response.") from None


def _mfa_kind(location, html):
    marker = f"{location or ''}\n{html or ''}".lower()
    for kind, needle in (("authenticator", "authenticator"),
                         ("authenticator", "authcode"),
                         ("sms", "sms"),
                         ("email", "email-verify")):
        if needle in marker:
            return kind
    return None


def _submit_mfa(session, url, page, kind, code, timeout, headers):
    form = page.form_for("code") or page.form_for("authCode")
    if form is None and page.forms:
        form = page.forms[0]
    if form is None:
        raise LoginError(f"Battle.net requested {kind} verification but no form was found.")
    action = urljoin(url, form["attrs"].get("action") or "")
    data = page.form_fields(form)
    for identity in ("code", "authCode", "verificationCode", "otp"):
        name = page.name_for(identity)
        if name:
            data[name] = code
            break
    else:
        raise LoginError(f"Battle.net requested {kind} verification but no code field was found.")
    csrf = page.value_for("csrftoken")
    if csrf is not None:
        data[page.name_for("csrftoken") or "csrftoken"] = csrf
    return session.post(action, data=data, headers=headers, timeout=timeout, allow_redirects=False)


def follow_login_result(session, response, *, timeout, headers, mfa_provider, depth=0,
                        diagnostics=None):
    if depth > 4:
        raise LoginError("Too many redirects while completing the Battle.net login.")
    if diagnostics is not None:
        diagnostics.record("login-result" if depth == 0 else "login-redirect",
                           response=response, note=f"depth={depth}")
    location = response.headers.get("Location")
    token = extract_st_token(location) or extract_st_token(response.url)
    if token:
        return token
    if 300 <= response.status_code < 400 and location:
        if "localhost" in location:
            raise LoginError("The login callback did not carry an ST credential.")
        try:
            response = session.get(urljoin(response.url, location), headers=headers,
                                   timeout=timeout, allow_redirects=True)
        except Exception as exc:  # noqa: BLE001
            raise LoginError(f"Could not follow the Battle.net login redirect: {type(exc).__name__}.") from None
        return follow_login_result(session, response, timeout=timeout, headers=headers,
                                   mfa_provider=mfa_provider, depth=depth + 1,
                                   diagnostics=diagnostics)
    token = extract_st_token(response.text)
    if token:
        return token
    kind = _mfa_kind(None, response.text)
    if kind:
        if mfa_provider is None:
            raise LoginError(f"Battle.net requires {kind} verification; rerun with an MFA code available.")
        code = mfa_provider(kind)
        next_response = _submit_mfa(session, response.url, parse_login_page(response.text),
                                    kind, code, timeout, headers)
        return follow_login_result(session, next_response, timeout=timeout, headers=headers,
                                   mfa_provider=mfa_provider, depth=depth + 1,
                                   diagnostics=diagnostics)
    raise LoginError("Battle.net did not return a web credential (check the account and password).")


def web_login(email, password, region, session, *, locale=DEFAULT_LOCALE, timeout=DEFAULT_TIMEOUT,
              mfa_provider=None, login_url=None, diagnostics=None):
    """Run the SRP6a login and return the ST web credential bytes."""
    url = validate_challenge_url(login_url or default_login_url(region))
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": locale,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    }
    try:
        response = session.get(url, headers=headers, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        raise LoginError(f"Could not reach the Battle.net login page: {type(exc).__name__}.") from None
    if diagnostics is not None:
        diagnostics.record("login-page", method="GET", url=url, response=response)
    if response.status_code != 200:
        raise LoginError(f"The Battle.net login page returned HTTP {response.status_code}.")
    page = parse_login_page(response.text)
    if page.name_for("password") is None and page.name_for("accountName") is None:
        raise LoginError("The Battle.net login page did not contain a password form.")
    origin = "{0.scheme}://{0.netloc}".format(urlsplit(response.url if response.url else url))
    srp = None
    srp_marker = page.value_for("srpEnabled")
    if srp_marker is None or srp_marker.lower() != "true":
        raise LoginError("The login page does not advertise SRP; refusing to send a plain password.")
    if srp_marker.lower() == "true":
        request_headers = dict(headers)
        request_headers.update({"Accept": "application/json", "Content-Type": "application/json",
                                "Referer": response.url or url})
        if diagnostics is not None:
            diagnostics.record("srp-request", method="POST", url=origin + SRP_PATH,
                               request_fields={"account_name": email})
        try:
            srp_response = session.post(
                origin + SRP_PATH,
                data=json.dumps({"inputs": [{"input_id": "account_name", "value": email}]}),
                headers=request_headers, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            raise LoginError(f"Could not start the Battle.net SRP exchange: {type(exc).__name__}.") from None
        payload = _raise_for_json(srp_response, "SRP exchange")
        if diagnostics is not None:
            diagnostics.record("srp-response", response=srp_response)
            diagnostics.record_srp_payload(payload)
        required = ("csrf_token", "modulus", "generator", "hash_function", "version",
                    "iterations", "username", "salt", "public_B")
        missing = [key for key in required if key not in payload]
        if missing:
            raise LoginError("The Battle.net SRP response is missing required fields.")
        if str(payload.get("hash_function", "")).upper() not in ("SHA-256", "SHA256"):
            raise LoginError("Battle.net requested an unsupported SRP hash function.")
        public_a, evidence = srp_client_proof(
            payload["username"], password, payload["salt"], payload["modulus"],
            payload["generator"], payload["public_B"], int(payload["version"]),
            int(payload["iterations"]))
        srp = {"csrf_token": payload["csrf_token"], "public_a": public_a,
               "client_evidence_m1": evidence}
    action = None
    form = page.form_for("password")
    if form:
        action = form["attrs"].get("action") or None
    target = urljoin(response.url or url, action) if action else (response.url or url)
    data = build_login_form(page, email, password, srp)
    submit_headers = dict(headers)
    submit_headers.update({"Origin": origin, "Referer": response.url or url,
                           "Content-Type": "application/x-www-form-urlencoded"})
    if diagnostics is not None:
        diagnostics.record("login-form-request", method="POST", url=target, request_fields=data)
    try:
        submitted = session.post(target, data=data, headers=submit_headers, timeout=timeout,
                                 allow_redirects=False)
    except Exception as exc:  # noqa: BLE001
        raise LoginError(f"Could not submit the Battle.net login form: {type(exc).__name__}.") from None
    return follow_login_result(session, submitted, timeout=timeout, headers=headers,
                               mfa_provider=mfa_provider, diagnostics=diagnostics)


def logon_request(locale=DEFAULT_LOCALE, credential=None, sdk_version=None, device_id=None):
    body = (field(1, "S2") + field(2, "Wn64") + field(3, locale)
            + field(6, 0, 0) + field(10, 1, 0))
    if credential:
        body += field(12, credential)
    if sdk_version:
        body += field(5, sdk_version)
    if device_id:
        body += field(15, device_id)
    return body


def verify_web_credentials_body(credential):
    return field(1, credential)


def generate_web_credentials_body(program=S2_PROGRAM):
    return field(1, program, 5)


def challenge_from_body(body):
    payload_type = payload = None
    for number, wire, value in parse(body):
        if number == 2 and wire == 2:
            payload_type = value.decode("utf-8", "replace")
        elif number == 3 and wire == 2:
            payload = value.decode("utf-8", "replace")
    if payload_type != "web_auth_url" or not payload:
        raise LoginError("Battle.net did not return a web authentication URL.")
    return validate_challenge_url(payload)


def logon_error_from_body(body):
    for number, wire, value in parse(body):
        if number == 1 and wire == 0:
            return value
    return None


def credential_from_response(body):
    for number, wire, value in parse(body):
        if number == 1 and wire == 2 and value:
            return value
    raise LoginError("Battle.net returned no renewed web credential.")


def read_credentials(path):
    if str(path) == "-":
        lines = [line for line in sys.stdin.read().splitlines() if line]
        if len(lines) < 2:
            raise LoginError("Expected an email and a password on stdin.")
        return lines[0], lines[1]
    return load_credentials(path)

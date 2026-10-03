"""Offline tests for the integrated headless Battle.net login.

The SRP reference vectors in this module were produced offline by running the
editor's own cached webpack worker
(``0.srp6a-routines.worker.1zxOL.js`` from the CEF cache) under Node; no
Battle.net connection was made.  Every credential-shaped value here is a
synthetic placeholder.
"""

import hashlib

import pytest

from sc2publish import login

MODULUS = (
    "9455873662930925120643648891662386491044469586506477235214809370779867522817"
    "0106115630190094901096401883540229236016599430725894430734991444298272129143"
    "6818202738594707308777416292794257489272309963768335774065700890788234751207"
    "23855492588316592686203439138514838131581023312004481906611790561347740748686507"
)
SALT = "a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90"
USERNAME = "ACCOUNTNAME"
PASSWORD = "hunter2-hunter2"
PUBLIC_B = (
    "7d2f1c0a5b6e3f4a9c8d7e6f5a4b3c2d1e0f9a8b7c6d5e4f3a2b1c0d9e8f7a6b"
    "5c4d3e2f1a0b9c8d7e6f5a4b3c2d1e0f9a8b7c6d5e4f3a2b1c0d9e8f7a6b"
)
FIXED_A = 0x1234567890ABCDEF1234567890ABCDEF1234567890ABCDEF1234567890ABCDEF

X_V1 = 4713849749830163963613637483342333027239308041477396514269151252293968025199
X_V2 = (
    "6789902770710581929868508050159407723896957068569213606190968495992545"
    "4363920441458631117429843925490915749819294398550741275250272159421091"
    "4604396250266"
)
# The jsbn BigInteger used by the login bundle treats a byte array as signed
# two's-complement, so version-2 x is negative when PBKDF2's top bit is set.
# Oracle from the editor's cached worker (0.srp6a-routines.worker.1zxOL.js)
# under Node, same salt/username as above and password "hunter2-hunter22".
SIGNED_PASSWORD = "hunter2-hunter22"
X_V2_SIGNED = int(
    "-19975050688051479213204296592428209080691038471699186387363034458807759088624499997"
    "96559735488727378825945847364487575509766037455091731097799722484861741"
)
PUBLIC_A_ORACLE = (
    "600866fa7626d2babf9eac5673a779b961beed57bcea21f905a8e897d3d9c0eb6fb786"
    "db4d8af8ef62cc52f8b961c1add1afb24571643272d96e9317fb3edc6b1e1e395a1f4c"
    "43646de5be187e33781f245819840a56e0c9ce8c1cb5bde484e394a801088977784a0c"
    "0756c525e3de28c531904d7166be96bec064a2dde5388b824f3a11fa2e8e37e080c0c2"
    "15ea833200f2671af892a5406006"
)
M1_V1_ORACLE = "2d1c160f7b4c245b58568672aa331f6a4dee5595338260ff7ec214c3eb997850"
M1_V2_ORACLE = "78dc2c1252642330e51bea3bc9e36d571e0c0805b549d2ceb6b580389364279b"

TOKEN = b"EU-SYNTHETICTOKEN0123456789"
LOGIN_URL = "https://eu.account.battle.net/login/?externalChallenge=login&app=s2"

LOGIN_HTML = """<html><body>
<form id="password-form" method="post" action="/login/?externalChallenge=login&amp;app=s2">
<input type="hidden" name="csrftoken" id="csrftoken" value="csrf-page-value">
<input type="hidden" name="srpEnabled" id="srpEnabled" value="true">
<input type="text" name="accountName" id="accountName" value="">
<input type="password" name="password" id="password" value="">
<input type="hidden" name="publicA" id="publicA" value="">
<input type="hidden" name="clientEvidenceM1" id="clientEvidenceM1" value="">
<input type="hidden" name="useSrp" id="useSrp" value="">
<button id="submit" type="submit">Log In</button>
</form></body></html>"""

MFA_HTML = """<html><body>
<form id="authenticator-form" method="post" action="/login/authenticator">
<input type="hidden" name="csrftoken" value="csrf-mfa">
<input type="text" name="authCode" id="authCode" value="">
<button type="submit">Continue</button>
</form></body></html>"""


class FakeResponse:
    def __init__(self, status=200, url=LOGIN_URL, text="", headers=None, payload=None):
        self.status_code = status
        self.url = url
        self.text = text
        self.headers = headers or {}
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def _next(self, method, url, kwargs):
        self.calls.append((method, url, kwargs))
        if not self.script:
            raise AssertionError(f"unexpected {method} to {url}")
        return self.script.pop(0)

    def get(self, url, **kwargs):
        return self._next("GET", url, kwargs)

    def post(self, url, **kwargs):
        return self._next("POST", url, kwargs)

    def close(self):
        pass


class FakeWebSocket:
    def __init__(self, incoming):
        self.incoming = list(incoming)
        self.sent = []
        self.closed = False

    def send(self, payload, opcode=2):
        assert opcode == 2
        self.sent.append(bytes(payload))

    def recv(self):
        if not self.incoming:
            raise AssertionError("no scripted server frame")
        return self.incoming.pop(0)

    def close(self):
        self.closed = True


def server_frame(*, service=None, method=None, token=0, body=b"", status=None, response=False):
    header = login.field(1, 254 if response else 0, 0)
    if method is not None:
        header += login.field(2, method, 0)
    header += login.field(3, token, 0)
    if status is not None:
        header += login.field(6, status, 0)
    if service is not None:
        header += login.field(11, service, 5)
    return len(header).to_bytes(2, "big") + header + body


def test_password_parsing(tmp_path):
    path = tmp_path / "credentials.txt"
    path.write_text("placeholder@example.invalid\nnot-a-real-password\n")
    assert login.read_credentials(path) == ("placeholder@example.invalid", "not-a-real-password")


def test_password_parsing_rejects_incomplete(tmp_path):
    path = tmp_path / "credentials.txt"
    path.write_text("placeholder@example.invalid\n")
    with pytest.raises(ValueError):
        login.read_credentials(path)


def test_x_version1_matches_reference_oracle():
    assert login.compute_x(USERNAME, PASSWORD, SALT, 1, 1) == X_V1


def test_x_version2_matches_reference_oracle():
    assert login.compute_x(USERNAME, PASSWORD, SALT, 2, 15000) == int(X_V2)


def test_full_srp_proof_matches_reference_oracle():
    public_a, evidence = login.srp_client_proof(
        USERNAME, PASSWORD, SALT, MODULUS, "2", PUBLIC_B, 1, 1, private_a=FIXED_A)
    assert public_a == PUBLIC_A_ORACLE
    assert evidence == M1_V1_ORACLE


def test_full_srp_proof_version2_matches_reference_oracle():
    public_a, evidence = login.srp_client_proof(
        USERNAME, PASSWORD, SALT, MODULUS, "2", PUBLIC_B, 2, 15000, private_a=FIXED_A)
    assert public_a == PUBLIC_A_ORACLE
    assert evidence == M1_V2_ORACLE


def test_x_version2_signed_matches_reference_oracle():
    assert login.compute_x(USERNAME, SIGNED_PASSWORD, SALT, 2, 15000) == X_V2_SIGNED


def test_negative_x_proof_agrees_with_server_simulation():
    modulus = int(MODULUS, 16)
    generator = 2
    chars = (modulus.bit_length() + 3) // 4
    x = login.compute_x(USERNAME, SIGNED_PASSWORD, SALT, 2, 15000)
    assert x < 0
    verifier = pow(generator, x, modulus)
    server_b = FIXED_A
    k = int.from_bytes(hashlib.sha256(bytes.fromhex(
        login.padded_hex(modulus, chars)
        + login.padded_hex(generator, chars))).digest(), "big") & ((1 << 256) - 1)
    public_b = (k * verifier + pow(generator, server_b, modulus)) % modulus
    public_a, evidence = login.srp_client_proof(
        USERNAME, SIGNED_PASSWORD, SALT, MODULUS, "2", format(public_b, "x"),
        2, 15000, private_a=FIXED_A)
    u = int.from_bytes(hashlib.sha256(bytes.fromhex(
        login.padded_hex(int(public_a, 16), chars)
        + login.padded_hex(public_b, chars))).digest(), "big") & ((1 << 256) - 1)
    shared = pow(int(public_a, 16) * pow(verifier, u, modulus) % modulus, server_b, modulus)
    expected = hashlib.sha256(
        login.bigint_to_signed_bytes(int(public_a, 16))
        + login.bigint_to_signed_bytes(public_b)
        + login.bigint_to_signed_bytes(shared)).hexdigest()
    assert evidence == expected


def test_srp_rejects_invalid_public_value():
    with pytest.raises(login.LoginError):
        login.srp_client_proof(USERNAME, PASSWORD, SALT, MODULUS, "2", "1", 1, 1, private_a=FIXED_A)


def test_login_page_parsing():
    page = login.parse_login_page(LOGIN_HTML)
    assert page.name_for("password") == "password"
    assert page.name_for("accountName") == "accountName"
    assert page.value_for("srpEnabled") == "true"
    form = page.form_for("password")
    assert form["attrs"]["action"] == "/login/?externalChallenge=login&app=s2"


def test_build_login_form_overrides_srp_fields():
    page = login.parse_login_page(LOGIN_HTML)
    srp = {"csrf_token": "fresh", "public_a": "0a1b", "client_evidence_m1": "dead"}
    data = login.build_login_form(page, "placeholder@example.invalid", PASSWORD, srp)
    assert data["csrftoken"] == "fresh"
    assert data["accountName"] == "placeholder@example.invalid"
    assert data["publicA"] == "0a1b"
    assert data["clientEvidenceM1"] == "dead"
    assert data["useSrp"] == "true"
    assert data["password"] == "." * len(PASSWORD)
    assert PASSWORD not in data.values()


def test_web_login_flow_with_fake_session():
    srp_payload = {
        "csrf_token": "csrf-srp",
        "modulus": MODULUS,
        "generator": "2",
        "hash_function": "SHA-256",
        "version": 1,
        "iterations": 1,
        "username": USERNAME,
        "salt": SALT,
        "public_B": PUBLIC_B,
        "eligible_credential_upgrade": False,
    }
    session = FakeSession([
        FakeResponse(text=LOGIN_HTML),
        FakeResponse(payload=srp_payload),
        FakeResponse(status=302, headers={
            "Location": f"http://localhost:0/?ST={TOKEN.decode()}&flowTrackingId=x"}),
    ])
    credential = login.web_login("placeholder@example.invalid", PASSWORD, "EU", session)
    assert credential == TOKEN
    kinds = [call[0] for call in session.calls]
    assert kinds == ["GET", "POST", "POST"]
    form = session.calls[2][2]["data"]
    assert form["csrftoken"] == "csrf-srp"
    assert form["publicA"] and form["clientEvidenceM1"]
    assert form["password"] != PASSWORD


def test_web_login_requires_srp_marker():
    html = LOGIN_HTML.replace('<input type="hidden" name="srpEnabled" id="srpEnabled" value="true">', "")
    session = FakeSession([FakeResponse(text=html)])
    with pytest.raises(login.LoginError):
        login.web_login("placeholder@example.invalid", PASSWORD, "EU", session)
    assert len(session.calls) == 1


def test_web_login_rejects_disabled_srp():
    session = FakeSession([FakeResponse(text=LOGIN_HTML.replace('value="true"', 'value="false"'))])
    with pytest.raises(login.LoginError):
        login.web_login('placeholder@example.invalid', PASSWORD, 'EU', session)
    assert len(session.calls) == 1


def test_web_login_mfa_flow():
    srp_payload = {
        "csrf_token": "csrf-srp",
        "modulus": MODULUS,
        "generator": "2",
        "hash_function": "SHA-256",
        "version": 1,
        "iterations": 1,
        "username": USERNAME,
        "salt": SALT,
        "public_B": PUBLIC_B,
    }
    seen = []

    def provider(kind):
        seen.append(kind)
        return "123456"

    session = FakeSession([
        FakeResponse(text=LOGIN_HTML),
        FakeResponse(payload=srp_payload),
        FakeResponse(text=MFA_HTML),
        FakeResponse(status=302, headers={"Location": f"http://localhost:0/?ST={TOKEN.decode()}"}),
    ])
    credential = login.web_login("placeholder@example.invalid", PASSWORD, "EU", session,
                                 mfa_provider=provider)
    assert credential == TOKEN
    assert seen == ["authenticator"]
    assert session.calls[3][2]["data"]["authCode"] == "123456"


def test_web_login_mfa_without_provider_fails():
    srp_payload = {
        "csrf_token": "c", "modulus": MODULUS, "generator": "2", "hash_function": "SHA-256",
        "version": 1, "iterations": 1, "username": USERNAME, "salt": SALT, "public_B": PUBLIC_B,
    }
    session = FakeSession([
        FakeResponse(text=LOGIN_HTML),
        FakeResponse(payload=srp_payload),
        FakeResponse(text=MFA_HTML),
    ])
    with pytest.raises(login.LoginError):
        login.web_login("placeholder@example.invalid", PASSWORD, "EU", session)


def test_logon_request_omits_credential_and_names_program():
    body = login.logon_request("enUS")
    values = {number: value for number, _, value in login.parse(body)}
    assert values[1] == b"S2"
    assert values[2] == b"Wn64"
    assert values[3] == b"enUS"
    assert 12 not in values


def test_logon_request_includes_cached_credential():
    body = login.logon_request("enUS", credential=b"EU-placeholder")
    values = {number: value for number, _, value in login.parse(body)}
    assert values[12] == b"EU-placeholder"


def test_verify_and_generate_bodies():
    assert login.verify_web_credentials_body(TOKEN) == login.field(1, TOKEN)
    assert login.generate_web_credentials_body() == login.field(1, login.S2_PROGRAM, 5)


def test_challenge_parsing_rejects_foreign_hosts():
    good = login.field(2, "web_auth_url") + login.field(3, LOGIN_URL)
    assert login.challenge_from_body(good) == LOGIN_URL
    for bad in ("https://evil.example/login", "http://eu.account.battle.net/login/",
                "https://account.battle.net.evil.example/login/"):
        with pytest.raises(login.LoginError):
            login.challenge_from_body(login.field(2, "web_auth_url") + login.field(3, bad))


def test_logon_result_and_renewed_credential_parsing():
    assert login.logon_error_from_body(login.field(1, 0, 0)) == 0
    assert login.logon_error_from_body(login.field(1, 7, 0)) == 7
    assert login.credential_from_response(login.field(1, b"EU-renewed")) == b"EU-renewed"


def test_extract_st_token_and_shape():
    assert login.extract_st_token("http://localhost:0/?ST=EU-abc&flow=1") == b"EU-abc"
    assert login.extract_st_token("http://localhost:0/?flow=1") is None
    shape = login.credential_shape(TOKEN)
    assert TOKEN.decode() not in str(shape)
    assert shape['region_prefix'] == 'EU-'

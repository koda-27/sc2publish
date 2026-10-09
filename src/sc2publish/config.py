"""Project manifests and private per-user account storage."""
import json
import os
import re
import tempfile
import tomllib
from pathlib import Path

from platformdirs import user_config_path, user_state_path

from .errors import PublishError


def config_home():
    return Path(os.environ.get("SC2PUBLISH_CONFIG_HOME", user_config_path("sc2publish")))


def private_write(path, data):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".write-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data.encode() if isinstance(data, str) else data)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


ACCOUNT_KEYS = ("web_credential", "web_credential_file", "toon_name", "realm",
                "region", "game_account_region", "sdk_version", "device_id",
                "email", "password", "credentials_file", "credential_email")


def parse_dotenv(text):
    """Parse a small KEY=VALUE file; comments, quoting, and 'export' allowed."""
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[7:].lstrip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].rstrip()
        values[key] = value
    return values


def account_values(values):
    result = {}
    for key, value in values.items():
        name = key[11:].lower() if key.startswith("SC2PUBLISH_") else key.lower()
        if name in ACCOUNT_KEYS:
            result[name] = value
    return result


def resolve_env_file(project, explicit=None):
    chosen = explicit or os.environ.get("SC2PUBLISH_ENV")
    if chosen:
        path = Path(chosen).expanduser()
        return path.resolve() if path.is_file() else None
    candidates = [project.base / ".env" if project else None,
                  Path.cwd() / ".env",
                  config_home() / ".env"]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate).resolve()
    return None


def env_file_target(project, explicit=None):
    chosen = explicit or os.environ.get("SC2PUBLISH_ENV")
    if chosen:
        return Path(chosen).expanduser().resolve()
    return (config_home() / ".env").resolve()


def load_account(project, explicit=None):
    """Account settings come only from a .env file and the process environment."""
    path = resolve_env_file(project, explicit)
    values = account_values(parse_dotenv(path.read_text(encoding="utf-8-sig"))) if path else {}
    values.update(account_values(os.environ))
    for key in ("realm", "game_account_region"):
        if key in values:
            try:
                values[key] = int(values[key])
            except (TypeError, ValueError):
                raise PublishError(f"SC2PUBLISH_{key.upper()} must be an integer.") from None
    if values.get("web_credential_file") and not values.get("web_credential"):
        try:
            import base64
            values["web_credential"] = base64.b64encode(
                Path(values["web_credential_file"]).expanduser().read_bytes()).decode()
        except OSError as exc:
            raise PublishError(f"Cannot read SC2PUBLISH_WEB_CREDENTIAL_FILE: {exc}") from None
    return values, path or env_file_target(project, explicit)


def update_env_file(path, updates):
    path = Path(path)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(updates)
    result = []
    for line in lines:
        stripped = line.strip().removeprefix("export ").lstrip()
        key = stripped.split("=", 1)[0].strip() if "=" in stripped else None
        name = key[11:].lower() if key and key.startswith("SC2PUBLISH_") else None
        if name in remaining:
            result.append(f"SC2PUBLISH_{name.upper()}={remaining.pop(name)}")
        else:
            result.append(line)
    if result and result[-1].strip():
        result.append("")
    result.extend(f"SC2PUBLISH_{name.upper()}={value}" for name, value in remaining.items())
    private_write(path, "\n".join(result) + "\n")


class Project:
    def __init__(self, selector=None):
        selector = selector or os.environ.get("SC2PUBLISH_PROJECT")
        if selector:
            path = Path(selector).expanduser()
            if not path.exists() and re.fullmatch(r"[A-Za-z0-9_-]+", selector):
                path = config_home() / "projects" / (selector + ".toml")
        else:
            path = next((p / "sc2publish.toml" for p in (Path.cwd(), *Path.cwd().parents)
                         if (p / "sc2publish.toml").is_file()), Path("sc2publish.toml"))
        try:
            self.path = path.resolve(strict=True)
            self.data = tomllib.loads(self.path.read_text())
        except (FileNotFoundError, tomllib.TOMLDecodeError) as exc:
            raise PublishError(f"Cannot load project {path}: {exc}") from None
        self.base = self.path.parent
        self.project = self.data.get("project", {})
        self.build = self.data.get("build", {})
        self.publish = self.data.get("publish", {})
        if self.publish.get("metadata", "compiled") != "compiled":
            raise PublishError('Metadata is now compiled from source. Remove [publish].metadata from the manifest.')
        self.name = self.project.get("name", "")
        self.region = self.project.get("region", "EU").upper()
        self.locale = self.project.get("locale", "enUS")
        if not self.name or len(self.name) > 80:
            raise PublishError("Set [project].name to a map name of 1–80 characters.")
        # Earlier listing names, so a renamed map still resolves its existing listing.
        self.previous_names = list(self.project.get("previous_names", []))
        if len(self.previous_names) > 15 or any(not isinstance(n, str) or not n or len(n) > 80
                                                for n in self.previous_names):
            raise PublishError("[project].previous_names must list at most 15 map names of 1–80 characters.")
        if self.region not in ("EU", "US", "KR"):
            raise PublishError("Supported regions: EU, US, KR.")
        if not re.fullmatch(r"[a-z]{2}[A-Z]{2}", self.locale):
            raise PublishError("Locale must use a four-letter code such as enUS.")
        self.map_id = int(self.project.get("map_id", 0))
        self.namespace = int(self.project.get("namespace", 0))
        if not 0 <= self.map_id <= 0xffffffff or not 0 <= self.namespace <= 0xffffffff:
            raise PublishError("map_id and namespace must be unsigned 32-bit integers, or omitted for discovery.")
        self.output = self.resolve(self.build.get("output", "build"))
        self.layers = [self.resolve(v) for v in self.build.get("layers", [])]
        if not self.layers or any(not p.is_dir() for p in self.layers):
            raise PublishError("[build].layers must list existing component directories, with the map last.")
        if any(self.output == p or self.output in p.parents or p in self.output.parents for p in self.layers):
            raise PublishError("Build output must be separate from the input component directories.")
        stem = re.sub(r"[^A-Za-z0-9_-]", "", self.name) or "Map"
        self.component_dir = self.output / "components" / (stem + ".SC2Map")
        self.archive = self.output / (stem + ".SC2Map")

    def resolve(self, value):
        path = Path(os.path.expandvars(value)).expanduser()
        return (self.base / path).resolve() if not path.is_absolute() else path.resolve()

    @property
    def state_path(self):
        import hashlib
        key = hashlib.sha256(str(self.path).encode()).hexdigest()[:20]
        return user_state_path("sc2publish") / (key + ".json")

    def read_state(self):
        return json.loads(self.state_path.read_text()) if self.state_path.exists() else {}

    def save_state(self, value):
        private_write(self.state_path, json.dumps(value, indent=2) + "\n")

"""Read user-supplied Battle.net credentials without logging their contents."""

import json
import re
from pathlib import Path


def load_credentials(path):
    content = Path(path).read_text(encoding="utf-8-sig")
    values = {}
    try:
        data = json.loads(content)
        if isinstance(data, dict):
            values = {key.lower(): str(value) for key, value in data.items()}
    except json.JSONDecodeError:
        pass
    lines = [line for line in content.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if not values:
        for line in lines:
            match = re.match(r"^\s*(email|username|login|account|password|pass)\s*[:=]\s*(.*)$", line, re.I)
            if match:
                values[match[1].lower()] = match[2]
    if not values and len(lines) == 2:
        values = {"email": lines[0], "password": lines[1]}
    if not values and len(lines) == 1:
        parts = lines[0].split(maxsplit=1)
        if len(parts) == 2 and "@" in parts[0]:
            values = {"email": parts[0], "password": parts[1]}
    if not values and len(lines) == 3 and "@" in lines[1] and "@" not in lines[0]:
        # Provider heading, email, password.
        values = {"email": lines[1], "password": lines[2]}
    email = next((values[key].strip() for key in ("email", "username", "login", "account") if values.get(key)), None)
    password = next((values[key] for key in ("password", "pass") if values.get(key)), None)
    if not email or not password:
        raise ValueError("Expected email/password fields, JSON, or two lines (email then password)")
    return email, password

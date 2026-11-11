from dataclasses import dataclass, field
import json
from pathlib import Path
from urllib.parse import urlsplit

from config import (SEEDANCE_MODEL, SEEDANCE_BASE_URL, SEEDANCE_AUTH_FILE,
                    SEEDANCE_POLL_INTERVAL, SEEDANCE_TIMEOUT_SECONDS)


@dataclass(frozen=True)
class SeedanceSettings:
    seedance_base_url: str
    seedance_model: str
    seedance_api_key: str = field(repr=False)
    poll_interval: int = SEEDANCE_POLL_INTERVAL
    timeout_seconds: int = SEEDANCE_TIMEOUT_SECONDS

    @classmethod
    def load(cls) -> "SeedanceSettings":
        if not isinstance(SEEDANCE_MODEL, str) or not SEEDANCE_MODEL.strip():
            raise ValueError("SEEDANCE_MODEL in config.py must not be empty.")
        url = SEEDANCE_BASE_URL.strip().rstrip("/")
        try:
            parts = urlsplit(url)
            valid = (parts.scheme == "https" and bool(parts.hostname)
                     and parts.username is None and parts.password is None
                     and not parts.query and not parts.fragment
                     and not any(c.isspace() or ord(c) < 32 for c in url))
            parts.port
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("SEEDANCE_BASE_URL must be an HTTPS API root URL.")
        for name, value in (("SEEDANCE_POLL_INTERVAL", SEEDANCE_POLL_INTERVAL),
                            ("SEEDANCE_TIMEOUT_SECONDS", SEEDANCE_TIMEOUT_SECONDS)):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer.")
        try:
            auth = json.loads(Path(SEEDANCE_AUTH_FILE).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, UnicodeError):
            raise ValueError("Unable to read the credentials file specified by SEEDANCE_AUTH_FILE in config.py.") from None
        if not isinstance(auth, dict):
            raise ValueError("The Seedance credentials file must contain a JSON object.")
        key = auth.get("SEEDANCE_API_KEY") or auth.get("api_key")
        if (not isinstance(key, str) or not key or key == "REPLACE_WITH_YOUR_API_KEY"
                or not key.isascii() or any(c.isspace() or ord(c) < 32 for c in key)):
            raise ValueError("The Seedance credentials file does not contain a valid API key.")
        return cls(url, SEEDANCE_MODEL.strip(), key,
                   SEEDANCE_POLL_INTERVAL, SEEDANCE_TIMEOUT_SECONDS)

"""
Environment loader — reads .env file and loads all keys.
Called once at startup before anything else.

No external dependencies — pure Python stdlib.
python-dotenv is popular but we don't need it.
"""

import os
from pathlib import Path


def load_env(env_path: Path | None = None) -> None:
    """
    Load .env file into os.environ.
    Searches for .env in:
      1. provided path
      2. current working directory
      3. project root (where main.py lives)

    Does NOT override existing env vars —
    real environment always wins over .env file.
    """
    candidates = []

    if env_path:
        candidates.append(Path(env_path))

    candidates.append(Path.cwd() / ".env")
    candidates.append(Path(__file__).parent.parent / ".env")

    for path in candidates:
        if path.exists():
            _parse_and_load(path)
            return


def _parse_and_load(path: Path) -> None:
    """Parse a .env file and load into os.environ."""
    with open(path, encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()

            # skip empty lines and comments
            if not line or line.startswith("#"):
                continue

            # must be KEY=VALUE format
            if "=" not in line:
                continue

            key, _, value = line.partition("=")
            key   = key.strip()
            value = value.strip()

            # strip inline comments
            if " #" in value:
                value = value[:value.index(" #")].strip()

            # strip surrounding quotes
            if len(value) >= 2:
                if (value.startswith('"') and value.endswith('"')) or \
                   (value.startswith("'") and value.endswith("'")):
                    value = value[1:-1]

            # skip empty values — don't overwrite with empty string
            if not value or value.startswith("your-"):
                continue

            # real environment always wins
            if key not in os.environ:
                os.environ[key] = value


def get(key: str, default: str = "") -> str:
    """Get an env var with optional default."""
    return os.environ.get(key, default)


def require(key: str) -> str:
    """Get an env var, raise if missing."""
    value = os.environ.get(key)
    if not value:
        raise ValueError(
            f"Required environment variable '{key}' is not set.\n"
            f"Add it to your .env file or export it in your shell."
        )
    return value

"""Shared test setup.

The app reads `data/` and `templates/` relative to the working directory and validates
required settings at import time, so both are prepared here before any `src` import.
Variables already set in the environment take precedence over these placeholders.
"""
import os
import secrets
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
os.chdir(REPO_ROOT)

_REQUIRED_SETTINGS = {
    "DATABASE_URL": "postgresql+asyncpg://localhost:5432/cbp_test",
    "GOOGLE_PROJECT_LOCATION": "asia-south1",
    "GOOGLE_APPLICATION_CREDENTIALS": str(REPO_ROOT / "tests" / "unused-credentials.json"),
    "GOOGLE_API_KEY": secrets.token_hex(16),
    "GOOGLE_PROJECT_ID": "cbp-test-project",
    "KB_BASE_URL": "https://kb.example.test",
    "KB_AUTH_TOKEN": secrets.token_hex(16),
    "SECRET_KEY": secrets.token_hex(32),
}
for name, value in _REQUIRED_SETTINGS.items():
    os.environ.setdefault(name, value)

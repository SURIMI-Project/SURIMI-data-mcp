"""Where the server finds its files: the application root holds static/, data/ and audit-repro/.

In the image that is /app, and in a checkout it is the repository root. Modules build file paths from
`APP_ROOT`, not from their own location.
"""
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent

"""YouTube OAuth authentication."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from src.config import PROJECT_ROOT, SECRETS_DIR, get_env

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
TOKEN_PATH = SECRETS_DIR / "token.json"


def _client_secrets_path() -> Path:
    path = Path(get_env("YOUTUBE_CLIENT_SECRETS", str(SECRETS_DIR / "client_secrets.json")))
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def get_credentials() -> Credentials:
    refresh_token = get_env("YOUTUBE_REFRESH_TOKEN")
    client_path = _client_secrets_path()

    creds: Credentials | None = None

    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
        return creds

    if creds and creds.valid:
        return creds

    if refresh_token and client_path.exists():
        with client_path.open(encoding="utf-8") as f:
            client_config = json.load(f)
        installed = client_config.get("installed") or client_config.get("web", {})
        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri=installed.get("token_uri", "https://oauth2.googleapis.com/token"),
            client_id=installed["client_id"],
            client_secret=installed["client_secret"],
            scopes=SCOPES,
        )
        creds.refresh(Request())
        TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
        return creds

    if not client_path.exists():
        raise FileNotFoundError(
            f"YouTube client secrets not found at {client_path}. "
            "Download OAuth credentials from Google Cloud Console."
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(client_path), SCOPES)
    creds = flow.run_local_server(port=0)
    TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    logger.info("OAuth complete. Token saved to %s", TOKEN_PATH)
    if creds.refresh_token:
        logger.info("Add this to .env as YOUTUBE_REFRESH_TOKEN=%s", creds.refresh_token)
    return creds


def main() -> None:
    """Run OAuth flow: python -m src.youtube.auth"""
    logging.basicConfig(level=logging.INFO)
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    creds = get_credentials()
    print("Authenticated successfully.")
    print(f"Token saved to: {TOKEN_PATH}")
    if creds.refresh_token:
        print(f"YOUTUBE_REFRESH_TOKEN={creds.refresh_token}")


if __name__ == "__main__":
    main()

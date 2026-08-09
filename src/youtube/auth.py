"""YouTube OAuth authentication."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from google.auth.exceptions import RefreshError
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


def _is_invalid_grant(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "invalid_grant" in text or "expired or revoked" in text


def _save_token(creds: Credentials) -> None:
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")


def _clear_stale_token() -> None:
    if TOKEN_PATH.exists():
        TOKEN_PATH.unlink()
        logger.warning("Removed stale YouTube token at %s — re-auth required", TOKEN_PATH)


def _refresh_or_raise(creds: Credentials) -> Credentials:
    creds.refresh(Request())
    _save_token(creds)
    return creds


def get_credentials() -> Credentials:
    refresh_token = get_env("YOUTUBE_REFRESH_TOKEN")
    client_path = _client_secrets_path()

    creds: Credentials | None = None

    if TOKEN_PATH.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not load token.json (%s) — will re-auth", exc)
            _clear_stale_token()
            creds = None

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            return _refresh_or_raise(creds)
        except RefreshError as exc:
            if not _is_invalid_grant(exc):
                raise
            logger.warning("YouTube token refresh failed (invalid_grant): %s", exc)
            _clear_stale_token()
            creds = None

    if refresh_token and client_path.exists():
        try:
            with client_path.open(encoding="utf-8") as f:
                client_config = json.load(f)
            installed = client_config.get("installed") or client_config.get("web", {})
            creds = Credentials(
                token=None,
                refresh_token=refresh_token,
                token_uri=installed.get(
                    "token_uri", "https://oauth2.googleapis.com/token"
                ),
                client_id=installed["client_id"],
                client_secret=installed["client_secret"],
                scopes=SCOPES,
            )
            return _refresh_or_raise(creds)
        except RefreshError as exc:
            if not _is_invalid_grant(exc):
                raise
            logger.warning(
                "YOUTUBE_REFRESH_TOKEN is expired/revoked — starting browser OAuth"
            )
            creds = None

    if not client_path.exists():
        raise FileNotFoundError(
            f"YouTube client secrets not found at {client_path}. "
            "Download OAuth credentials from Google Cloud Console."
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(client_path), SCOPES)
    creds = flow.run_local_server(port=0)
    _save_token(creds)
    logger.info("OAuth complete. Token saved to %s", TOKEN_PATH)
    if creds.refresh_token:
        logger.info("Add this to .env as YOUTUBE_REFRESH_TOKEN=%s", creds.refresh_token)
    return creds


def main() -> None:
    """Run OAuth flow: python -m src.youtube.auth"""
    logging.basicConfig(level=logging.INFO)
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    # Force a clean browser login when the stored grant is dead.
    if TOKEN_PATH.exists():
        try:
            probe = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
            if probe and probe.expired and probe.refresh_token:
                probe.refresh(Request())
                _save_token(probe)
                print("Existing token refreshed successfully.")
                print(f"Token saved to: {TOKEN_PATH}")
                return
            if probe and probe.valid:
                print("Existing token is still valid.")
                print(f"Token saved to: {TOKEN_PATH}")
                return
        except RefreshError as exc:
            if _is_invalid_grant(exc):
                logger.warning("Stored token invalid — clearing and opening browser login")
                _clear_stale_token()
            else:
                raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not use existing token (%s) — re-auth", exc)
            _clear_stale_token()

    creds = get_credentials()
    print("Authenticated successfully.")
    print(f"Token saved to: {TOKEN_PATH}")
    if creds.refresh_token:
        print(f"YOUTUBE_REFRESH_TOKEN={creds.refresh_token}")
        print("Update .env with the refresh token above (shared with trends pipeline).")


if __name__ == "__main__":
    main()

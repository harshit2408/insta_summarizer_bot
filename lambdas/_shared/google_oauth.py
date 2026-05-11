"""
Google OAuth 2.0 helpers — Web-application flow, stdlib only.

Two operations only:

  1. ``build_authorization_url(...)``
       Construct the Google consent screen URL the user opens via Telegram.
       The bot generates a unique ``state`` token tied to the chat_id so we
       can authenticate the callback request.

  2. ``exchange_code_for_tokens(...)`` / ``refresh_access_token(...)``
       POST to https://oauth2.googleapis.com/token with the standard form
       payload. The full ``google-auth-oauthlib`` SDK pulls in cryptography
       + a vendored httplib2 (~30 MB unzipped) — overkill for two HTTP calls.

The Lambda only stores the **refresh token** (encrypted via KMS). Access
tokens are obtained on demand right before each Google Docs API call —
they're short-lived (~1 hour) and never persisted.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

logger = logging.getLogger(__name__)


GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

# Scopes required to create a doc, read its current contents (so we can
# locate sections), and append new entries. ``documents`` covers all three —
# we don't need the broader ``drive`` scope.
DEFAULT_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.file",
)


class OAuthError(RuntimeError):
    """Any failure communicating with Google's OAuth endpoints."""


@dataclass
class TokenBundle:
    """Subset of Google's token-endpoint response we care about."""

    access_token: str
    expires_in: int
    refresh_token: str | None
    scope: str
    token_type: str

    @classmethod
    def from_response(cls, payload: dict) -> "TokenBundle":
        return cls(
            access_token=payload["access_token"],
            expires_in=int(payload.get("expires_in", 3600)),
            refresh_token=payload.get("refresh_token"),
            scope=payload.get("scope", ""),
            token_type=payload.get("token_type", "Bearer"),
        )


# ── Public API ───────────────────────────────────────────────────────────────


def build_authorization_url(
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    scopes: tuple[str, ...] = DEFAULT_SCOPES,
) -> str:
    """Build the URL to send the user to for consent.

    ``access_type=offline`` and ``prompt=consent`` together guarantee that
    Google returns a refresh token even on re-authorisation.
    """
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline",
        "include_granted_scopes": "true",
        "prompt": "consent",
        "state": state,
    }
    return f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"


def exchange_code_for_tokens(
    *,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
) -> TokenBundle:
    """Trade an authorization code for an access + refresh token bundle."""
    payload = urllib.parse.urlencode(
        {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }
    ).encode("utf-8")

    return TokenBundle.from_response(_post_form(GOOGLE_TOKEN_URL, payload))


def refresh_access_token(
    *,
    client_id: str,
    client_secret: str,
    refresh_token: str,
) -> TokenBundle:
    """Use a stored refresh token to mint a fresh access token.

    The response from this endpoint never contains a new refresh token —
    callers should keep using the original refresh token and only update
    the cached access token.
    """
    payload = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
    ).encode("utf-8")

    return TokenBundle.from_response(_post_form(GOOGLE_TOKEN_URL, payload))


# ── Internal ─────────────────────────────────────────────────────────────────


def _post_form(url: str, body: bytes, *, timeout: float = 15.0) -> dict:
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        err = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        logger.error("Google OAuth HTTP %s: %s", exc.code, err[:300])
        # Bubble up a more specific error for 'invalid_grant' which means the
        # refresh token has been revoked (user disconnected, password changed,
        # token > 6 months unused). Caller should prompt re-authorisation.
        if "invalid_grant" in err:
            raise OAuthError(
                "Google refresh token is invalid or revoked — user must re-authorise."
            ) from exc
        raise OAuthError(f"Google OAuth HTTP {exc.code}: {err[:200]}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise OAuthError(f"Network error talking to Google OAuth: {exc}") from exc

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise OAuthError(f"Non-JSON OAuth response: {raw[:200]}") from exc

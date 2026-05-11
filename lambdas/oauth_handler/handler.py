"""
OAuth Handler Lambda — Google account linking for Telegram users.

Two API Gateway routes share this Lambda:

  GET /oauth/start?chat_id=<id>
      Generates a per-user signed ``state`` token, builds the Google consent
      URL, and 302-redirects the browser to it. The Telegram bot sends the
      user this link during onboarding.

  GET /oauth/callback?code=<...>&state=<...>
      Handles Google's redirect after consent. Verifies the state, exchanges
      the code for a refresh token, encrypts that token with KMS, and writes
      it to the Users table. Sends a Telegram confirmation message and
      renders a tiny HTML success page.

State token format
------------------
``<chat_id>.<unix_ts>.<hmac-sha256(chat_id|ts, OAUTH_STATE_SECRET)>``

We sign the chat_id+timestamp so the callback can be sure (a) the redirect
came from a /start we generated, and (b) it hasn't been tampered with.
A 30-minute TTL prevents replay of stale links.

All AWS interactions use boto3 (built-in) and Google interactions use urllib.
No external dependencies.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import logging
import os
import time
import urllib.parse
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

from google_oauth import (
    OAuthError,
    build_authorization_url,
    exchange_code_for_tokens,
)
from kms_helper import encrypt_refresh_token, TokenEncryptionError
from telegram import send_message

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# ── Environment ──────────────────────────────────────────────────────────────
GOOGLE_CLIENT_ID = os.environ["GOOGLE_CLIENT_ID"]
GOOGLE_CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]
GOOGLE_REDIRECT_URI = os.environ["GOOGLE_REDIRECT_URI"]
OAUTH_STATE_SECRET = os.environ["OAUTH_STATE_SECRET"]

DYNAMODB_USERS_TABLE = os.environ["DYNAMODB_USERS_TABLE"]
KMS_KEY_ID = os.environ["KMS_KEY_ID"]
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

STATE_TTL_SECONDS = int(os.environ.get("OAUTH_STATE_TTL", "1800"))  # 30 min default

# ── AWS clients (module level → reused across warm invocations) ──────────────
_region = os.environ.get("AWS_REGION", "ap-south-1")
_dynamodb = boto3.resource("dynamodb", region_name=_region)
_kms = boto3.client("kms", region_name=_region)


# ─────────────────────────────────────────────────────────────────────────────
# Lambda entry point — API Gateway HTTP v2
# ─────────────────────────────────────────────────────────────────────────────

def lambda_handler(event: dict, context) -> dict:
    """Routes by ``rawPath`` so a single Lambda serves both /start and /callback."""
    path = event.get("rawPath") or ""
    qs = event.get("queryStringParameters") or {}
    logger.info("OAuth request path=%s qs=%s", path, _redact(qs))

    try:
        if path.endswith("/oauth/start"):
            return _handle_start(qs)
        if path.endswith("/oauth/callback"):
            return _handle_callback(qs)
    except Exception:
        logger.exception("OAuth handler error path=%s", path)
        return _html_response(500, _error_page("Something went wrong. Please try again."))

    return _html_response(404, _error_page("Not found."))


# ─────────────────────────────────────────────────────────────────────────────
# /oauth/start — issue signed state + redirect to Google
# ─────────────────────────────────────────────────────────────────────────────

def _handle_start(qs: dict) -> dict:
    chat_id = (qs.get("chat_id") or "").strip()
    if not chat_id or not chat_id.lstrip("-").isdigit():
        return _html_response(400, _error_page(
            "Missing or invalid chat_id. Open this link from the Telegram bot."
        ))

    state = _sign_state(chat_id)
    auth_url = build_authorization_url(
        client_id=GOOGLE_CLIENT_ID,
        redirect_uri=GOOGLE_REDIRECT_URI,
        state=state,
    )
    logger.info("Issued OAuth start for chat_id=%s", chat_id)

    return {
        "statusCode": 302,
        "headers": {"Location": auth_url},
        "body": "",
    }


# ─────────────────────────────────────────────────────────────────────────────
# /oauth/callback — code → tokens → encrypted store
# ─────────────────────────────────────────────────────────────────────────────

def _handle_callback(qs: dict) -> dict:
    error = qs.get("error")
    if error:
        logger.warning("Google returned OAuth error=%s", error)
        return _html_response(400, _error_page(
            f"Google declined the authorization request ({html.escape(error)}). "
            "Return to Telegram and try again."
        ))

    code = qs.get("code", "")
    state = qs.get("state", "")
    if not code or not state:
        return _html_response(400, _error_page("Missing code or state parameter."))

    chat_id = _verify_state(state)
    if chat_id is None:
        return _html_response(400, _error_page(
            "Authorization link is invalid or expired. Please request a new link from the bot."
        ))

    try:
        tokens = exchange_code_for_tokens(
            client_id=GOOGLE_CLIENT_ID,
            client_secret=GOOGLE_CLIENT_SECRET,
            code=code,
            redirect_uri=GOOGLE_REDIRECT_URI,
        )
    except OAuthError as exc:
        logger.error("Token exchange failed for chat_id=%s: %s", chat_id, exc)
        return _html_response(502, _error_page(
            "Could not complete sign-in with Google. Please try again."
        ))

    if not tokens.refresh_token:
        # Happens if user revoked + re-authed without prompt=consent. Our auth
        # URL forces consent so this shouldn't fire — log loudly if it does.
        logger.error("No refresh_token returned for chat_id=%s — bad scope/prompt config", chat_id)
        return _html_response(500, _error_page(
            "Google didn't return a refresh token. Please disconnect this app "
            "from your Google Account settings and try again."
        ))

    # Encrypt the refresh token before persistence
    try:
        encrypted = encrypt_refresh_token(
            _kms,
            key_id=KMS_KEY_ID,
            chat_id=chat_id,
            refresh_token=tokens.refresh_token,
        )
    except TokenEncryptionError as exc:
        logger.error("KMS encrypt failed for chat_id=%s: %s", chat_id, exc)
        return _html_response(500, _error_page("Could not securely store credentials."))

    _save_user_tokens(chat_id, encrypted_token=encrypted, scope=tokens.scope)

    if TELEGRAM_BOT_TOKEN:
        send_message(
            bot_token=TELEGRAM_BOT_TOKEN,
            chat_id=chat_id,
            text=(
                "✅ Google account connected!\n\n"
                "Send me an Instagram link any time and I'll save the summary "
                "to your Google Docs."
            ),
        )

    return _html_response(200, _success_page())


# ─────────────────────────────────────────────────────────────────────────────
# DynamoDB
# ─────────────────────────────────────────────────────────────────────────────

def _save_user_tokens(chat_id: str, *, encrypted_token: str, scope: str) -> None:
    table = _dynamodb.Table(DYNAMODB_USERS_TABLE)
    now = datetime.now(timezone.utc).isoformat()
    try:
        table.update_item(
            Key={"chat_id": chat_id},
            UpdateExpression=(
                "SET google_refresh_token_encrypted = :tok, "
                "    google_oauth_scope            = :scope, "
                "    google_connected_at           = :now, "
                "    onboarding_completed          = :true, "
                "    last_active                   = :now, "
                "    created_at                    = if_not_exists(created_at, :now)"
            ),
            ExpressionAttributeValues={
                ":tok": encrypted_token,
                ":scope": scope,
                ":now": now,
                ":true": True,
            },
        )
        logger.info("Stored encrypted Google refresh token for chat_id=%s", chat_id)
    except ClientError as exc:
        logger.error("DynamoDB update_item failed for chat_id=%s: %s", chat_id, exc)
        raise


# ─────────────────────────────────────────────────────────────────────────────
# State signing — HMAC over (chat_id, timestamp)
# ─────────────────────────────────────────────────────────────────────────────

def _sign_state(chat_id: str) -> str:
    ts = str(int(time.time()))
    msg = f"{chat_id}|{ts}".encode("utf-8")
    sig = hmac.HMAC(OAUTH_STATE_SECRET.encode("utf-8"), msg, hashlib.sha256).digest()
    sig_b64 = base64.urlsafe_b64encode(sig).rstrip(b"=").decode("ascii")
    return f"{chat_id}.{ts}.{sig_b64}"


def _verify_state(state: str) -> str | None:
    """Return the chat_id if the signature + TTL check out, else None."""
    parts = state.split(".")
    if len(parts) != 3:
        return None
    chat_id, ts_str, sig_b64 = parts
    if not chat_id or not ts_str.isdigit():
        return None

    age = int(time.time()) - int(ts_str)
    if age < 0 or age > STATE_TTL_SECONDS:
        logger.warning("State token expired or future-dated (age=%ds)", age)
        return None

    expected = hmac.HMAC(
        OAUTH_STATE_SECRET.encode("utf-8"),
        f"{chat_id}|{ts_str}".encode("utf-8"),
        hashlib.sha256,
    ).digest()
    expected_b64 = base64.urlsafe_b64encode(expected).rstrip(b"=").decode("ascii")
    if not hmac.compare_digest(sig_b64, expected_b64):
        logger.warning("State token signature mismatch for chat_id=%s", chat_id)
        return None

    return chat_id


# ─────────────────────────────────────────────────────────────────────────────
# HTML responses
# ─────────────────────────────────────────────────────────────────────────────

def _html_response(status: int, body: str) -> dict:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "text/html; charset=utf-8",
            "Cache-Control": "no-store",
        },
        "body": body,
    }


def _success_page() -> str:
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Connected ✓</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         background:#0e1116; color:#eaeaea; display:grid; place-items:center;
         min-height:100vh; margin:0; padding:1rem; }
  .card { background:#171b22; border:1px solid #2a313c; border-radius:12px;
          max-width:460px; padding:2.5rem; text-align:center;
          box-shadow:0 6px 24px rgba(0,0,0,.4); }
  h1 { margin:0 0 .5rem; font-size:1.6rem; }
  p  { margin:.6rem 0; line-height:1.5; color:#b8c0cc; }
  .check { font-size:3rem; margin-bottom:.5rem; }
</style></head>
<body>
  <main class="card">
    <div class="check">✅</div>
    <h1>Google account connected</h1>
    <p>You can return to Telegram now — just send me any Instagram link
       and I'll save the AI summary to your Google Docs.</p>
    <p style="font-size:.85rem; opacity:.6;">You can close this tab.</p>
  </main>
</body></html>
"""


def _error_page(message: str) -> str:
    safe = html.escape(message)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Sign-in error</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         background:#0e1116; color:#eaeaea; display:grid; place-items:center;
         min-height:100vh; margin:0; padding:1rem; }}
  .card {{ background:#171b22; border:1px solid #4a2a2a; border-radius:12px;
          max-width:460px; padding:2.5rem; text-align:center; }}
  h1 {{ margin:0 0 .5rem; font-size:1.4rem; color:#ff8a80; }}
  p  {{ line-height:1.5; color:#cfd3da; }}
</style></head>
<body>
  <main class="card">
    <h1>Couldn't complete sign-in</h1>
    <p>{safe}</p>
  </main>
</body></html>
"""


def _redact(qs: dict) -> dict:
    """Strip secret values for safe logging."""
    safe = {}
    for k, v in qs.items():
        if k.lower() in {"code", "state"} and isinstance(v, str):
            safe[k] = v[:6] + "…" if v else ""
        else:
            safe[k] = v
    return safe

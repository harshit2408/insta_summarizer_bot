"""
AWS KMS helper — encrypt / decrypt Google OAuth refresh tokens.

Why a wrapper?
  * Every encrypt call must use the same EncryptionContext as the matching
    decrypt call. Forgetting this on either side silently breaks the round
    trip — keep the binding centralised.
  * Refresh tokens are short ASCII strings (~150 bytes) so we encrypt them
    directly with ``kms:Encrypt`` instead of using envelope encryption.
    AWS limits direct encryption to 4 KB which is plenty.
  * The ciphertext is base64 encoded before storage in DynamoDB so it
    survives JSON serialisation cleanly.

Per-user EncryptionContext: ``{"chat_id": "<telegram chat id>"}``. This binds
the ciphertext to the user it was created for, so even an attacker with
``kms:Decrypt`` access cannot replay another user's stored token.
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


class TokenEncryptionError(RuntimeError):
    """Raised when the KMS round trip fails (network, permissions, mismatched context)."""


def encrypt_refresh_token(
    kms_client: Any,
    *,
    key_id: str,
    chat_id: str,
    refresh_token: str,
) -> str:
    """Encrypt ``refresh_token`` and return a base64-encoded ciphertext.

    The returned string is safe to store as a DynamoDB ``S`` attribute.
    """
    if not refresh_token:
        raise ValueError("refresh_token must be non-empty")
    if not chat_id:
        raise ValueError("chat_id must be non-empty (used as encryption context)")

    try:
        response = kms_client.encrypt(
            KeyId=key_id,
            Plaintext=refresh_token.encode("utf-8"),
            EncryptionContext={"chat_id": str(chat_id)},
        )
    except Exception as exc:  # boto3 ClientError or network
        logger.error("KMS encrypt failed for chat_id=%s: %s", chat_id, exc)
        raise TokenEncryptionError(f"KMS encrypt failed: {exc}") from exc

    blob: bytes = response["CiphertextBlob"]
    return base64.b64encode(blob).decode("ascii")


def decrypt_refresh_token(
    kms_client: Any,
    *,
    chat_id: str,
    ciphertext_b64: str,
) -> str:
    """Reverse of :func:`encrypt_refresh_token`.

    KMS infers the key from the ciphertext blob — no need to pass key id on
    decrypt. The same EncryptionContext used at encrypt time must be supplied
    or KMS rejects the request with ``InvalidCiphertextException``.
    """
    if not ciphertext_b64:
        raise ValueError("ciphertext is empty")
    try:
        blob = base64.b64decode(ciphertext_b64)
    except (ValueError, TypeError) as exc:
        raise TokenEncryptionError(f"Stored ciphertext is not valid base64: {exc}") from exc

    try:
        response = kms_client.decrypt(
            CiphertextBlob=blob,
            EncryptionContext={"chat_id": str(chat_id)},
        )
    except Exception as exc:
        logger.error("KMS decrypt failed for chat_id=%s: %s", chat_id, exc)
        raise TokenEncryptionError(f"KMS decrypt failed: {exc}") from exc

    return response["Plaintext"].decode("utf-8")


# Convenience wrappers when the caller only has env vars to work with ─────────


def get_kms_key_id() -> str:
    """Read the KMS key id (alias or arn) from the standard env var."""
    return os.environ["KMS_KEY_ID"]

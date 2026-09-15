"""
Encryption utility for sensitive data (account_data: logins, passwords).

Uses Fernet (AES-128-CBC with HMAC-SHA256) from the cryptography library.
Key is loaded from ENCRYPTION_KEY env var. If not set, a key is auto-generated
and saved to .env — but production MUST set it explicitly.

Backwards-compatible: if data doesn't look encrypted (no Fernet prefix),
it's returned as-is (plain text from older DB entries).
"""

import base64
import logging
import os

logger = logging.getLogger(__name__)

# Lazy-loaded Fernet instance
_fernet = None
_key_loaded = False


def _get_fernet():
    """Lazy-load Fernet instance. Returns None if cryptography not installed."""
    global _fernet, _key_loaded
    if _key_loaded:
        return _fernet
    _key_loaded = True

    try:
        from cryptography.fernet import Fernet
    except ImportError:
        logger.warning(
            "cryptography package not installed — account_data will NOT be encrypted! "
            "Install with: pip install cryptography"
        )
        return None

    key = os.getenv("ENCRYPTION_KEY", "").strip()
    env_name = os.getenv("ENV", os.getenv("NODE_ENV", "")).lower()
    is_production = env_name in ("production", "prod")

    if not key:
        if is_production:
            # FAIL in production — auto-generating a key would make all
            # previously encrypted data unreadable on next restart (Docker, K8s, etc.)
            logger.critical(
                "ENCRYPTION_KEY is not set and ENV=production! "
                "Refusing to auto-generate — all existing account_data would become unreadable. "
                "Set ENCRYPTION_KEY explicitly in your environment."
            )
            return None

        # Auto-generate key for development only
        key = Fernet.generate_key().decode()
        key_hash = __import__("hashlib").sha256(key.encode()).hexdigest()[:8]
        logger.warning(
            "ENCRYPTION_KEY not set! Auto-generated key (hash: %s).\n"
            "Key saved to .env — CHECK YOUR .env FILE FOR THE ACTUAL KEY!\n"
            "⚠️  In production (ENV=production), the bot will REFUSE to start without an explicit key.",
            key_hash,
        )
        # Try to append to .env
        try:
            env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
            if os.path.exists(env_path):
                with open(env_path, "a", encoding="utf-8") as f:
                    f.write(f"\n# Auto-generated encryption key — KEEP SECRET!\nENCRYPTION_KEY={key}\n")
                logger.info(f"Auto-generated ENCRYPTION_KEY saved to {env_path}")
        except Exception as e:
            logger.warning(f"Could not save auto-generated key to .env: {e}")

    try:
        _fernet = Fernet(key.encode() if isinstance(key, str) else key)
    except Exception as e:
        logger.error(f"Invalid ENCRYPTION_KEY: {e}. Encryption DISABLED.")
        return None

    return _fernet


def encrypt(data: str) -> str:
    """Encrypt a string. Returns the original string if encryption unavailable."""
    if not data:
        return data
    f = _get_fernet()
    if f is None:
        return data
    try:
        return f.encrypt(data.encode("utf-8")).decode("utf-8")
    except Exception as e:
        logger.error(f"Encryption failed: {e}. Storing PLAINTEXT — THIS IS A SECURITY ISSUE!")
        return data


def decrypt(data: str) -> str:
    """Decrypt a string. Returns the original string if decryption fails (backwards compat)."""
    if not data:
        return data
    f = _get_fernet()
    if f is None:
        return data
    try:
        return f.decrypt(data.encode("utf-8")).decode("utf-8")
    except Exception as e:
        # Not encrypted (old data) or wrong key
        if is_encrypted(data):
            logger.error(f"Decryption failed (wrong key?): {e}. Data may be unreadable.")
        return data


def is_encrypted(data: str) -> bool:
    """Check if data looks like a Fernet token (starts with 'gAAAAA' by default)."""
    if not data:
        return False
    try:
        # Fernet tokens are base64url-encoded and start with version byte 0x80
        # which encodes to 'g' in base64
        return data.startswith("gAAAAA")
    except Exception:
        return False

#!/usr/bin/env python3
"""Diagnostic script for Digiseller API authentication.

Run from the bot directory:
    python test_digiseller_auth.py

Or from anywhere (script will cd to the bot dir):
    python /path/to/test_digiseller_auth.py

Checks:
  1. Config values are loaded (seller_id, api_key, product_id)
  2. Clock skew vs Digiseller server
  3. Signature computation
  4. Actual auth request
  5. Lists common mistakes
"""

import asyncio
import hashlib
import json
import os
import sys
import time
import pathlib
from urllib.parse import urlencode

# Try to load .env
try:
    from dotenv import load_dotenv
    # Look for .env in the bot directory (parent of scripts/)
    _here = pathlib.Path(__file__).resolve().parent
    _bot_dir = _here.parent / "subscription-bot"
    if not _bot_dir.exists():
        _bot_dir = _here
    env_path = _bot_dir / ".env"
    if env_path.exists():
        load_dotenv(env_path)
        print(f"[OK] Loaded .env from: {env_path}")
    else:
        print(f"[WARN] No .env found at: {env_path}")
        print("      Make sure you run this script from the bot directory.")
except ImportError:
    print("[WARN] python-dotenv not installed; relying on environment variables")

# Add bot dir to path so we can import config
sys.path.insert(0, str(_bot_dir))


async def check_server_time():
    """Check Digiseller server time vs local time."""
    import aiohttp
    try:
        async with aiohttp.ClientSession() as session:
            # Digiseller has a public time endpoint
            async with session.get(
                "https://api.digiseller.ru/api/version",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                data = await resp.json(content_type=None)
                # Some endpoints return server timestamp
                server_ts = data.get("timestamp") or data.get("server_time")
                if server_ts:
                    local_ts = int(time.time())
                    skew = local_ts - int(server_ts)
                    print(f"  Local time:   {local_ts} ({time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(local_ts))})")
                    print(f"  Server time:  {server_ts}")
                    print(f"  Skew:         {skew:+d} seconds")
                    if abs(skew) > 60:
                        print(f"  [FAIL] Clock skew > 60s — Digiseller will reject auth!")
                        print(f"         Fix your system clock (sync NTP).")
                    else:
                        print(f"  [OK] Clock skew within acceptable range.")
                else:
                    print(f"  [INFO] Server response: {data}")
    except Exception as e:
        print(f"  [WARN] Could not check server time: {e}")


def check_config():
    """Check that config values are present and sane."""
    print("\n=== 1. Config check ===")
    try:
        from config import (
            DIGISELLER_SELLER_ID,
            DIGISELLER_API_KEY,
            DIGISELLER_PRODUCT_ID,
            DIGISELLER_CURRENCY,
            DIGISELLER_EMAIL_DOMAIN,
        )
    except ImportError as e:
        print(f"  [FAIL] Cannot import config: {e}")
        print(f"         Make sure you run this script from the bot directory: {_bot_dir}")
        return False

    issues = []

    # seller_id
    print(f"  DIGISELLER_SELLER_ID  = {DIGISELLER_SELLER_ID!r}")
    if not DIGISELLER_SELLER_ID or DIGISELLER_SELLER_ID <= 0:
        issues.append("seller_id is 0 or empty — set it in .env")
    elif DIGISELLER_SELLER_ID < 1000:
        issues.append(f"seller_id={DIGISELLER_SELLER_ID} looks too small (usually 6-7 digits)")

    # api_key
    key = DIGISELLER_API_KEY or ""
    masked = key[:4] + "***" + key[-4:] if len(key) > 8 else f"*** (len={len(key)})"
    print(f"  DIGISELLER_API_KEY    = {masked}  (length={len(key)})")
    if not key:
        issues.append("api_key is empty — set it in .env")
    elif len(key) < 20:
        issues.append(f"api_key length={len(key)} looks too short (usually 32+ chars)")
    if key != key.strip():
        issues.append("api_key has leading/trailing whitespace — will be stripped automatically")

    # product_id
    print(f"  DIGISELLER_PRODUCT_ID = {DIGISELLER_PRODUCT_ID!r}")
    if not DIGISELLER_PRODUCT_ID or DIGISELLER_PRODUCT_ID <= 0:
        issues.append("product_id is 0 or empty — set it in .env")

    print(f"  DIGISELLER_CURRENCY   = {DIGISELLER_CURRENCY!r}")
    print(f"  DIGISELLER_EMAIL_DOMN = {DIGISELLER_EMAIL_DOMAIN!r}")

    if issues:
        print("\n  [FAIL] Config issues:")
        for i in issues:
            print(f"         - {i}")
        return False
    print("\n  [OK] Config values look present.")
    return True


async def test_auth():
    """Try to authenticate and show the full request/response."""
    import aiohttp
    from config import DIGISELLER_SELLER_ID, DIGISELLER_API_KEY

    print("\n=== 3. Auth request ===")
    api_key = (DIGISELLER_API_KEY or "").strip()
    timestamp = int(time.time())
    sign_input = api_key + str(timestamp)
    sign = hashlib.sha256(sign_input.encode("utf-8")).hexdigest()

    payload = {
        "seller_id": int(DIGISELLER_SELLER_ID),
        "timestamp": timestamp,
        "sign": sign,
    }
    print(f"  POST https://api.digiseller.ru/api/apilogin")
    print(f"  Body: {json.dumps({**payload, 'sign': sign[:20] + '...'}, indent=2)}")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.digiseller.ru/api/apilogin",
                json=payload,
                headers={"Accept": "application/json"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                status = resp.status
                data = await resp.json(content_type=None)
                print(f"\n  HTTP status: {status}")
                print(f"  Response: {json.dumps(data, indent=2, ensure_ascii=False)}")

                if data.get("token"):
                    print("\n  [OK] Auth successful! Token acquired.")
                    return True
                else:
                    print("\n  [FAIL] Auth failed.")
                    return False
    except Exception as e:
        print(f"\n  [FAIL] Network error: {e}")
        return False


def show_common_mistakes():
    """Print common mistakes checklist."""
    print("\n=== 4. Common mistakes checklist ===")
    print("""
  [ ] DIGISELLER_SELLER_ID is a NUMBER (e.g., 1234567), NOT your email/login
      Find it at: my.digiseller.com → "Мой аккаунт" → top right corner

  [ ] DIGISELLER_API_KEY is the EXACT key from:
      my.digiseller.com → API → API keys
      (not the password, not the login, not the product id)

  [ ] API key has permission: [Statistics] → "Sales statistics"
      Without this permission, auth may succeed but stats calls fail

  [ ] API key is not expired
      Check the "valid until" date in the API keys list

  [ ] DIGISELLER_PRODUCT_ID (id_d) is the product ID, NOT seller_id
      Find it: Товары → your product → "id_d" column

  [ ] System clock is synced (NTP)
      Windows: Settings → Time & Language → Date & time → Sync now
      Linux: sudo ntpdate pool.ntp.org  or  sudo systemctl restart systemd-timesyncd

  [ ] You are using api.digiseller.RU (not .com — they are different systems)
      Some old docs reference digiseller.com; the current API is at digiseller.ru
""")


async def main():
    print("=" * 60)
    print("Digiseller API Authentication Diagnostic")
    print("=" * 60)

    # 1. Config
    config_ok = check_config()
    if not config_ok:
        print("\nFix config issues above first, then re-run this script.")
        show_common_mistakes()
        return

    # 2. Server time / clock skew
    print("\n=== 2. Clock skew check ===")
    await check_server_time()

    # 3. Auth attempt
    auth_ok = await test_auth()

    # 4. Common mistakes
    show_common_mistakes()

    print("=" * 60)
    if auth_ok:
        print("RESULT: Auth works! The issue is elsewhere.")
        print("Next: try creating a payment URL from the bot.")
    else:
        print("RESULT: Auth failed. Fix the issues above and re-run.")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())

"""One-time interactive Garmin Connect login that mints a reusable token store.

Usage:  python scripts/login.py [tokenstore_dir]

Garmin's SSO sits behind a Cloudflare WAF and most accounts have MFA enabled,
so this cannot run headlessly. Run it once, by hand; the server then loads
the resulting tokens and refreshes them on its own. Re-run only if the server
starts failing with an auth error.

Credentials are read from this prompt only. They are never written to disk,
never passed as command-line arguments, and never logged. Only the resulting
tokens are saved to tokenstore_dir.
"""

import getpass
import os
import sys

from garminconnect import Garmin


def main():
    tokenstore = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("GARMIN_TOKEN_DIR")
    if not tokenstore:
        print(
            "usage: python scripts/login.py <tokenstore_dir>\n"
            "(or set GARMIN_TOKEN_DIR in the environment)",
            file=sys.stderr,
        )
        sys.exit(2)

    email = input("Garmin Connect email: ").strip()
    password = getpass.getpass("Garmin Connect password (hidden): ")

    try:
        client = Garmin(
            email=email,
            password=password,
            prompt_mfa=lambda: input("MFA / 2FA code: ").strip(),
        )
        client.login(tokenstore=tokenstore)
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        print(f"\nLogin failed: {msg}", file=sys.stderr)
        if "429" in msg or "too many" in msg.lower():
            print(
                "\nGarmin is rate-limiting you (HTTP 429). Wait at least an hour before\n"
                "retrying -- retrying in a loop makes it worse.",
                file=sys.stderr,
            )
        sys.exit(3)

    print(f"\nSuccess. Tokens saved to {tokenstore}")
    print("Treat that directory like a password: restrict its permissions, never commit it.")


if __name__ == "__main__":
    main()

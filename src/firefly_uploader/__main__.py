"""Command line.

    python -m firefly_uploader serve                  run the web app
    python -m firefly_uploader set-password USERNAME  when a password is forgotten
    python -m firefly_uploader check [--test]         test FIREFLY_URL/FIREFLY_TOKEN (read-only)

Settings, from the environment or `.env`:
    UPLOADER_DB          the database (default: uploader.db here)
    UPLOADER_SECRET_KEY  encrypts the Firefly tokens in the database (default: a key file made
                         next to the database on first start)
"""

import argparse
import getpass
import os
from pathlib import Path

from dotenv import load_dotenv

from .firefly import FireflyClient, FireflyError


def check(prefix: str) -> None:
    """Read-only: show what the uploader sees in Firefly."""
    with FireflyClient.from_env(prefix) as firefly:
        print(f"Firefly {firefly.version()} at {firefly.url}, logged in as {firefly.user_email()}")
        print("Asset accounts:")
        for account in firefly.asset_accounts():
            inactive = "" if account.active else ", inactive"
            print(f"  #{account.id} {account.name} ({account.currency}{inactive})")
        categories = firefly.categories()
        print(f"Categories ({len(categories)}): {', '.join(categories) or '-'}")


def open_store():
    from .auth import TokenCipher, load_key
    from .store import Store

    path = Path(os.environ.get("UPLOADER_DB", "uploader.db"))
    key = load_key(os.environ.get("UPLOADER_SECRET_KEY"), path.parent / "secret.key")
    try:
        cipher = TokenCipher(key)
    except ValueError:
        raise SystemExit("Error: UPLOADER_SECRET_KEY isn't a valid key (it must be 32 url-safe base64-encoded bytes)")
    return Store(path, cipher)


def serve(host: str, port: int) -> None:
    import uvicorn

    from .web import create_app

    store = open_store()
    print(f"Open http://{host}:{port} (data in {store.path.resolve()})")
    # Behind a reverse proxy, FORWARDED_ALLOW_IPS lets it tell the app the page was asked over https.
    uvicorn.run(
        create_app(store), host=host, port=port,
        proxy_headers=True, forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"),
    )


def set_password(username: str) -> None:
    store = open_store()
    password = getpass.getpass(f"New password for {username}: ")
    if len(password) < 8:
        raise SystemExit("Error: the password needs at least 8 characters")
    if getpass.getpass("Again: ") != password:
        raise SystemExit("Error: the two passwords aren't the same")
    if not store.set_password(username, password):
        raise SystemExit(f"Error: there's no user {username}")
    print(f"Password changed; {username} is logged out everywhere.")


def main(argv: list[str] | None = None) -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="python -m firefly_uploader")
    commands = parser.add_subparsers(dest="command", required=True)
    serve_parser = commands.add_parser("serve", help="run the web app")
    serve_parser.add_argument("--host", default="127.0.0.1", help="default: this PC only")
    serve_parser.add_argument("--port", type=int, default=8765)
    password_parser = commands.add_parser("set-password", help="set a new password for a user")
    password_parser.add_argument("username")
    check_parser = commands.add_parser("check", help="test FIREFLY_URL/FIREFLY_TOKEN (read-only)")
    check_parser.add_argument(
        "--test", action="store_true", help="use the throwaway instance (FIREFLY_TEST_* in .env)"
    )
    args = parser.parse_args(argv)
    try:
        if args.command == "serve":
            serve(args.host, args.port)
        elif args.command == "set-password":
            set_password(args.username)
        else:
            check("FIREFLY_TEST" if args.test else "FIREFLY")
    except FireflyError as error:
        raise SystemExit(f"Error: {error}")


if __name__ == "__main__":
    main()

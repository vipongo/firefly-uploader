"""Command line: `python -m firefly_uploader check [--test]` tests the Firefly connection."""

import argparse

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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m firefly_uploader")
    commands = parser.add_subparsers(dest="command", required=True)
    check_parser = commands.add_parser("check", help="test the connection to Firefly (read-only)")
    check_parser.add_argument(
        "--test", action="store_true", help="use the throwaway instance (FIREFLY_TEST_* in .env)"
    )
    args = parser.parse_args(argv)
    try:
        check("FIREFLY_TEST" if args.test else "FIREFLY")
    except FireflyError as error:
        raise SystemExit(f"Error: {error}")


if __name__ == "__main__":
    main()

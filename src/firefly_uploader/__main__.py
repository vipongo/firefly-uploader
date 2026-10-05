"""Command line.

    python -m firefly_uploader serve [--test]   run the web app
    python -m firefly_uploader check [--test]   test the Firefly connection (read-only)

--test uses the throwaway instance (FIREFLY_TEST_* in .env) instead of FIREFLY_*.
"""

import argparse
import os

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


def serve(prefix: str, host: str, port: int) -> None:
    import uvicorn

    from .store import Store
    from .web import create_app

    FireflyClient.from_env(prefix).close()  # fail early when the settings are missing
    load_dotenv()
    store = Store(os.environ.get("UPLOADER_DB", "uploader.db"))
    app = create_app(lambda: FireflyClient.from_env(prefix), store)
    print(f"Open http://{host}:{port} (remembered answers in {store.path.resolve()})")
    uvicorn.run(app, host=host, port=port)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m firefly_uploader")
    commands = parser.add_subparsers(dest="command", required=True)
    serve_parser = commands.add_parser("serve", help="run the web app")
    serve_parser.add_argument("--host", default="127.0.0.1", help="default: this PC only")
    serve_parser.add_argument("--port", type=int, default=8765)
    check_parser = commands.add_parser("check", help="test the connection to Firefly (read-only)")
    for command in (serve_parser, check_parser):
        command.add_argument(
            "--test", action="store_true", help="use the throwaway instance (FIREFLY_TEST_* in .env)"
        )
    args = parser.parse_args(argv)
    prefix = "FIREFLY_TEST" if args.test else "FIREFLY"
    try:
        if args.command == "serve":
            serve(prefix, args.host, args.port)
        else:
            check(prefix)
    except FireflyError as error:
        raise SystemExit(f"Error: {error}")


if __name__ == "__main__":
    main()

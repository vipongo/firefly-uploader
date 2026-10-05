"""Prepare the throwaway Firefly from compose.yml: user, API token in .env, CHF as main currency.

Run after `docker compose -f dev/firefly-test/compose.yml up -d` (again after `down -v`):
    .venv\\Scripts\\python dev\\firefly-test\\setup.py
"""

import re
import secrets
import subprocess
import time
from pathlib import Path

import httpx

URL = "http://localhost:18081"
EMAIL = "test@example.com"
CONTAINER = "firefly-test-app-1"
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"

# Firefly also logs to stdout, so the token is printed on its own line and picked out below.
MAKE_TOKEN = (
    'require "vendor/autoload.php"; $app = require "bootstrap/app.php";'
    "$app->make(Illuminate\\Contracts\\Console\\Kernel::class)->bootstrap();"
    f'echo "\\n", FireflyIII\\User::where("email", "{EMAIL}")->firstOrFail()'
    '->createToken("uploader-test")->accessToken, "\\n";'
)


def wait_until_up(web: httpx.Client) -> None:
    for _ in range(60):
        try:
            if web.get("/").status_code in (200, 302):
                return
        except httpx.TransportError:
            pass
        time.sleep(2)
    raise SystemExit(f"Firefly isn't answering on {URL}; is the container running?")


def register(web: httpx.Client) -> None:
    """Sign up through the web form. The first user of a fresh instance becomes its owner."""
    page = web.get("/register")
    form = re.search(r'name="_token" value="([^"]+)"', page.text)
    if not form:  # registration closes once the first user exists
        print("Registration closed, assuming the test user already exists")
        return
    csrf = form.group(1)
    password = secrets.token_urlsafe(24)  # never needed again: the API uses the token
    response = web.post("/register", data={
        "_token": csrf, "email": EMAIL, "password": password, "password_confirmation": password,
    })
    if response.status_code != 302:
        raise SystemExit(f"Registration failed with HTTP {response.status_code}")
    print(f"Registered {EMAIL}")


def in_container(*command: str) -> str:
    result = subprocess.run(["docker", "exec", CONTAINER, *command], capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"{' '.join(command[:3])} failed:\n{result.stdout}{result.stderr}")
    return result.stdout


def make_token() -> str:
    # A fresh instance has no OAuth client for personal tokens until someone opens the API page.
    in_container("php", "artisan", "passport:client", "--personal", "--name=uploader-test",
                 "--no-interaction")
    output = in_container("php", "-r", MAKE_TOKEN)
    tokens = re.findall(r"^eyJ[\w.-]+$", output, re.MULTILINE)
    if not tokens:
        raise SystemExit("Couldn't create an API token:\n" + output)
    return tokens[-1]


def save_to_env(token: str) -> None:
    lines = ENV_FILE.read_text().splitlines() if ENV_FILE.exists() else []
    lines = [line for line in lines if not line.startswith("FIREFLY_TEST_")]
    lines += [f"FIREFLY_TEST_URL={URL}", f"FIREFLY_TEST_TOKEN={token}"]
    ENV_FILE.write_text("\n".join(lines) + "\n")
    print(f"Saved FIREFLY_TEST_URL and FIREFLY_TEST_TOKEN to {ENV_FILE}")


def set_up_like_real_instance(token: str) -> None:
    """CHF as main currency and one CHF asset account per bank."""
    api = httpx.Client(base_url=URL + "/api/v1", headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.api+json",
    })
    api.post("/currencies/CHF/enable", json={}).raise_for_status()
    api.post("/currencies/CHF/primary", json={}).raise_for_status()
    existing = {item["attributes"]["name"] for item in api.get("/accounts?type=asset").json()["data"]}
    for name in ["UBS", "Revolut"]:
        if name not in existing:
            api.post("/accounts", json={
                "name": name, "type": "asset", "account_role": "defaultAsset", "currency_code": "CHF",
            }).raise_for_status()
    print("CHF is the main currency; asset accounts UBS and Revolut exist")


def main() -> None:
    with httpx.Client(base_url=URL, follow_redirects=False, timeout=30) as web:
        wait_until_up(web)
        register(web)
    token = make_token()
    save_to_env(token)
    set_up_like_real_instance(token)


if __name__ == "__main__":
    main()

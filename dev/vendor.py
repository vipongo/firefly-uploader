"""Download the third-party files the web pages use into static/vendor/, with their licenses.

They're kept in the repository so the app works without reaching the internet. To update one,
change its version below and run:  .venv\\Scripts\\python dev\\vendor.py
"""

from pathlib import Path

import httpx

VENDOR = Path(__file__).resolve().parents[1] / "src" / "firefly_uploader" / "static" / "vendor"

# package: (version, files to fetch, license)
PACKAGES = {
    "admin-lte": ("4.10.0", ["dist/css/adminlte.min.css", "dist/js/adminlte.min.js"], "MIT"),
    "bootstrap": ("5.3.8", ["dist/js/bootstrap.bundle.min.js"], "MIT"),
    "bootstrap-icons": ("1.13.1", ["font/bootstrap-icons.min.css", "font/fonts/bootstrap-icons.woff2"], "MIT"),
    "tom-select": ("2.6.2", ["dist/css/tom-select.bootstrap5.min.css", "dist/js/tom-select.complete.min.js"], "Apache-2.0"),
}


def main() -> None:
    lines = [
        "# Third-party files",
        "",
        "Downloaded by `dev/vendor.py` from npm (via jsdelivr); don't edit them by hand.",
        "",
        "| Package | Version | License |",
        "| --- | --- | --- |",
    ]
    with httpx.Client(base_url="https://cdn.jsdelivr.net/npm/", timeout=60, follow_redirects=True) as http:
        for package, (version, files, license_name) in PACKAGES.items():
            for file in [*files, "LICENSE"]:
                response = http.get(f"{package}@{version}/{file}")
                response.raise_for_status()
                target = VENDOR / package / file.removeprefix("dist/")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(response.content)
                print(f"{package}@{version}/{file} -> {target.relative_to(VENDOR)}")
            lines.append(f"| [{package}](https://www.npmjs.com/package/{package}) | {version} | {license_name}, see `{package}/LICENSE` |")
    (VENDOR / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

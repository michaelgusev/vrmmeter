#!/usr/bin/env python3
"""
Asset build script for VRM Meter.
Fetches external JS dependencies (Alpine.js, Chart.js) and compiles
Tailwind CSS into a production-ready, minified static stylesheet.
"""

import os
import platform
import stat
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT_DIR / "vrmmeter" / "web" / "static"
JS_DIR = STATIC_DIR / "js"
CSS_DIR = STATIC_DIR / "css"
CACHE_DIR = ROOT_DIR / ".cache"

# Pinned versions of client-side libraries
ASSETS = {
    "alpine.min.js": "https://cdn.jsdelivr.net/npm/alpinejs@3.14.3/dist/cdn.min.js",
    "chart.umd.min.js": "https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js",
}

TAILWIND_VERSION = "v3.4.17"


def download_file(url: str, dest: Path) -> None:
    print(f"  Downloading {url} -> {dest.name}...")
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; VRMMeterAssetBuilder/1.0)"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp, open(dest, "wb") as f:
        f.write(resp.read())


def fetch_js_assets() -> None:
    print("1. Fetching JavaScript dependencies...")
    JS_DIR.mkdir(parents=True, exist_ok=True)
    for filename, url in ASSETS.items():
        dest = JS_DIR / filename
        download_file(url, dest)
        print(f"     [OK] {filename} ({dest.stat().st_size:,} bytes)")


def get_tailwind_cli_name() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()

    if system == "windows":
        return "tailwindcss-windows-x64.exe"
    elif system == "darwin":
        if "arm" in machine or "aarch" in machine:
            return "tailwindcss-macos-arm64"
        return "tailwindcss-macos-x64"
    else:  # Linux
        if "arm" in machine or "aarch" in machine:
            return "tailwindcss-linux-arm64"
        elif "armv7" in machine:
            return "tailwindcss-linux-armv7"
        return "tailwindcss-linux-x64"


def compile_tailwind_css() -> None:
    print("2. Compiling Tailwind CSS stylesheet...")
    CSS_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    cli_name = get_tailwind_cli_name()
    cli_path = CACHE_DIR / cli_name

    if not cli_path.exists():
        url = f"https://github.com/tailwindlabs/tailwindcss/releases/download/{TAILWIND_VERSION}/{cli_name}"
        download_file(url, cli_path)
        if platform.system().lower() != "windows":
            cli_path.chmod(cli_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    input_css = CACHE_DIR / "input.css"
    if not input_css.exists():
        input_css.write_text(
            "@tailwind base;\n@tailwind components;\n@tailwind utilities;\n",
            encoding="utf-8",
        )

    config_js = ROOT_DIR / "scripts" / "tailwind.config.js"
    output_css = CSS_DIR / "tailwind.css"

    cmd = [
        str(cli_path),
        "-c",
        str(config_js),
        "-i",
        str(input_css),
        "-o",
        str(output_css),
        "--minify",
    ]

    print("     Compiling utility classes from templates...")
    result = subprocess.run(cmd, cwd=str(ROOT_DIR), capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Error compiling CSS:\n{result.stderr}", file=sys.stderr)
        sys.exit(result.returncode)

    print(f"     [OK] {output_css.name} ({output_css.stat().st_size:,} bytes)")


def main() -> None:
    print("========================================")
    print(" Building VRM Meter Frontend Assets")
    print("========================================")
    fetch_js_assets()
    compile_tailwind_css()
    print("========================================")
    print("[OK] All static assets built successfully!")
    print("========================================")


if __name__ == "__main__":
    main()
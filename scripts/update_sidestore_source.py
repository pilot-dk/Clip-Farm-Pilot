#!/usr/bin/env python3
"""Put a newly built IPA at the top of Clip Farm Pilot's SideStore feed."""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / "sidestore-source.json"
RELEASE_BASE = "https://github.com/pilot-dk/Clip-Farm-Pilot/releases/download"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True, help="Marketing version, with or without a leading v")
    parser.add_argument("--ipa", required=True, type=Path, help="Built IPA used to calculate the download size")
    parser.add_argument("--date", default=date.today().isoformat(), help="Release date in YYYY-MM-DD format")
    parser.add_argument("--description", default="Fully local Clip Farm Pilot iPhone and iPad update.")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    version = args.version.removeprefix("v")
    if not args.ipa.is_file():
        raise SystemExit(f"IPA not found: {args.ipa}")

    source = json.loads(args.source.read_text(encoding="utf-8"))
    app = source["apps"][0]
    asset_name = f"Clip-Farm-Pilot-iOS-v{version}-Local-Unsigned.ipa"
    download_url = f"{RELEASE_BASE}/v{version}/{asset_name}"
    item = {
        "version": version,
        "date": args.date,
        "downloadURL": download_url,
        "localizedDescription": args.description,
        "size": args.ipa.stat().st_size,
        "minOSVersion": "17.0",
    }

    previous = [entry for entry in app.get("versions", []) if entry.get("version") != version]
    app["versions"] = [item, *previous]
    app["version"] = version
    app["versionDate"] = args.date
    app["versionDescription"] = args.description
    app["downloadURL"] = download_url
    app["size"] = item["size"]
    args.source.write_text(json.dumps(source, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

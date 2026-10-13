"""Update only the two GitOps image mappings after a successful registry push."""
import argparse
from pathlib import Path
import re
import sys

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FILE = ROOT / "overlays/cmc/kustomization.yaml"


def update(path: Path, username: str, tag: str) -> None:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", username):
        raise ValueError("Docker Hub username contains unsupported characters")
    if not re.fullmatch(r"sha-[0-9a-f]{12,40}", tag):
        raise ValueError("Image tag must be immutable and formatted as sha-<git sha>")

    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    images = document.get("images", [])
    expected = {"odoo-queue", "odoo-backup"}
    found = {item.get("name") for item in images}
    if found != expected:
        raise ValueError(f"Expected exactly these image mappings: {sorted(expected)}")
    for item in images:
        item["newName"] = f"docker.io/{username}/{item['name']}"
        item["newTag"] = tag
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--username", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--file", type=Path, default=DEFAULT_FILE)
    args = parser.parse_args()
    update(args.file, args.username, args.tag)
    print(f"Updated {args.file} to {args.tag}")


if __name__ == "__main__":
    try:
        main()
    except ValueError as error:
        sys.exit(str(error))

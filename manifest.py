"""Render manifests used by PowerShell without native-shell inline quoting."""
import argparse
import hashlib
from pathlib import Path
import sys
import yaml

parser = argparse.ArgumentParser()
parser.add_argument("action", choices=("storage", "restore"))
parser.add_argument("--template", type=Path)
parser.add_argument("--backup-set")
parser.add_argument("--database")
args = parser.parse_args()
if args.action == "storage":
    documents = [d for d in yaml.safe_load_all(sys.stdin) if d and d["kind"] == "PersistentVolumeClaim"]
    yaml.safe_dump_all(documents, sys.stdout, sort_keys=False)
else:
    document = yaml.safe_load(args.template.read_text(encoding="utf-8"))
    suffix = hashlib.sha256(args.database.encode()).hexdigest()[:8]
    document["metadata"]["name"] = "odoo-restore-drill-" + args.backup_set.lower() + "-" + suffix
    for item in document["spec"]["template"]["spec"]["containers"][0]["env"]:
        if item["name"] == "BACKUP_SET":
            item["value"] = args.backup_set
        elif item["name"] == "RESTORE_DATABASE":
            item["value"] = args.database
    yaml.safe_dump(document, sys.stdout, sort_keys=False)

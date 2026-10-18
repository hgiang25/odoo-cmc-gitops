"""Promote the exact pair of tested Dev image references to Production, without rebuilding."""
import argparse
from pathlib import Path
import re
import yaml

ROOT = Path(__file__).resolve().parents[1]


def promote(expected_tag, root=ROOT, source_root=None):
    if not re.fullmatch(r'sha-[0-9a-f]{12,40}', expected_tag):
        raise ValueError('Expected immutable sha- tag is required')
    source = yaml.safe_load((Path(source_root or root) / 'overlays/dev/kustomization.yaml').read_text(encoding='utf-8'))
    target = root / 'overlays/prod/kustomization.yaml'
    document = yaml.safe_load(target.read_text(encoding='utf-8'))
    images = source['images']
    if {i['name'] for i in images} != {'odoo-queue', 'odoo-backup'} or len(images) != 2:
        raise ValueError('Dev must contain exactly the Odoo and backup image pair')
    if any(i['newTag'] != expected_tag for i in images):
        raise ValueError('Dev changed since acceptance; expected tag does not match')
    document['images'] = images
    target.write_text(yaml.safe_dump(document, sort_keys=False), encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expected-tag', required=True)
    parser.add_argument('--source-root', type=Path)
    parser.add_argument('--target-root', type=Path, default=ROOT)
    args = parser.parse_args()
    promote(args.expected_tag, args.target_root, args.source_root)

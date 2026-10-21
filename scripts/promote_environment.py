"""Promote a verified Dev image pair, optionally publishing semantic release tags."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import yaml

ROOT = Path(__file__).resolve().parents[1]
SEMVER = r'v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)'


def dev_images(root):
    images = yaml.safe_load((Path(root) / 'overlays/dev/kustomization.yaml').read_text(encoding='utf-8'))['images']
    if len(images) != 2 or {i['name'] for i in images} != {'odoo-queue', 'odoo-backup'}:
        raise ValueError('Dev must contain exactly the Odoo and backup image pair')
    tags = {i.get('newTag', '') for i in images}
    if len(tags) != 1 or not re.fullmatch(r'sha-[0-9a-f]{12,40}', next(iter(tags))):
        raise ValueError('Dev images must share one immutable sha- tag')
    for item in images:
        if 'digest' in item or not re.fullmatch(r'docker\.io/[a-z0-9_-]+/[a-z0-9_.-]+', item.get('newName', '')):
            raise ValueError('Expected Docker Hub image names without digest overrides')
    return images


def next_release(images):
    tags = {i['newTag'] for i in images}
    if len(tags) != 1:
        raise ValueError('Prod images must share one release tag')
    tag = next(iter(tags))
    match = re.fullmatch(SEMVER, tag)
    if match:
        major, minor, patch = map(int, match.groups())
        return f'v{major}.{minor}.{patch + 1}'
    if re.fullmatch(r'sha-[0-9a-f]{12,40}', tag):
        return 'v1.0.0'
    raise ValueError('Prod tag must be sha-* or vMAJOR.MINOR.PATCH')


def digest(reference, missing_ok=False):
    result = subprocess.run(['docker', 'buildx', 'imagetools', 'inspect', reference,
                             '--format', '{{json .Manifest}}'], capture_output=True, text=True)
    if result.returncode:
        # Authentication/network errors must never be treated as an unused tag.
        if missing_ok and any(s in result.stderr.lower() for s in ('manifest unknown', 'not found')):
            return None
        raise RuntimeError(f'Cannot inspect {reference}: {result.stderr}')
    value = json.loads(result.stdout)['digest']
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', value):
        raise ValueError('Registry returned an invalid digest')
    return value


def publish(images, release):
    pair = [(i['newName'], digest(f"{i['newName']}:{i['newTag']}")) for i in images]
    # Preflight both destinations before publishing either tag; retry partial runs safely.
    existing = [digest(f'{name}:{release}', missing_ok=True) for name, _ in pair]
    for (name, source), target in zip(pair, existing):
        if target is not None and target != source:
            raise ValueError(f'Refusing to overwrite existing release {name}:{release}')
    for (name, source), target in zip(pair, existing):
        if target is None:
            subprocess.run(['docker', 'buildx', 'imagetools', 'create', '--prefer-index=false',
                            '--tag', f'{name}:{release}', f'{name}@{source}'], check=True)
        if digest(f'{name}:{release}') != source:
            raise ValueError(f'Release digest differs from Dev: {name}')


def promote(expected_tag, root=ROOT, source_root=None, release_tag=None):
    images = dev_images(source_root or root)
    if any(i['newTag'] != expected_tag for i in images):
        raise ValueError('Dev changed since acceptance; expected tag does not match')
    target = Path(root) / 'overlays/prod/kustomization.yaml'
    document = yaml.safe_load(target.read_text(encoding='utf-8'))
    if release_tag:
        if not re.fullmatch(SEMVER, release_tag):
            raise ValueError('Release tag must be vMAJOR.MINOR.PATCH')
        publish(images, release_tag)
        images = [dict(i, newTag=release_tag) for i in images]
    document['images'] = images
    target.write_text(yaml.safe_dump(document, sort_keys=False), encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expected-tag')
    parser.add_argument('--source-root', type=Path, default=ROOT)
    parser.add_argument('--target-root', type=Path, default=ROOT)
    parser.add_argument('--resolve-dev', action='store_true')
    parser.add_argument('--release', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.resolve_dev:
        print(dev_images(args.source_root)[0]['newTag'])
    else:
        if not args.expected_tag:
            parser.error('--expected-tag is required for promotion')
        release = None
        if args.release:
            document = yaml.safe_load((args.target_root / 'overlays/prod/kustomization.yaml').read_text(encoding='utf-8'))
            release = next_release(document['images'])
        promote(args.expected_tag, args.target_root, args.source_root, release)
        if args.output:
            with args.output.open('a', encoding='utf-8') as stream:
                stream.write(f'release_tag={release or args.expected_tag}\n')

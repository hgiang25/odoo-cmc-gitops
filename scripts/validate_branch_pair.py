"""Check the shared DB budget against two actual branch snapshots, not stale sibling overlays."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from validate import render
from scripts.validate_environments import validate_environment


def validate_pair(dev_root, prod_root):
    total = sum(validate_environment(render(Path(root) / 'overlays' / env), env)
                for env, root in [('dev', dev_root), ('prod', prod_root)]) + 32
    if total > 300:
        raise ValueError(f'Cross-branch DB budget {total}/300 exceeds capacity')
    print(f'Actual dev/prod branch DB budget: {total}/300')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dev-root', required=True)
    parser.add_argument('--prod-root', required=True)
    args = parser.parse_args()
    validate_pair(args.dev_root, args.prod_root)

"""Push an already-reviewed CI commit to an explicit branch; never force push."""
import subprocess
import sys
import argparse


def run(*args, check=True):
    return subprocess.run(args, check=check)


def push(branch='main'):
    if branch not in ('main', 'dev', 'prod'):
        raise ValueError('Only main, dev or prod may be published')
    for attempt in range(3):
        if run('git', 'push', 'origin', 'HEAD:' + branch, check=False).returncode == 0:
            return
        if attempt == 2:
            raise RuntimeError('GitOps push failed after 3 attempts; check PAT/branch protection or concurrent changes')
        run('git', 'fetch', 'origin', branch)
        # Conflicts must fail for review, never choose one side automatically.
        run('git', 'rebase', 'origin/' + branch)
        run(sys.executable, 'validate.py')
    raise AssertionError('unreachable')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--branch', choices=['main', 'dev', 'prod'], default='main')
    push(parser.parse_args().branch)

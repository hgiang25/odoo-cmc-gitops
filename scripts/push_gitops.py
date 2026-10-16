"""Push an already-reviewed CI commit; retry non-overlapping main updates, never force push."""
import subprocess
import sys


def run(*args, check=True):
    return subprocess.run(args, check=check)


def push():
    for attempt in range(3):
        if run('git', 'push', 'origin', 'HEAD:main', check=False).returncode == 0:
            return
        if attempt == 2:
            raise RuntimeError('GitOps push failed after 3 attempts; check PAT/branch protection or concurrent changes')
        run('git', 'fetch', 'origin', 'main')
        # Conflicts must fail for review, never choose one side automatically.
        run('git', 'rebase', 'origin/main')
        run(sys.executable, 'validate.py')
    raise AssertionError('unreachable')


if __name__ == '__main__':
    push()

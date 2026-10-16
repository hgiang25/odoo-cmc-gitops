"""Parse workflow YAML and check embedded Bash without running deployment commands."""
import argparse
from pathlib import Path
import re
import subprocess

import yaml


def validate(folder, bash):
    count = 0
    for path in Path(folder).glob('*.yml'):
        workflow = yaml.safe_load(path.read_text(encoding='utf-8'))
        for job in workflow['jobs'].values():
            for step in job.get('steps', []):
                if 'run' not in step:
                    continue
                script = re.sub(r'\$\{\{.*?\}\}', 'EXPRESSION_VALUE', step['run'], flags=re.S)
                result = subprocess.run([bash, '-n'], input=script, text=True,
                                        capture_output=True, timeout=30)
                if result.returncode:
                    raise ValueError(f'{path.name}/{step.get("name")}: {result.stderr}')
                count += 1
    print(f'Workflow YAML and {count} embedded Bash scripts passed syntax validation.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', default='.github/workflows')
    parser.add_argument('--bash', default='bash')
    args = parser.parse_args()
    validate(args.directory, args.bash)

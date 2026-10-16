"""Verify DB-backed health through the selected environment's actual access path."""
import argparse
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.check_health import check


def verify(environment, context, domain):
    if environment == 'prod':
        if not domain:
            raise ValueError('Production domain is required')
        subprocess.run([sys.executable, str(Path(__file__).with_name('verify_private_elb.py')),
                        '--context', context], check=True, timeout=120)
        check(f'https://{domain}/web/health?db_server_status=1')
        return
    # kubectl chooses an unused port; never accidentally probe another process on 18069.
    process = subprocess.Popen(['kubectl', '--context', context, '-n', 'odoo-dev',
                                'port-forward', '--address', '127.0.0.1',
                                'svc/odoo-dev-access', ':8080'], stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True)
    lines = queue.Queue()

    def read():
        for line in process.stdout:
            if match := re.search(r'Forwarding from 127\.0\.0\.1:(\d+) ->', line):
                lines.put(int(match.group(1)))
                return
        lines.put(None)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        port = lines.get(timeout=45)
        if not port or process.poll() is not None:
            raise RuntimeError('Dev port-forward failed; inspect service endpoints and access permissions')
        check(f'http://127.0.0.1:{port}/web/health?db_server_status=1')
        if process.poll() is not None:
            raise RuntimeError('Dev port-forward exited during health verification')
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
        reader.join(timeout=2)
        process.stdout.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--environment', choices=('dev', 'prod'), required=True)
    parser.add_argument('--context', required=True)
    parser.add_argument('--domain', default='')
    args = parser.parse_args()
    verify(args.environment, args.context, args.domain)

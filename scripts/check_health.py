"""Bounded HTTP health check; HTTP 200 alone is not proof of Odoo database health."""
import argparse
import json
import time
import urllib.error
import urllib.request


def healthy(body):
    try:
        result = json.loads(body)
        return result.get('status') == 'pass' and result.get('db_server_status') is True
    except (ValueError, AttributeError):
        return False


def check(url, attempts=12):
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=15) as response:
                if response.status == 200 and healthy(response.read(65536)):
                    print('Odoo HTTP and database health: pass')
                    return
        except (urllib.error.URLError, TimeoutError):
            pass
        if attempt < attempts - 1:
            time.sleep(5)
    raise RuntimeError('Odoo endpoint did not return HTTP 200 and JSON status=pass; check TLS, routing and DB')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('url')
    check(parser.parse_args().url)

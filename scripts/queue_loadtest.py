"""Run via Odoo shell using queue_loadtest.sh; creates only OCA test jobs."""
import os
import re
from datetime import datetime, timedelta


def main(env):
    mode = os.environ['LOADTEST_MODE']
    batch = os.environ['LOADTEST_BATCH']
    expected_db = os.environ['LOADTEST_DATABASE']
    count = int(os.environ.get('LOADTEST_COUNT', '10000'))
    if mode not in ('enqueue', 'status', 'cancel'):
        raise ValueError('Mode must be enqueue, status or cancel')
    if not re.fullmatch(r'[A-Za-z0-9-]{1,64}', batch):
        raise ValueError('Batch must contain 1-64 letters, digits or hyphens')
    if env.cr.dbname != expected_db or expected_db not in ('odoo17_dev', 'odoo17_prod'):
        raise ValueError('Database does not match the requested environment')
    if not 1 <= count <= 50000:
        raise ValueError('Count must be between 1 and 50000')
    jobs = env['queue.job'].sudo()
    prefix = 'LOADTEST-' + batch + '-'
    domain = [('name', '=like', prefix + '%'),
              ('model_name', '=', 'queue.job'), ('method_name', '=', '_test_job')]
    if mode == 'enqueue':
        # One producer per batch, even if two shells start together.
        env.cr.execute('SELECT pg_try_advisory_xact_lock(hashtext(%s))', [prefix])
        if not env.cr.fetchone()[0]:
            raise ValueError('Another producer is creating this batch')
        if jobs.search_count(domain):
            raise ValueError('Batch already exists; use status or a new batch name')
        if not callable(getattr(jobs, '_test_job', None)):
            raise ValueError('This image does not provide the OCA _test_job method')
        if os.environ.get('QUEUE_JOB__NO_DELAY') or env.context.get('queue_job__no_delay'):
            raise ValueError('Synchronous queue execution must be disabled')
        # Commit once: partial/failed generation does not leave a partial batch.
        # ETA allows KEDA to observe the backlog before workers drain it.
        eta = datetime.utcnow() + timedelta(seconds=120)
        for number in range(count):
            jobs.with_delay(description=prefix + str(number + 1),
                            channel='root', priority=50, max_retries=1,
                            eta=eta, identity_key=prefix + str(number + 1))._test_job(
                                failure_rate=-1)
            if (number + 1) % 1000 == 0:
                print('Prepared', number + 1, '/', count, flush=True)
        env.cr.commit()
        print('Committed', count, 'test jobs; batch:', batch, flush=True)
    elif mode == 'cancel':
        # Lock only this batch's unstarted jobs, without blocking executing jobs.
        env.cr.execute('''SELECT id FROM queue_job
            WHERE name LIKE %s AND model_name = 'queue.job'
              AND method_name = '_test_job' AND state IN ('pending', 'enqueued')
            FOR UPDATE SKIP LOCKED''', [prefix + '%'])
        selected = jobs.browse([row[0] for row in env.cr.fetchall()])
        selected.button_cancelled()
        env.cr.commit()
        print('Cancelled', len(selected), 'unstarted jobs; running/locked jobs remain.', flush=True)
    for group in jobs.read_group(domain, ['state'], ['state']):
        print(group['state'], group['state_count'], flush=True)


if 'env' in globals():
    main(env)

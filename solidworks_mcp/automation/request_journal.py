"""Durable request receipts. Called under the executor's session lease.

Receipts are never evicted automatically: dropping a key could re-enable a
duplicate write. A completed receipt is historical evidence, not a fresh CAD
execution or a substitute for checking the current native document. A new
attempt replaces a receipt in two cases only, where there is no write to
duplicate: a create whose output file does not exist, and a completed request
whose document is back at the exact saved state it started from (a revert).
"""

import copy
import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .session_lease import SessionLease


class RequestConflict(RuntimeError):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code, self.details = code, details


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=True, allow_nan=False).encode()).hexdigest()


class RequestJournal:
    def __init__(self, path=None):
        self.path = Path(path) if path else SessionLease().path.parent / 'request-receipts.sqlite3'

    @contextmanager
    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=2)
        try:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('CREATE TABLE IF NOT EXISTS receipts (key_hash TEXT PRIMARY KEY, payload TEXT NOT NULL)')
            with db:
                yield db
        finally:
            db.close()

    def get(self, key):
        with self._connect() as db:
            row = db.execute('SELECT payload FROM receipts WHERE key_hash=?', (digest(key),)).fetchone()
        if row is None:
            return None
        value = json.loads(row[0])
        if (value.get('schema') != 1 or value.get('key') != key or
                value.get('status') not in {'RUNNING','COMPLETED','FAILED'}):
            raise RequestConflict('REQUEST_JOURNAL_INVALID', 'Receipt identity or version is invalid')
        return value

    def lookup(self, key, request):
        prior = self.get(key)
        if prior and prior['request_hash'] != digest(request):
            raise RequestConflict('IDEMPOTENCY_CONFLICT',
                                  'This key is bound to a different document, revision or payload',
                                  key=key, previous_document=prior['document'])
        return prior

    def begin(self, key, request, document, before, previous=None):
        """previous: the receipt this attempt replaces (see the module note)."""
        receipt = {'schema':1, 'key':key, 'request_hash':digest(request),
                   'document':document, 'before':before, 'status':'RUNNING',
                   'started_at':time.time()}
        with self._connect() as db:
            if previous is not None:
                receipt['attempt'] = int(previous.get('attempt', 1)) + 1
                changed = db.execute('UPDATE receipts SET payload=? WHERE key_hash=?',
                                     (json.dumps(receipt), digest(key))).rowcount
                if changed != 1:
                    raise RequestConflict('IDEMPOTENCY_CONFLICT', 'The receipt to replace is gone')
                return receipt
            try:
                db.execute('INSERT INTO receipts VALUES (?,?)', (digest(key), json.dumps(receipt)))
            except sqlite3.IntegrityError as exc:
                raise RequestConflict('IDEMPOTENCY_CONFLICT', 'Concurrent or duplicate request key') from exc
        return receipt

    def finish(self, receipt, result, after):
        clean = copy.deepcopy(result)
        # Pixel evidence belongs to the original operation; replay requests fresh views.
        clean.setdefault('data', {}).pop('images', None)
        clean['data'].pop('executor', None)
        receipt = {**receipt, 'status':'COMPLETED' if result.get('success') else 'FAILED',
                   'finished_at':time.time(), 'after':after, 'result':clean}
        raw = json.dumps(receipt, ensure_ascii=True, allow_nan=False)
        if len(raw.encode()) > 4 * 1024 * 1024:
            receipt['result'] = {'success':False, 'message':'Completed payload exceeds receipt limit',
                'error_name':'RECEIPT_RESULT_TOO_LARGE', 'error_code':-1,
                'data':{'original_success':bool(result.get('success'))}}
            raw = json.dumps(receipt)
        with self._connect() as db:
            db.execute('UPDATE receipts SET payload=? WHERE key_hash=?', (raw, digest(receipt['key'])))
        return receipt

    @staticmethod
    def replay(receipt, current):
        if receipt['status'] == 'RUNNING':
            raise RequestConflict('REQUEST_OUTCOME_UNKNOWN',
                'The previous request has no durable completion receipt; do not repeat it', key=receipt['key'])
        if not receipt.get('after') or current != receipt['after']:
            raise RequestConflict('IDEMPOTENCY_STATE_CHANGED',
                'The current document no longer matches the recorded outcome', key=receipt['key'])
        result = copy.deepcopy(receipt['result'])
        if receipt['status'] == 'FAILED':
            # The caller must not read a replayed failure as the outcome of a new attempt.
            result['message'] = ('Not executed again: this identical request already failed '
                                 '(a new idempotency_key runs it once more). ' + str(result.get('message') or ''))
        result.setdefault('data', {})['request_receipt'] = {
            'key':receipt['key'], 'replayed':True, 'status':receipt['status'],
            'original_finished_at':receipt['finished_at'], 'cad_executed':False,
            'callback_invoked':False, 'native_mutation_started':False,
            'evidence':'historical_result; request fresh observation if needed'}
        return result

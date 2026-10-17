"""Historical operation results, stored beside receipts; never authority to replay CAD.

The session journal alone owns quarantine and request receipts alone own retry
decisions. This table only answers wait_operation after reconnect.
"""

import hashlib
import json
import shutil
import sqlite3
import time
from pathlib import Path

from .request_journal import RequestJournal

RETENTION_SEC = 7 * 86400
MAX_RESULT_BYTES = 16 * 1024 * 1024
MAX_HISTORY_BYTES = 128 * 1024 * 1024
MAX_RESULTS = 128
TERMINAL = {'COMPLETED', 'RECONCILED', 'NOT_EXECUTED'}


class OperationResultError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class OperationResults:
    def __init__(self, path):
        self.journal = RequestJournal(path)
        self.assets = Path(path).parent / 'operation-result-assets'

    @staticmethod
    def _table(db):
        db.execute('CREATE TABLE IF NOT EXISTS operation_results ('
                   'id TEXT PRIMARY KEY, finished REAL NOT NULL, status TEXT NOT NULL, '
                   'size INTEGER NOT NULL, payload TEXT, sha256 TEXT NOT NULL)')

    def save(self, operation, result):
        try:
            self._save(operation, result)
        except Exception:
            self._remove_assets(operation.id)
            raise
        self.prune()

    def _save(self, operation, result):
        # Validate before creating assets, then validate again before the atomic DB commit.
        clean = json.loads(json.dumps(result, ensure_ascii=True, allow_nan=False))
        if len(json.dumps(clean).encode()) > MAX_RESULT_BYTES:
            raise OperationResultError('OPERATION_RESULT_TOO_LARGE', 'Operation result exceeds 16 MiB')
        images = (clean.get('data') or {}).get('images') or []
        expected_asset_bytes = sum(Path(item['path']).stat().st_size for item in images
                                   if isinstance(item, dict) and item.get('path') and Path(item['path']).is_file())
        if expected_asset_bytes > MAX_RESULT_BYTES:
            raise OperationResultError('OPERATION_RESULT_TOO_LARGE', 'Operation image assets exceed 16 MiB')
        asset_bytes = 0
        for index, item in enumerate(images):
            if not isinstance(item, dict) or not item.get('path'):
                continue
            source = Path(item['path'])
            if not source.is_file():
                item['history_asset_error'] = 'OPERATION_ASSET_MISSING'
                continue
            asset_bytes += source.stat().st_size
            if asset_bytes > MAX_RESULT_BYTES:
                raise OperationResultError('OPERATION_RESULT_TOO_LARGE', 'Operation image assets exceed 16 MiB')
            target = self.assets / operation.id / (str(index) + source.suffix)
            target.parent.mkdir(parents=True, exist_ok=True)
            raw = source.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            if item.get('sha256') and item['sha256'] != digest:
                item['history_asset_error'] = 'OPERATION_ASSET_CHANGED'
                continue
            target.write_bytes(raw)
            item.update(original_path=str(source), path=str(target), sha256=digest,
                        retention_expires_at=time.time() + RETENTION_SEC)
        record = {'schema': 1, 'operation_id': operation.id, 'tool': operation.name,
                  'status': operation.status, 'finished_at': time.time(),
                  'document': operation.record.get('document_hint'), 'result': clean}
        raw = json.dumps(record, ensure_ascii=True, allow_nan=False)
        size = len(raw.encode()) + asset_bytes
        try:
            with self.journal._connect() as db:
                self._table(db)
                db.execute('INSERT OR REPLACE INTO operation_results VALUES (?,?,?,?,?,?)',
                           (operation.id, record['finished_at'], operation.status, size, raw,
                            hashlib.sha256(raw.encode()).hexdigest()))
        except Exception:
            self._remove_assets(operation.id)
            raise

    def _remove_assets(self, key):
        target = self.assets / key
        if (len(key) == 32 and all(c in '0123456789abcdef' for c in key) and target.is_dir()
                and not target.is_symlink() and target.resolve().parent == self.assets.resolve()):
            shutil.rmtree(target)

    def get(self, operation_id):
        if not self.journal.path.is_file():
            return None
        try:
            with self.journal._connect() as db:
                self._table(db)
                row = db.execute('SELECT finished,status,payload,sha256 FROM operation_results WHERE id=?',
                                 (operation_id,)).fetchone()
            if row is None:
                return None
            finished, status, raw, digest = row
            if raw is None or (status in TERMINAL and time.time() - finished > RETENTION_SEC):
                raise OperationResultError('OPERATION_RESULT_EXPIRED', 'The historical result retention period ended')
            if hashlib.sha256(raw.encode()).hexdigest() != digest:
                raise ValueError('Result checksum mismatch')
            record = json.loads(raw)
            if record.get('schema') != 1 or record.get('operation_id') != operation_id:
                raise ValueError('Result schema or identity mismatch')
            result = record['result']
            if not isinstance(result, dict):
                raise ValueError('Result must be an object')
            warnings = []
            for item in (result.get('data') or {}).get('images') or []:
                if not isinstance(item, dict) or not item.get('path'):
                    continue
                path = Path(item['path'])
                code = item.get('history_asset_error')
                if not path.is_file():
                    code = 'OPERATION_ASSET_MISSING'
                elif item.get('sha256') != hashlib.sha256(path.read_bytes()).hexdigest():
                    code = 'OPERATION_ASSET_CHANGED'
                if code:
                    warnings.append({'code': code, 'path': str(path)})
                    item.pop('path', None)
            result.setdefault('data', {})['result_history'] = {
                'from_journal': True, 'operation_id': operation_id, 'status': status,
                'finished_at': finished, 'cad_executed': False, 'artifacts': warnings,
                'evidence': 'Historical result; verify current delivery hashes before acceptance.'}
            return record
        except OperationResultError:
            raise
        except (ValueError, TypeError, KeyError, sqlite3.DatabaseError, OSError) as exc:
            raise OperationResultError('OPERATION_RESULT_CORRUPT', str(exc)) from exc

    def prune(self):
        """Keep unresolved results and all request receipts; expire only completed history."""
        removed = []
        with self.journal._connect() as db:
            self._table(db)
            rows = db.execute('SELECT id,finished,status,size FROM operation_results WHERE payload IS NOT NULL '
                              'ORDER BY finished DESC').fetchall()
            count, total = 0, 0
            for key, finished, status, size in rows:
                if status not in TERMINAL:
                    continue
                count += 1
                total += size
                if time.time() - finished > RETENTION_SEC or count > MAX_RESULTS or total > MAX_HISTORY_BYTES:
                    db.execute('UPDATE operation_results SET payload=NULL,size=0 WHERE id=?', (key,))
                    removed.append(key)
            db.execute('DELETE FROM operation_results WHERE payload IS NULL AND id NOT IN '
                       '(SELECT id FROM operation_results WHERE payload IS NULL ORDER BY finished DESC LIMIT 1024)')
        for key in removed:
            # Only UUID directories created by this store are eligible for cleanup.
            self._remove_assets(key)

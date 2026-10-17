"""Preserve raw-script evidence without inferring rollback or repeating COM calls."""
from ..shape_program import ProgramError
from .runtime import structured_error


def execution_failure(exc, stage, traceback_text, stdout, stderr, watchdog=None):
    hresult = getattr(exc, 'hresult', None)
    if not isinstance(hresult, int) or isinstance(hresult, bool):
        hresult = None
    if isinstance(exc, ProgramError):
        code = exc.code
    elif isinstance(exc, SyntaxError):
        code = 'PYTHON_SYNTAX_ERROR'
    elif isinstance(exc, (SystemExit, KeyboardInterrupt)):
        code = 'PYTHON_SCRIPT_INTERRUPTED'
    elif hresult is not None:
        unsigned = hresult & 0xffffffff
        code = ('COM_OBJECT_DISCONNECTED' if unsigned in {0x80010108, 0x800401FD}
                else 'COM_MEMBER_MISMATCH' if unsigned in {0x80020003, 0x80020006}
                else 'COM_CALL_FAILED')
    else:
        code = 'PYTHON_EXECUTION_ERROR'
    executed = stage in {'execute_python', 'post_execution'}
    message = f'{type(exc).__name__}: {exc}'
    sections = [message]
    if stdout:
        sections.append('=== Output before failure ===\n' + stdout.rstrip())
    if stderr:
        sections.append('=== Stderr before failure ===\n' + stderr.rstrip())
    sections.append('=== Traceback ===\n' + traceback_text)
    details = {'exception_type': type(exc).__name__,
               'script_execution_started': executed,
               'script_state': 'PARTIAL_EFFECTS_POSSIBLE' if executed else 'NOT_EXECUTED',
               'automatic_retry': False}
    if isinstance(exc, ProgramError):
        details['native_details'] = exc.details
        if exc.details.get('native_outcome_unknown'):
            # Preserve the executor quarantine signal across diagnostic wrapping.
            details['native_outcome_unknown'] = True
    return {'success': False, 'message': '\n\n'.join(sections),
            'error_code': 999, 'error_name': 'swUnknownError',
            'data': {'error_type': type(exc).__name__, 'error_message': str(exc),
                     'traceback': traceback_text, 'stdout': stdout, 'stderr': stderr,
                     'result': None, 'watchdog': watchdog,
                     'error': structured_error(code, message, stage=stage,
                         recoverable=False, document_restored=None,
                         com_hresult=hresult, details=details)}}

"""Read SolidWorks process identities without calling COM or its UI thread."""

import ctypes
import os
from ctypes import wintypes


def process_created_at(pid):
    """Creation time of a running process; None when it has exited or never existed."""
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    try:
        handle = kernel.OpenProcess(0x1000, False, int(pid))
    except (TypeError, ValueError, OverflowError, ctypes.ArgumentError):
        return None
    if not handle:
        return None
    try:
        code = wintypes.DWORD()
        # A handle can outlive its process; 259 is STILL_ACTIVE.
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:
            return None
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            return None
        ticks = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        return ticks / 1e7 - 11644473600
    finally:
        kernel.CloseHandle(handle)


def process_alive(pid, created_at):
    """True only for the same process instance; a reused PID has another creation time."""
    if pid is None or created_at is None:
        return False
    current = process_created_at(pid)
    return current is not None and abs(current - float(created_at)) < 1e-3


def process_private_bytes(pid):
    """Committed private memory of a process in bytes, or None; no COM."""
    class Counters(ctypes.Structure):
        _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD)] + [
            (name, ctypes.c_size_t) for name in (
                'PeakWorkingSetSize', 'WorkingSetSize', 'QuotaPeakPagedPoolUsage', 'QuotaPagedPoolUsage',
                'QuotaPeakNonPagedPoolUsage', 'QuotaNonPagedPoolUsage', 'PagefileUsage',
                'PeakPagefileUsage', 'PrivateUsage')]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    try:
        handle = kernel.OpenProcess(0x1000, False, int(pid))
    except (TypeError, ValueError, OverflowError, ctypes.ArgumentError):
        return None
    if not handle:
        return None
    try:
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        query = ctypes.WinDLL('psapi', use_last_error=True).GetProcessMemoryInfo
        query.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
        if not query(handle, ctypes.byref(counters), counters.cb):
            return None
        return int(counters.PrivateUsage)
    finally:
        kernel.CloseHandle(handle)


def commit_memory():
    """System commit charge: (available bytes, limit bytes), or None."""
    class Status(ctypes.Structure):
        _fields_ = [('dwLength', wintypes.DWORD), ('dwMemoryLoad', wintypes.DWORD)] + [
            (name, ctypes.c_ulonglong) for name in (
                'ullTotalPhys', 'ullAvailPhys', 'ullTotalPageFile', 'ullAvailPageFile',
                'ullTotalVirtual', 'ullAvailVirtual', 'ullAvailExtendedVirtual')]
    status = Status()
    status.dwLength = ctypes.sizeof(status)
    if not ctypes.WinDLL('kernel32', use_last_error=True).GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    return int(status.ullAvailPageFile), int(status.ullTotalPageFile)


# SolidWorks grows with every rebuild of a long session and warns, then fails, near the commit limit.
MEMORY_WARNING_SHARE = .15      # Warn when less than this share of the commit limit is still available.
MEMORY_WARNING_GB = 24.         # ...or when SolidWorks alone holds more than this.


def solidworks_memory():
    """Memory pressure of the running SolidWorks; a COM-free health signal."""
    try:
        processes = solidworks_processes()
    except OSError:
        return None
    if not processes:
        return None
    private = [process_private_bytes(p['pid']) or 0 for p in processes]
    state = {'solidworks_private_gb': round(sum(private) / 2**30, 2)}
    commit = commit_memory()
    if commit:
        state.update(commit_available_gb=round(commit[0] / 2**30, 2), commit_limit_gb=round(commit[1] / 2**30, 2))
    low = bool(commit and commit[0] < MEMORY_WARNING_SHARE * commit[1])
    if low or state['solidworks_private_gb'] > MEMORY_WARNING_GB:
        state['warning'] = 'SOLIDWORKS_MEMORY_HIGH'
        state['action'] = ('SolidWorks holds a lot of committed memory after many rebuilds and will soon show a '
                           'low-memory dialog. Save, then run expert_tools restart_solidworks before more builds.')
    return state


OWN_PROCESS = {'pid': os.getpid(), 'created_at': process_created_at(os.getpid())}


def process_snapshot():
    """Identities and private bytes for benchmark/status; no COM or window messages."""
    try:
        return {'mcp': {**OWN_PROCESS, 'private_bytes': process_private_bytes(OWN_PROCESS['pid'])},
                'solidworks': [{**p, 'private_bytes': process_private_bytes(p['pid'])}
                              for p in solidworks_processes()]}
    except OSError as exc:
        return {'probe_error': str(exc), 'mcp': OWN_PROCESS}


def solidworks_processes():
    class ProcessEntry(ctypes.Structure):
        _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD),
                    ('th32ProcessID', wintypes.DWORD), ('th32DefaultHeapID', ctypes.c_size_t),
                    ('th32ModuleID', wintypes.DWORD), ('cntThreads', wintypes.DWORD),
                    ('th32ParentProcessID', wintypes.DWORD), ('pcPriClassBase', wintypes.LONG),
                    ('dwFlags', wintypes.DWORD), ('szExeFile', wintypes.WCHAR * 260)]

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise OSError(ctypes.get_last_error(), 'Cannot enumerate process identities')
    result = []
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        present = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while present:
            if entry.szExeFile.lower() == 'sldworks.exe':
                handle = kernel.OpenProcess(0x1000, False, entry.th32ProcessID)
                if not handle:
                    raise OSError(ctypes.get_last_error(), 'Cannot verify SolidWorks process')
                try:
                    times = [wintypes.FILETIME() for _ in range(4)]
                    if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                        raise OSError(ctypes.get_last_error(), 'Cannot verify process creation time')
                    ticks = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
                    result.append({'pid': entry.th32ProcessID,
                                   'created_at': ticks / 1e7 - 11644473600})
                finally:
                    kernel.CloseHandle(handle)
            present = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    return sorted(result, key=lambda item: item['pid'])

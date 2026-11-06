"""Hard process limits for disposable analysis workers on Linux and Windows."""
import os

_job_handle = None


def constrain(memory_bytes, cpu_seconds):
    global _job_handle
    if os.name == 'posix':
        import resource
        resource.setrlimit(resource.RLIMIT_CPU,(cpu_seconds,cpu_seconds))
        resource.setrlimit(resource.RLIMIT_AS,(memory_bytes,memory_bytes))
        return
    if os.name != 'nt':
        raise RuntimeError('No hard analysis process limit provider for this platform')
    import ctypes as c
    from ctypes import wintypes as w
    class Basic(c.Structure):
        _fields_=[('process_time',c.c_longlong),('job_time',c.c_longlong),('flags',w.DWORD),
                  ('min_work',c.c_size_t),('max_work',c.c_size_t),('active',w.DWORD),
                  ('affinity',c.c_size_t),('priority',w.DWORD),('scheduling',w.DWORD)]
    class IO(c.Structure):
        _fields_=[(name,c.c_ulonglong) for name in ('read_ops','write_ops','other_ops','read_bytes','write_bytes','other_bytes')]
    class Extended(c.Structure):
        _fields_=[('basic',Basic),('io',IO),('process_memory',c.c_size_t),('job_memory',c.c_size_t),
                  ('peak_process',c.c_size_t),('peak_job',c.c_size_t)]
    kernel=c.WinDLL('kernel32',use_last_error=True)
    kernel.CreateJobObjectW.argtypes=[c.c_void_p,w.LPCWSTR];kernel.CreateJobObjectW.restype=w.HANDLE
    kernel.SetInformationJobObject.argtypes=[w.HANDLE,c.c_int,c.c_void_p,w.DWORD]
    kernel.SetInformationJobObject.restype=w.BOOL
    kernel.AssignProcessToJobObject.argtypes=[w.HANDLE,w.HANDLE];kernel.AssignProcessToJobObject.restype=w.BOOL
    kernel.GetCurrentProcess.restype=w.HANDLE
    limits=Extended();limits.basic.flags=0x100|0x2|0x2000
    limits.basic.process_time=cpu_seconds*10_000_000;limits.process_memory=memory_bytes
    job=kernel.CreateJobObjectW(None,None)
    if not job or not kernel.SetInformationJobObject(job,9,c.byref(limits),c.sizeof(limits)) or not kernel.AssignProcessToJobObject(job,kernel.GetCurrentProcess()):
        raise c.WinError(c.get_last_error())
    _job_handle=job  # Keep until worker exit; kill-on-close prevents escaped children.

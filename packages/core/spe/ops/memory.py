"""Return completed batch-processing allocations to memory-limited workers."""
import gc
import sys


def release_processing_memory():
    gc.collect()
    arrow = sys.modules.get('pyarrow')
    if arrow is not None:
        arrow.default_memory_pool().release_unused()
    if sys.platform.startswith('linux'):
        import ctypes
        try:
            # glibc otherwise retains freed large parsing/encoding arenas.
            trim = ctypes.CDLL(None).malloc_trim
        except AttributeError:
            return  # Non-glibc Linux remains supported.
        trim.argtypes = [ctypes.c_size_t]
        trim.restype = ctypes.c_int
        trim(0)

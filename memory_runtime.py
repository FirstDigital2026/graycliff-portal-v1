"""Bound transient background-work memory without discarding durable job state."""
import ctypes
import gc
import logging
from functools import wraps

log = logging.getLogger(__name__)

def release_memory(store):
    """Drop only the rebuildable sheet cache, then release unused allocator pages."""
    store.invalidate()
    gc.collect()
    try:
        ctypes.CDLL(None).malloc_trim(0)
    except (AttributeError, OSError):
        pass  # Non-glibc hosts still get cache eviction and garbage collection.

def background_job(fn, store):
    @wraps(fn)
    def run():
        try:
            return fn()
        finally:
            release_memory(store)
    return run

def object_bytes(value, limit):
    """Measure decoded JSON up to the cache limit; stop before traversing huge sheets."""
    import sys
    seen=set()
    def size(item):
        ident=id(item)
        if ident in seen:return 0
        seen.add(ident)
        total=sys.getsizeof(item)
        if isinstance(item,dict):
            for k,v in item.items():
                total+=size(k)+size(v)
                if total>limit:break
        elif isinstance(item,(list,tuple)):
            for v in item:
                total+=size(v)
                if total>limit:break
        return total
    return size(value)

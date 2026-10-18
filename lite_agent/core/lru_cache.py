import collections
import threading


class LRUCache:
    """线程安全的 LRU 缓存，Agent 和 WorkerAgent 共用。"""

    def __init__(self, maxsize=200):
        self.cache = collections.OrderedDict()
        self.maxsize = maxsize
        self.lock = threading.Lock()

    def setdefault(self, key, default):
        with self.lock:
            if key in self.cache:
                self.cache.move_to_end(key)
                return self.cache[key]
            self.cache[key] = default
            if len(self.cache) > self.maxsize:
                self.cache.popitem(last=False)
            return self.cache[key]

    def get(self, key, default=None):
        with self.lock:
            if key in self.cache:
                self.cache.move_to_end(key)
                return self.cache[key]
            return default

    def __getitem__(self, key):
        with self.lock:
            self.cache.move_to_end(key)
            return self.cache[key]

    def __setitem__(self, key, value):
        with self.lock:
            self.cache[key] = value
            self.cache.move_to_end(key)
            if len(self.cache) > self.maxsize:
                self.cache.popitem(last=False)
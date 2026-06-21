import os
import json
import re
import glob
import sqlite3
import time
import copy
from collections.abc import Mapping

_cache = None

def load_env():
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
    if os.path.exists(env_path):
        with open(env_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    if '=' in line:
                        k, v = line.split('=', 1)
                        os.environ[k.strip()] = v.strip().strip("'").strip('"')

def _replace_vars(obj):
    if isinstance(obj, dict):
        return {k: _replace_vars(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_replace_vars(v) for v in obj]
    elif isinstance(obj, str):
        return re.sub(r'\$\{([^}]+)\}', lambda m: os.environ.get(m.group(1), ''), obj)
    return obj

def _deep_merge(dict1, dict2):
    """Deep merge dict2 into dict1. dict2 overrides dict1."""
    res = copy.deepcopy(dict1)
    for k, v in dict2.items():
        if isinstance(v, dict) and k in res and isinstance(res[k], dict):
            res[k] = _deep_merge(res[k], v)
        else:
            res[k] = copy.deepcopy(v)
    return res

class ConfigDictProxy(Mapping):
    """透明配置代理类，提供热更新（合并 SQLite / conf.d）及深层遍历支持。"""
    
    _sqlite_cache = {}
    _sqlite_ttl = 0
    
    def __init__(self, base_dict, path=""):
        self._base = base_dict
        self._path = path

    @classmethod
    def _get_sqlite_overrides(cls):
        now = time.time()
        if now < cls._sqlite_ttl:
            return cls._sqlite_cache
            
        overrides = {}
        db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'settings.db')
        if os.path.exists(db_path):
            try:
                conn = sqlite3.connect(db_path, timeout=1.0)
                cursor = conn.cursor()
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='settings'")
                if cursor.fetchone():
                    cursor.execute("SELECT key, value FROM settings")
                    for k, v in cursor.fetchall():
                        # 核心安全红线: 屏蔽一切对 edge 的重写
                        if k == 'edge' or k.startswith('edge.'):
                            continue
                            
                        # 安全防御: 防止敏感信息通过这里写入或意外覆盖
                        if 'api_key' in k or 'token' in k or 'secret' in k:
                            continue
                            
                        try:
                            parsed_v = json.loads(v)
                            parts = k.split('.')
                            curr = overrides
                            for part in parts[:-1]:
                                curr = curr.setdefault(part, {})
                            curr[parts[-1]] = parsed_v
                        except Exception:
                            pass
                conn.close()
            except Exception:
                pass
                
        cls._sqlite_cache = _replace_vars(overrides)
        cls._sqlite_ttl = now + 5.0
        return cls._sqlite_cache

    def _get_merged_dict(self):
        sqlite_all = self._get_sqlite_overrides()
        
        curr_override = sqlite_all
        if self._path:
            for part in self._path.split('.'):
                if isinstance(curr_override, dict):
                    curr_override = curr_override.get(part, {})
                else:
                    curr_override = {}
                    break
                    
        return _deep_merge(self._base, curr_override)

    def __getitem__(self, key):
        # 核心安全红线：禁止在根节点访问 edge
        if not self._path and key == "edge":
            raise KeyError("Access to 'edge' configuration is strictly blocked by Security Red Line.")
            
        merged = self._get_merged_dict()
        if key not in merged:
            raise KeyError(key)
            
        val = merged[key]
        if isinstance(val, dict):
            new_path = f"{self._path}.{key}" if self._path else key
            return ConfigDictProxy(val, new_path)
        return val

    def __iter__(self):
        return iter(self._get_merged_dict())

    def __len__(self):
        return len(self._get_merged_dict())

def load_config():
    global _cache
    if _cache is not None:
        return _cache
        
    load_env()
    base_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(base_dir, 'config.json')
    
    base_data = {}
    if os.path.exists(config_path):
        with open(config_path, 'r', encoding='utf-8') as f:
            base_data = json.load(f)
            
    # Load conf.d/*.json
    conf_d_path = os.path.join(base_dir, 'conf.d')
    if os.path.isdir(conf_d_path):
        for file in glob.glob(os.path.join(conf_d_path, '*.json')):
            try:
                with open(file, 'r', encoding='utf-8') as f:
                    module_name = os.path.splitext(os.path.basename(file))[0]
                    module_data = json.load(f)
                    base_data[module_name] = _deep_merge(base_data.get(module_name, {}), module_data)
            except Exception:
                pass
                
    # 核心安全红线：清除基底数据中的 edge
    if 'edge' in base_data:
        del base_data['edge']
        
    base_data = _replace_vars(base_data)
    _cache = ConfigDictProxy(base_data)
    return _cache

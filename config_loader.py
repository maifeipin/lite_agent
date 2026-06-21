import os
import json
import re
import glob
import sqlite3
import time
import copy
import logging

_base_cache = None
_sqlite_cache = {}
_sqlite_ttl = 0
_merged_cache = None
_merged_ttl = 0

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

def _remove_edge_keys(obj):
    """递归移除 base config 中的 edge 键"""
    if isinstance(obj, dict):
        if 'edge' in obj:
            del obj['edge']
        for k, v in obj.items():
            _remove_edge_keys(v)
    elif isinstance(obj, list):
        for item in obj:
            _remove_edge_keys(item)

def _get_base_config():
    global _base_cache
    if _base_cache is not None:
        return _base_cache
        
    load_env()
    base_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(base_dir, 'config.json')
    
    base_data = {}
    if os.path.exists(config_path):
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                base_data = json.load(f)
        except Exception as e:
            logging.warning(f"解析 config.json 失败: {e}")
            
    conf_d_path = os.path.join(base_dir, 'conf.d')
    if os.path.isdir(conf_d_path):
        for file in glob.glob(os.path.join(conf_d_path, '*.json')):
            try:
                with open(file, 'r', encoding='utf-8') as f:
                    module_name = os.path.splitext(os.path.basename(file))[0]
                    module_data = json.load(f)
                    base_data[module_name] = _deep_merge(base_data.get(module_name, {}), module_data)
            except Exception as e:
                logging.warning(f"解析 conf.d/{os.path.basename(file)} 失败: {e}")
                
    # 核心安全红线：清除基底数据中的 edge (递归清除)
    _remove_edge_keys(base_data)
    
    # 变量展开仅在受信任的 base 层进行
    _base_cache = _replace_vars(base_data)
    return _base_cache

def _get_sqlite_overrides():
    global _sqlite_cache, _sqlite_ttl
    now = time.time()
    if now < _sqlite_ttl:
        return _sqlite_cache
        
    overrides = {}
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'settings.db')
    if os.path.exists(db_path):
        try:
            conn = sqlite3.connect(db_path, timeout=1.0)
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='settings'")
            if cursor.fetchone():
                cursor.execute("SELECT key, value FROM settings")
                
                SENSITIVE_SEGS = {'api_key', 'apikey', 'token', 'secret', 'password', 'passwd', 'credential', 'authorization', 'private_key', 'webhook'}
                
                for k, v in cursor.fetchall():
                    # 核心安全红线: 分段精确判断，屏蔽一切对 edge 的重写
                    if any(seg == 'edge' for seg in k.split('.')):
                        continue
                        
                    # 安全防御: 段精确匹配，防止敏感信息写入，同时避免误伤 (如 rss.token_count)
                    if any(seg in SENSITIVE_SEGS for seg in k.split('.')):
                        continue
                        
                    try:
                        parsed_v = json.loads(v)
                        parts = k.split('.')
                        curr = overrides
                        for part in parts[:-1]:
                            curr = curr.setdefault(part, {})
                        curr[parts[-1]] = parsed_v
                    except Exception as e:
                        logging.warning(f"解析 SQLite 配置项 {k} 失败: {e}")
            conn.close()
        except Exception as e:
            logging.warning(f"读取 SQLite settings.db 失败: {e}")
            
    # 修复 P0 漏洞：绝不对 SQLite 来源的值做 _replace_vars() 展开，防止恶意读取 .env 密钥
    _sqlite_cache = overrides
    _sqlite_ttl = now + 5.0
    return _sqlite_cache

def load_config():
    """
    返回合并后的纯 dict 配置快照。
    由于返回 pure dict，完美兼容 isinstance 和 json.dumps。
    注意：返回的字典在 5 秒 TTL 内为全系统共享引用，禁止就地修改 (mutate)，否则将引发全局污染！
    """
    global _merged_cache, _merged_ttl
    now = time.time()
    if _merged_cache is not None and now < _merged_ttl:
        return _merged_cache

    base = _get_base_config()
    overrides = _get_sqlite_overrides()
    
    _merged_cache = _deep_merge(base, overrides)
    _merged_ttl = now + 5.0
    return _merged_cache

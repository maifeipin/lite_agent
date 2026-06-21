import os
import json
import re
import glob
import sqlite3
import time
import copy
import logging
import tempfile

_base_cache = None
_sqlite_cache = {}
_sqlite_ttl = 0
_merged_cache = None
_merged_ttl = 0

SENSITIVE_SEGS = {'api_key', 'apikey', 'token', 'secret', 'password', 'passwd', 'credential', 'authorization', 'private_key', 'webhook'}

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

def bust_base_cache():
    """使 _base_cache 失效，下次加载时重新读取 config.json 和 conf.d/"""
    global _base_cache, _merged_ttl
    _base_cache = None
    _merged_ttl = 0

def _init_db():
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'settings.db')
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=5.0)
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS settings (
                        key TEXT PRIMARY KEY,
                        value TEXT
                    )''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS audit_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        action TEXT,
                        target_key TEXT,
                        old_value TEXT,
                        new_value TEXT,
                        operator TEXT,
                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                    )''')
    conn.commit()
    conn.close()
    return db_path

def write_setting(key: str, value: any, operator: str = "system") -> bool:
    """更新 SQLite 配置，附带安全审计与自动 TTL 驱逐。"""
    if any(seg == 'edge' for seg in key.split('.')):
        raise ValueError("Access to 'edge' configuration is strictly blocked by Security Red Line.")
    if any(seg in SENSITIVE_SEGS for seg in key.split('.')):
        raise ValueError(f"Writing to sensitive key '{key}' via SQLite is prohibited.")
        
    db_path = _init_db()
    new_val_str = json.dumps(value, ensure_ascii=False)
    
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        cursor = conn.cursor()
        cursor.execute("BEGIN TRANSACTION")
        
        cursor.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = cursor.fetchone()
        old_val_str = row[0] if row else None
        
        cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, new_val_str))
        
        cursor.execute("""
            INSERT INTO audit_log (action, target_key, old_value, new_value, operator)
            VALUES (?, ?, ?, ?, ?)
        """, ('WRITE_SQLITE', key, old_val_str, new_val_str, operator))
        
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()
        
    global _sqlite_ttl, _merged_ttl
    _sqlite_ttl = 0
    _merged_ttl = 0
    return True

def write_conf_d(module_name: str, data: dict, operator: str = "system") -> bool:
    """将字典原子化写入 conf.d/{module_name}.json。"""
    if module_name == 'edge':
        raise ValueError("Module 'edge' is protected and cannot be written via Web UI.")
        
    base_dir = os.path.dirname(os.path.abspath(__file__))
    conf_d_path = os.path.join(base_dir, 'conf.d')
    os.makedirs(conf_d_path, exist_ok=True)
    
    target_path = os.path.join(conf_d_path, f"{module_name}.json")
    new_val_str = json.dumps(data, indent=4, ensure_ascii=False)
    
    old_val_str = None
    if os.path.exists(target_path):
        try:
            with open(target_path, 'r', encoding='utf-8') as f:
                old_val_str = f.read()
        except Exception:
            pass
            
    fd, tmp_path = tempfile.mkstemp(dir=conf_d_path, prefix=f"{module_name}_", suffix=".tmp", text=True)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(new_val_str)
    os.replace(tmp_path, target_path)
    
    try:
        db_path = _init_db()
        conn = sqlite3.connect(db_path, timeout=5.0)
        conn.execute("""
            INSERT INTO audit_log (action, target_key, old_value, new_value, operator)
            VALUES (?, ?, ?, ?, ?)
        """, ('WRITE_CONFD', module_name, old_val_str, new_val_str, operator))
        conn.commit()
        conn.close()
    except Exception as e:
        logging.warning(f"Failed to write audit_log for conf_d: {e}")
        
    bust_base_cache()
    return True

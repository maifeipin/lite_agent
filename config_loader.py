import os
import json
import re

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

def load_config():
    global _cache
    if _cache is not None:
        return _cache
        
    load_env()
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')
    if not os.path.exists(config_path):
        _cache = {}
        return _cache
    
    with open(config_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
        
    _cache = _replace_vars(data)
    return _cache

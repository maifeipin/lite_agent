"""共享路径加载器: 从 paths.json 读取, 支持环境变量覆盖。
用法:
    from paths import cfg
    url = cfg("meili_url", "http://127.0.0.1:7700")

环境变量覆盖规则: RSS_{KEY_UPPER} 优先级最高。
例: export RSS_MEILI_URL=http://other:7700
"""
import os
import json

_here = os.path.dirname(os.path.abspath(__file__))
_cfg = {}

_cfg_path = os.path.join(_here, "paths.json")
if os.path.exists(_cfg_path):
    try:
        with open(_cfg_path, encoding="utf-8") as f:
            _cfg.update(json.load(f))
    except (json.JSONDecodeError, IOError):
        pass  # 文件损坏则退回默认值


def cfg(key, default=None):
    """获取配置值。环境变量 RSS_{KEY_UPPER} > paths.json > default。
    返回值自动展开 ~ (expanduser)。"""
    env_key = "RSS_" + key.upper()
    val = os.environ.get(env_key)
    if val is None:
        val = _cfg.get(key)
    if val is None:
        val = default
    if val is not None and isinstance(val, str):
        val = os.path.expanduser(val)
    return val


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        key = sys.argv[1]
        default = sys.argv[2] if len(sys.argv) > 2 else ""
        print(cfg(key, default))
    else:
        print(json.dumps(_cfg, ensure_ascii=False, indent=2))

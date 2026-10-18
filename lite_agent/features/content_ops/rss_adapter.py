"""
RSS 内容聚合 HTTP 业务 Adapter (features.content_ops.rss_adapter).
从公共宿主解耦 RSS 简报文件读取与响应。
"""

import os
import json
from typing import Any


def _resolve_vps_work_dir(http_handler: Any) -> str:
    # 1. 环境变量优先
    env_vps_work = os.environ.get("RSS_VPS_WORK_DIR")
    if env_vps_work:
        return os.path.abspath(os.path.expanduser(env_vps_work))

    # 2. 从宿主/应用配置读取
    srv = getattr(http_handler, "server", None)
    api_srv = getattr(srv, "api_server", None)
    cfg = getattr(api_srv, "config", {}) if api_srv else {}
    if isinstance(cfg, dict):
        content_cfg = cfg.get("content_ops", {}) or cfg.get("rss", {})
        if isinstance(content_cfg, dict) and content_cfg.get("vps_work_dir"):
            return os.path.abspath(os.path.expanduser(content_cfg["vps_work_dir"]))

    # 3. 从全局配置读取
    try:
        from lite_agent.core.config_loader import load_config
        g_cfg = load_config() or {}
        g_work = (g_cfg.get("content_ops", {}) or g_cfg.get("rss", {}) or {}).get("vps_work_dir")
        if g_work:
            return os.path.abspath(os.path.expanduser(g_work))
    except Exception:
        pass

    # 4. 从项目内的 scripts/rss_topic/paths.json 解析
    from lite_agent.core.constants import PROJECT_ROOT
    repo_root = PROJECT_ROOT
    paths_json = os.path.join(repo_root, "scripts", "rss_topic", "paths.json")
    if os.path.exists(paths_json):
        try:
            with open(paths_json, "r", encoding="utf-8") as f:
                pcfg = json.load(f)
                if pcfg.get("vps_work_dir"):
                    return os.path.abspath(os.path.expanduser(pcfg["vps_work_dir"]))
        except Exception:
            pass

    # 5. 回退到用户主目录或当前工作目录
    return os.path.expanduser("~/rss_topic_work")


def handle_rss_brief(http_handler: Any, parsed_url: Any) -> None:
    vps_work_dir = _resolve_vps_work_dir(http_handler)
    brief_path = os.path.join(vps_work_dir, 'latest_brief.json')
    if not os.path.exists(brief_path):
        http_handler.send_response(404)
        if hasattr(http_handler, "_send_cors_headers"):
            http_handler._send_cors_headers()
        http_handler.send_header('Content-Type', 'application/json')
        http_handler.end_headers()
        http_handler.wfile.write(json.dumps({"detail": "Brief not found"}).encode('utf-8'))
        return

    try:
        with open(brief_path, 'r', encoding='utf-8') as f:
            content = f.read()
        http_handler.send_response(200)
        if hasattr(http_handler, "_send_cors_headers"):
            http_handler._send_cors_headers()
        http_handler.send_header('Content-Type', 'application/json')
        http_handler.end_headers()
        http_handler.wfile.write(content.encode('utf-8'))
    except Exception as e:
        http_handler.send_response(500)
        if hasattr(http_handler, "_send_cors_headers"):
            http_handler._send_cors_headers()
        http_handler.send_header('Content-Type', 'application/json')
        http_handler.end_headers()
        http_handler.wfile.write(json.dumps({"detail": str(e)}).encode('utf-8'))

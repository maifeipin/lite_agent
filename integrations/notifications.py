"""HTTP adapter for the platform alert endpoint; no domain scheduling rules."""
import json
import logging
import os
import urllib.request
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def send_im_alert(title: str, text: str, color: str = "yellow", dedup_key: Optional[str] = None) -> bool:
    """Wait for channel confirmation; HTTP acceptance alone is not delivery."""
    try:
        from core.config_loader import load_config
        api = ((load_config() or {}).get('channels') or {}).get('api') or {}
        url = os.environ.get("LITE_AGENT_ALERT_URL") or f"http://127.0.0.1:{api.get('port', 8887)}/api/v1/alert"
        token = os.environ.get("API_AUTH_TOKEN") or api.get('auth_token', '')
        payload: Dict[str, Any] = {
            "title": title,
            "text": text,
            "color": color,
            "wait_delivery": True
        }
        if dedup_key:
            payload["dedup_key"] = dedup_key
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            receipt = json.loads(response.read().decode('utf-8'))
        return isinstance(receipt, dict) and receipt.get('status') == 'delivered'
    except Exception as e:
        logger.debug(f"[send_im_alert] 推送跳过/异常: {e}")
        return False


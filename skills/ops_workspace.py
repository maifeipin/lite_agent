import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.skill_engine import skill
from features.sys_ops.workspace_service import (
    WORKSPACE,
    ops_workspace_run as _ops_workspace_run,
)
from features.content_ops.content_service import (
    ops_web_fetch as _ops_web_fetch,
)


@skill(
    name='ops_workspace_run',
    description='在安全工作区内写入并执行 Python 代码，返回执行结果。用于访问 MongoDB、数据分析等复杂操作',
    params={
        'code': {
            'type': 'string',
            'description': '完整的 Python 代码，必须包含必要的 import 和 print 输出。代码将在隔离的工作区目录中执行'
        },
        'timeout': {
            'type': 'integer',
            'description': '执行超时秒数，默认 30',
            'default': 30
        }
    }
)
def ops_workspace_run(code: str, timeout: int = 30) -> str:
    return _ops_workspace_run(code=code, timeout=timeout)


@skill(
    name='ops_web_fetch',
    description='''抓取网页 HTML 内容（仅适合静态/简单页面）。先直连，失败则走 SOCKS5 代理 (127.0.0.1:18988)。返回 HTML 纯文本摘要。

⚠️ 以下场景请优先使用 `web_clip` 而不是本工具：
  • 微信公众号文章 (mp.weixin.qq.com)
  • 知乎问答 / 专栏 (zhihu.com / zhuanlan.zhihu.com)
  • B 站视频 / 专栏 (bilibili.com)
  • 小红书 (xiaohongshu.com)
  • V2EX / Linux.do / 机器之心 / 量子位 / 网易等动态加载站点

本工具仅适合：纯静态 HTML 页面、API 接口、RSS feed、简单文档站点。
如果返回空页面 / 反爬墙 / 要求 JS 渲染，请勿反复重试本工具，应改用 web_clip。''',
    params={
        'url': {
            'type': 'string',
            'description': '目标网页 URL'
        },
        'use_proxy': {
            'type': 'boolean',
            'description': '是否使用代理（直连失败后自动尝试）',
            'default': True
        }
    }
)
def ops_web_fetch(url: str, use_proxy: bool = True) -> str:
    return _ops_web_fetch(url=url, use_proxy=use_proxy)

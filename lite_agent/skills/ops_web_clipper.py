import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from lite_agent.core.skill_engine import skill
from lite_agent.features.content_ops import content_service


@skill(
    name='web_clip',
    guest_ok=True,
    description="""网页剪藏工具（Playwright 真浏览器抓取）—— 处理 URL 转 Markdown 的首选工具。

⚠️ 本工具只能**抓取 URL**（必须传 urls 参数）。抓取的网页 > 2500 字或含图时自动上传 HedgeDoc 返回链接，force_hedgedoc=True 可强制上传。
**如果你要把已有文本/报告/Markdown 直接上传到 HedgeDoc（而非抓取网页），不要用本工具，改用 `hedgedoc_upload` 技能——它接受任意 markdown 内容直接上传。**

✅ 必须使用本工具的场景：
  • 微信公众号文章 (mp.weixin.qq.com) — 反爬墙必须绕过
  • 知乎问答 / 专栏 (zhihu.com / zhuanlan.zhihu.com) — 必须登录态
  • B 站视频 / 专栏 (bilibili.com)
  • 小红书 (xiaohongshu.com)
  • V2EX / Linux.do 论坛
  • 机器之心 / 量子位 / 网易新闻 / 联合早报等动态内容站
  • 任何 SPA / JS 渲染 / 反爬墙站点

通用：用户给出 URL 想转 Markdown / 看全文 / 转笔记 / 阅读 → 直接用本工具。
**不要**先尝试 ops_web_fetch 再回头用本工具——浪费时间。直接用 web_clip。

工作机制：调用 Mac 端 Playwright 有头浏览器，自动处理懒加载、反盗链、登录态。
内容 > 2500 字、含图、或命中 content_ops.force_upload_hosts 的域名时自动上传 HedgeDoc 并返回在线链接，否则直接返回 Markdown。
上传后的链接是**单篇开放匿名只读**的存档（邮件/账单等其他路径仍默认仅本人可见），手机/微信里可直接打开。

使用场景：
- 用户给出具体 URL："帮我把这个链接转成笔记" / "再转一个 https://..." → 直接传入 url
- 用户说批量操作："帮我把这几个链接都保存一下" → 传入逗号分隔的多个 URL（最多 3 个）
- 用户说模糊请求："帮我看看这篇文章讲了什么" + URL → 先调用 web_clip 获取内容，再自行总结

注意：
- 如果用户提到"我关注的列表"、"我的订阅"等，请先调用 rss_today 获取文章列表和链接，再用本工具批量抓取
- 同一 URL 不要重复调用，结果会被缓存，再次调用浪费时间
- 抓取失败请直接告诉用户失败原因，不要尝试 pip install playwright 或换工具重试
- 📌 返回内容里带 🔗 存档链接时：**绝对不要再用本工具去抓这个链接**（正文已经随结果给你了，重抓既浪费也会误导判断成上传失败）
- 📌 怎么用返回内容，看用户意图分两种：
    1. 用户要「总结 / 提炼 / 讲了什么 / 要点」→ 用返回内容里的**正文全文**自己总结，并在回复开头或结尾附上 🔗 存档链接
    2. 用户只是「存成笔记 / 转 Markdown」→ 把 📄 标题、📊 元信息、🔗 链接发给用户即可，不要贴全文
- ⚠️ 不要编造工具未声明的事实：是否上传以返回内容里有无 🔗 链接为准。""",
    params={
        'urls': {
            'type': 'string',
            'description': '要抓取的 URL，多个 URL 用逗号分隔。例如："https://mp.weixin.qq.com/s/xxx" 或 "https://url1.com,https://url2.com"',
        },
        'screenshot': {
            'type': 'boolean',
            'description': '是否同时截取全页长图作为兜底（默认 false，当图片无法正常显示时可设为 true）',
            'default': False,
        },
        'force_hedgedoc': {
            'type': 'boolean',
            'description': '是否强制将文章上传至 HedgeDoc（即使未达 2500 字或无图，默认 false）',
            'default': False,
        }
    },
    tags=['web', 'tool', 'rss'],
)
def web_clip(urls: str = "", screenshot: bool = False, force_hedgedoc: bool = False, url: str = None, output_format: str = 'markdown', auto_upload: bool = True) -> str:
    target_urls = urls or url or ""
    return content_service.web_clip(urls=target_urls, screenshot=screenshot, force_hedgedoc=force_hedgedoc, url=url, output_format=output_format, auto_upload=auto_upload)


@skill(
    name='hedgedoc_upload',
    description="""把任意 Markdown 文本直接上传到 HedgeDoc，返回公网可访问链接。

✅ **专用于"上传已有内容"**：当你已经生成了一份报告/总结/文档（Markdown 或纯文本），
需要把它变成一个可分享的在线链接时，用本工具。**不需要提供 URL，直接传内容即可。**

❌ 不要用本工具抓取网页（那是 web_clip 的职责）。也不要自己写 Python/requests 代码
去调 HedgeDoc API——本工具已封装登录+创建笔记+返回公网链接的全流程，直接调用即可。

💡 典型用法示例：
  场景：上游子任务生成了安全巡检报告，需要上传到 HedgeDoc 获取链接。
  正确做法：hedgedoc_upload(content="<报告的完整 Markdown 内容>")
  返回：一个 https://md.maifeipin.com/xxxx 形式的公网链接，原样转给用户即可。

注意：content 参数放完整的 Markdown 内容（支持标题、列表、表格、代码块等所有 Markdown 语法）。""",
    params={
        'content': {
            'type': 'string',
            'description': '要上传的 Markdown 文本（完整内容，支持标题/列表/表格/代码块等语法）',
        },
        'title': {
            'type': 'string',
            'description': '笔记标题，默认为 Untitled Note',
            'default': 'Untitled Note'
        }
    },
    tags=['web', 'tool'],
)
def hedgedoc_upload(content: str, title: str = 'Untitled Note') -> str:
    return content_service.upload_markdown_to_hedgedoc(content=content, title=title)

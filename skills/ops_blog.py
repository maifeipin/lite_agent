import sys, os
from typing import Optional, List
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.skill_engine import skill
from features.content_ops import content_service


@skill(
    name='blog_list_articles',
    description='获取 Halo 博客最近发布的文章列表',
    params={
        'limit': {
            'type': 'integer',
            'description': '返回的文章数量，默认 5 篇',
            'default': 5
        }
    }
)
def blog_list_articles(limit: int = 5) -> str:
    return content_service.blog_list_articles(limit=limit)


@skill(
    name='blog_publish_article',
    description='发布 Markdown 文章到 Halo 博客。⚠️注意：当你决定要发布文章时，请直接并且立即调用此技能，绝不允许先回复确认文字或告知用户“准备发布”，必须直接执行调用！',
    params={
        'title': {
            'type': 'string',
            'description': '文章标题'
        },
        'content': {
            'type': 'string',
            'description': '文章的 Markdown 正文内容'
        },
        'status': {
            'type': 'string',
            'description': '状态，PUBLISHED 或 DRAFT',
            'default': 'PUBLISHED'
        }
    }
)
def blog_publish_article(title: str, content: str, status: str = 'PUBLISHED') -> str:
    return content_service.blog_publish_article(title=title, content=content, status=status)


@skill(
    name='blog_export_articles',
    description='将 Halo 博客中的所有文章导出为 Markdown 文件到本地目录',
    params={
        'export_dir': {
            'type': 'string',
            'description': '导出目录路径，默认为 /root/blog_export',
            'default': '/root/blog_export'
        }
    }
)
def blog_export_articles(export_dir: str = '/root/blog_export', output_dir: Optional[str] = None) -> str:
    return content_service.blog_export_articles(export_dir=export_dir, output_dir=output_dir)

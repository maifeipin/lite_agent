import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from lite_agent.core.skill_engine import skill
from lite_agent.features.content_ops import content_service


@skill(
    name="web_search",
    description="在互联网上搜索最新信息、新闻、文档或实时知识。支持输入任意关键词或自然语言问题。",
    params={
        "query": {
            "type": "string",
            "description": "要搜索的关键词或问题"
        },
        "max_results": {
            "type": "integer",
            "description": "返回的最大结果数量，默认为 5，最多不超过 10",
            "default": 5
        }
    }
)
def web_search(query: str, max_results: int = 5) -> str:
    return content_service.web_search(query=query, max_results=max_results)

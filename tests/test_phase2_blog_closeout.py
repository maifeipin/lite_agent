"""Historical blog tool arguments and publishing status contracts."""
from unittest.mock import patch

def test_blog_list_preserves_limit_keyword():
    from skills import ops_blog
    from features.content_ops import content_service as c
    with patch.object(c,'halo_request',return_value={'status':200,'data':{'content':[]}}) as request:
        ops_blog.blog_list_articles(limit=3)
    assert 'size=3' in request.call_args.args[1]

def test_blog_publish_preserves_third_positional_status():
    from skills import ops_blog
    from features.content_ops import content_service as c
    with patch.object(c,'halo_request',return_value={'status':200,'data':{}}) as request:
        ops_blog.blog_publish_article('review','body','DRAFT')
    assert request.call_args.args[2]['status']=='DRAFT',request.call_args.args[2]

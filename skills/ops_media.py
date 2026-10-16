import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.skill_engine import skill
from features.content_ops import media_service


@skill(
    name='ops_media_music_stats',
    description='查询媒体库中音乐的统计信息，包括总歌曲数、缺少封面、未知歌手、未知专辑的占比等',
    params={}
)
def ops_media_music_stats() -> str:
    return media_service.get_music_stats()


@skill(
    name='ops_media_music_duplicates',
    description='扫描媒体库中重名的歌曲（相同标题和歌手），按比特率/音质比对，找出重复文件以便清理。',
    params={}
)
def ops_media_music_duplicates() -> str:
    return media_service.get_music_duplicates()

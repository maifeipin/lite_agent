import sys, os
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from skill_engine import skill

_config = None

def _media_config():
    global _config
    if _config is None:
        import config_loader
        _config = config_loader.load_config()
    return _config.get('media', {})

def _get_pg_conn():
    import psycopg2
    c = _media_config()
    return psycopg2.connect(
        host=c.get('postgres_host', '100.115.42.126'),
        port=c.get('postgres_port', 5432),
        user=c.get('postgres_user', 'postgres'),
        password=c.get('postgres_password', ''),
        database=c.get('postgres_db', 'webmusic'),
        connect_timeout=5
    )

@skill(
    name='ops_media_music_stats',
    description='查询媒体库中音乐的统计信息，包括总歌曲数、缺少封面、未知歌手、未知专辑的占比等',
    params={}
)
def ops_media_music_stats() -> str:
    query = """
    SELECT 
        COUNT(*) as total_songs,
        SUM(CASE WHEN "Artist" = 'Unknown Artist' OR "Artist" IS NULL OR "Artist" = '' THEN 1 ELSE 0 END) as unknown_artists,
        SUM(CASE WHEN "Album" = 'Unknown Album' OR "Album" IS NULL OR "Album" = '' THEN 1 ELSE 0 END) as unknown_albums,
        SUM(CASE WHEN "CoverArt" IS NULL OR "CoverArt" = '' THEN 1 ELSE 0 END) as missing_covers,
        SUM(CASE WHEN "Genre" = 'Unknown Genre' OR "Genre" IS NULL OR "Genre" = '' THEN 1 ELSE 0 END) as unknown_genres
    FROM "MediaFiles";
    """
    try:
        with _get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(query)
                row = cur.fetchone()
                if row:
                    result = f"""
总歌曲数: {row[0]}
未知歌手: {row[1]}
未知专辑: {row[2]}
缺失封面: {row[3]}
未知流派: {row[4]}
"""
                    return f"音乐库统计信息：\n{result.strip()}\n"
                return "没有找到任何音乐数据。"
    except Exception as e:
        return f"获取音乐统计信息失败: {e}"

@skill(
    name='ops_media_music_duplicates',
    description='查找媒体库中可能重复的歌曲（通过 FileHash 匹配）',
    params={
        'limit': {
            'type': 'integer',
            'description': '返回的最大组数',
            'default': 5
        }
    }
)
def ops_media_music_duplicates(limit: int = 5) -> str:
    query = f"""
    SELECT "FileHash", COUNT(*) as count, string_agg("Title" || ' - ' || "Artist", ' | ') as files
    FROM "MediaFiles"
    WHERE "FileHash" IS NOT NULL AND "FileHash" != ''
    GROUP BY "FileHash"
    HAVING COUNT(*) > 1
    ORDER BY count DESC
    LIMIT %s;
    """
    try:
        with _get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(query, (limit,))
                rows = cur.fetchall()
                if not rows:
                    return "恭喜，没有找到重复的歌曲。"
                
                lines = []
                for row in rows:
                    lines.append(f"Hash: {row[0]}, 数量: {row[1]}, 歌曲: {row[2]}")
                return f"重复歌曲查询结果（按 FileHash）：\n" + '\n'.join(lines) + '\n'
    except Exception as e:
        return f"获取重复歌曲信息失败: {e}"

"""
Media 媒体库管理业务服务 (features.content_ops.media_service)。
从 skills/ops_media.py 迁入音乐媒体库统计与查重业务逻辑。
"""
import os
from typing import Optional, Dict, Any
from lite_agent.core.config_loader import load_config


def get_media_config() -> Dict[str, Any]:
    cfg = load_config() or {}
    return cfg.get('media', {})


def get_pg_conn():
    import psycopg2
    c = get_media_config()
    return psycopg2.connect(
        host=c.get('postgres_host', '100.115.42.126'),
        port=c.get('postgres_port', 5432),
        user=c.get('postgres_user', 'postgres'),
        password=c.get('postgres_password', ''),
        database=c.get('postgres_db', 'webmusic'),
        connect_timeout=5
    )


def get_music_stats() -> str:
    """查询媒体库中音乐的统计信息，包括总歌曲数、缺少封面、未知歌手、未知专辑的占比等"""
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
        with get_pg_conn() as conn:
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


def get_music_duplicates() -> str:
    """扫描媒体库中重名的歌曲（相同标题和歌手），按比特率/音质比对"""
    query = """
    SELECT "Title", "Artist", COUNT(*) as cnt, STRING_AGG("Bitrate"::text, ', ') as bitrates
    FROM "MediaFiles"
    WHERE "Title" IS NOT NULL AND "Title" != ''
    GROUP BY "Title", "Artist"
    HAVING COUNT(*) > 1
    ORDER BY cnt DESC
    LIMIT 20;
    """
    try:
        with get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(query)
                rows = cur.fetchall()
                if not rows:
                    return "✅ 媒体库中未发现重复曲目。"
                lines = ["🎵 **重复曲目扫描列表 (Top 20):**\n"]
                for r in rows:
                    lines.append(f"- **{r[0]}** - {r[1] or '未知歌手'}: 重复 {r[2]} 份 (码率: {r[3]})")
                return "\n".join(lines)
    except Exception as e:
        return f"查询重复曲目失败: {e}"


# 兼容符号
ops_media_music_stats = get_music_stats
ops_media_music_duplicates = get_music_duplicates

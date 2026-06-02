from .base import BaseChannel

import re

def smart_truncate(text: str, max_len: int = 50) -> str:
    """截断字符串，但避免将 URL 截断"""
    if len(text) <= max_len:
        return text
    
    urls = [m.span() for m in re.finditer(r'http[s]?://\S+', text)]
    cut_pos = max_len
    for start, end in urls:
        if start < cut_pos < end:
            cut_pos = end
            break
            
    return text[:cut_pos] + ("..." if cut_pos < len(text) else "")

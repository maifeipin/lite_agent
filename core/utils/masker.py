import re

def mask_secrets(text: str) -> str:
    """Mask sensitive information like passwords and database URIs in strings for logging."""
    if not text:
        return text
    
    if not isinstance(text, str):
        try:
            text = str(text)
        except Exception:
            return text
            
    # Mask connection string passwords: generic scheme://user:password@host...
    text = re.sub(
        r'([a-zA-Z0-9_+-]+://[^:\s]+:)([^@\s]+)(@)',
        r'\g<1>***\g<3>',
        text,
        flags=re.IGNORECASE
    )
    
    # Mask key/password assignments: password=hunter2 or api_key="sk-..."
    text = re.sub(
        r'([a-zA-Z0-9_]*(?:password|api_key|apikey|token|secret|access_key|bearer)\b["\']?\]?\s*[:=]\s*["\']?)([^"\'\s,{}]+)',
        r'\g<1>***',
        text,
        flags=re.IGNORECASE
    )
    
    return text

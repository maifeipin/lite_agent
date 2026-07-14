import sys
sys.path.insert(0, '.')
from skills.ops_meili_sync import sync_meili
r = sync_meili()
print(r[:500] if r else '(empty)')

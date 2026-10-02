from pathlib import Path
import re

root = Path("app/api")
for f in sorted(root.glob("*.py")):
    text = f.read_text(encoding="utf-8")
    prefix_match = re.search(r'prefix="([^"]+)"', text)
    pfx = prefix_match.group(1) if prefix_match else ""
    for m in re.finditer(r'@router\.(get|post|put|patch|delete)\("([^"]*)"', text):
        print(f"{m.group(1).upper():6} /api/v1{pfx}{m.group(2)}")

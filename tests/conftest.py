import sys
from pathlib import Path

# 測試分資料夾存放，但彼此會共用輔助模組（如 from test_daily_pipeline import FakeRunner）。
for child in sorted(Path(__file__).parent.iterdir()):
    if child.is_dir() and child.name != "fixtures" and not child.name.startswith((".", "__")):
        sys.path.insert(0, str(child))

"""旧号池浏览器测试入口；全站布局现在由统一回归覆盖。"""
from pathlib import Path
import runpy

if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).with_name("test_global_glass_ui.py")), run_name="__main__")

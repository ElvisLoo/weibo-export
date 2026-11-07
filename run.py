"""极客港 · 微博导出工具 一键启动入口。

用法（在项目根目录 weibo-export/ 下）：
    python run.py                # 默认 http://127.0.0.1:8765
    python run.py --port 9000
等价于：
    python -m weibo_export.server
"""

import os
import sys

# 确保可从项目根直接运行（把项目根加入 sys.path）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from weibo_export.server import main

if __name__ == "__main__":
    main(sys.argv[1:])

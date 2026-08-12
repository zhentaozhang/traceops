"""`python -m scripts.worker_cli` 的兼容入口。

实现已移入 `traceops.cli.worker`：包内模块能被 `pip install -e` 后正常导入，
而 scripts/ 下的脚本需要手动设置 PYTHONPATH —— 那个隐藏依赖会让 CI 与容器
启动方式不一致。这里只保留一个转发，让文档里的旧命令继续可用。
"""
from __future__ import annotations

import sys

from traceops.cli.worker import main

if __name__ == "__main__":
    sys.exit(main())

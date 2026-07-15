import sys
import os
from loguru import logger

if __name__ == "__main__":
    # 确保在项目根目录运行
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    from process_manager import ProcessManager
    ProcessManager().run()

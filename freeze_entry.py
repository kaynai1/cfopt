#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PyInstaller 打包入口。

不能用 src/cfopt/cli.py 作为入口：它内部是相对导入（from .probe import），
PyInstaller 把脚本当 __main__ 执行时没有包上下文，会直接 ImportError。
所以单独放一个绝对导入的入口文件。
"""
import sys

from cfopt.cli import main

if __name__ == "__main__":
    sys.exit(main())

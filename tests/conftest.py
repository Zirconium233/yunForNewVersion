# -*- coding: utf-8 -*-
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

FIXTURES = os.path.join(ROOT, "tests", "fixtures")


@pytest.fixture(autouse=True)
def _restore_cwd():
    """main.py 导入时有 chdir 副作用（保留），测试之间恢复目录。"""
    cwd = os.getcwd()
    yield
    os.chdir(cwd)

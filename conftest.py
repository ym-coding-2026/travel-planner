"""pytest 根配置：把 src 加入模块搜索路径，使 `travel_planner` 可直接 import。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

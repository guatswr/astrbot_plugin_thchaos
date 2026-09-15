"""空文件，但在仓库根目录就有意义：

pytest 会把含 conftest.py 的目录加入 sys.path，测试里的 ``import logic`` 才能
在不安装本插件（也就不需要 AstrBot 运行时）的前提下跑起来。
"""

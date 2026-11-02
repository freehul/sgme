# -*- coding: utf-8 -*-
"""adapters/_shared 包：八适配器共用逻辑的真源目录（目前只有 skill_cache）。

不是运行时分发单元——各适配器把 ``skill_cache.py`` 以**源码副本**随自己的分发包
携带（``scripts/sync_shared_skill_cache.py`` 同步），因为它们安装后是独立目录。
"""

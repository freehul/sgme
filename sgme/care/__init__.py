# -*- coding: utf-8 -*-
"""sgme/care/：Care Engine 扩展模块（ST-25）。

定位（SGME-CareEngine设计-v0.1）：SGME = 被动记忆引擎，只发信号/存记忆/
提供角色数据；主动关怀（决策/触达）由消费方 agent 承担。本包 = SGME 侧
的角色层数据结构（roles.py，T-35）与关怀信号增强（T-36，待加）。

- roles.py：角色卡（CC V2 兼容子集）+ persona 物化（唯一物化例外）
"""

from sgme.care import roles as roles  # noqa: F401  （模块显式导出）

__all__ = ["roles"]

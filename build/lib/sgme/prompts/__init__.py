"""sgme/prompts：提示词版本管理包（#33）。

对外导出：
- PromptStore：版本管理器（get/list_versions/publish/activate/configure_ab）
- PromptVersion / VersionInfo / BucketCtx：数据结构
- VersionedSource：版本感知资源协议
- PromptManifestError：配置/操作异常
"""

from sgme.prompts.manager import (
    BucketCtx,
    PromptManifestError,
    PromptStore,
    PromptVersion,
    VersionInfo,
    VersionedSource,
)

__all__ = [
    "BucketCtx",
    "PromptManifestError",
    "PromptStore",
    "PromptVersion",
    "VersionInfo",
    "VersionedSource",
]

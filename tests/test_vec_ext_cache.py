"""ST-58/T-290：sqlite-vec 扩展加载改**连接级**判定（P5）。

旧实现问题：一条连接加载失败 → 全局 `_VEC_EXTENSION_LOADED` 置 False →
**其它健康连接被一起污染**，集体降级 numpy 全量余弦（28,370×1024）。
生产 18,534 次失败却 60/60 独立复现成功，正是这个放大机制在起作用。

新实现：
- `_VEC_EXTENSION_LOADED` 语义收窄为「sqlite_vec 包是否可用」（模块级事实）；
- 「本连接是否已加载」每次**问连接本身**（探测 `vec_version()` 标量函数）。

⚠️ 为什么不用「给连接打标」——两种方案均已实测否决（本文件锁定该事实）：
- ``conn._sgme_vec_ok = True`` → ``AttributeError``（C 扩展类型无 ``__dict__``）；
- ``weakref.WeakSet()`` → ``TypeError: cannot create weak reference``。
"""

from __future__ import annotations

import sqlite3
import weakref

import pytest

from sgme.data.search import vector as vector_mod


def _has_vec_pkg() -> bool:
    try:
        import sqlite_vec  # noqa: F401

        return True
    except Exception:
        return False


def _real_vec_path() -> str:
    import sqlite_vec

    return sqlite_vec.loadable_path()


def _fresh_conn() -> sqlite3.Connection:
    return sqlite3.connect(":memory:", check_same_thread=False)


needs_vec = pytest.mark.skipif(
    not _has_vec_pkg(), reason="sqlite_vec 包未安装（该环境无此依赖）"
)


def test_connection_has_no_dict_and_no_weakref():
    """锁定「为什么只能探测、不能打标」这两个事实（方案若回归会在这里先炸）。"""
    conn = _fresh_conn()
    with pytest.raises(AttributeError):
        conn._sgme_vec_ok = True
    assert not hasattr(conn, "__dict__")

    with pytest.raises(TypeError):
        weakref.ref(conn)


def test_probe_does_not_throw_on_plain_conn():
    """探测本身在普通连接上不得抛异常（未加载 = OperationalError，属正常）。"""
    conn = _fresh_conn()
    assert vector_mod._vec_already_loaded(conn) is False


def test_package_unavailable_short_circuit(monkeypatch):
    """包不可用（path=None）→ 直接 False，不做无谓探测。"""
    conn = _fresh_conn()
    monkeypatch.setattr(vector_mod, "_VEC_TRIED_INIT", True)
    monkeypatch.setattr(vector_mod, "_VEC_LOADABLE_PATH", None)
    assert vector_mod.try_load_vec_extension(conn) is False


@needs_vec
def test_same_conn_short_circuits(monkeypatch):
    """同一连接：已加载后即使 loadable_path 失效，仍走短路返回 True。"""
    conn = _fresh_conn()
    assert vector_mod.try_load_vec_extension(conn) is True

    # 切断可加载路径：若没有连接级短路，本调用必然失败
    monkeypatch.setattr(vector_mod, "_VEC_LOADABLE_PATH", "/__nonexistent_vec__.so")
    assert vector_mod.try_load_vec_extension(conn) is True, (
        "已加载的连接应走短路，不依赖 loadable_path"
    )


@needs_vec
def test_other_conn_not_polluted(monkeypatch):
    """关键回归：A 连接加载失败，不得污染 B 连接（旧实现的放大机制）。"""
    conn_a, conn_b = _fresh_conn(), _fresh_conn()

    # 强制 A 连接加载失败
    monkeypatch.setattr(vector_mod, "_VEC_TRIED_INIT", True)
    monkeypatch.setattr(vector_mod, "_VEC_LOADABLE_PATH", "/__nonexistent_vec__.so")
    assert vector_mod.try_load_vec_extension(conn_a) is False

    # 全局标志语义收窄后仍是「包可用」，不应被 A 的失败抹成 False
    monkeypatch.setattr(vector_mod, "_VEC_EXTENSION_LOADED", True)
    assert vector_mod.try_load_vec_extension(conn_b) is False  # 同样坏路径下也失败

    # 换成真实路径：B 连接应能加载成功（不被 A 连坐）
    monkeypatch.setattr(vector_mod, "_VEC_LOADABLE_PATH", _real_vec_path())
    assert vector_mod.try_load_vec_extension(conn_b) is True
    assert vector_mod._VEC_EXTENSION_LOADED is True, (
        "全局标志只表示包是否可用，不得因单连接加载失败被置 False"
    )

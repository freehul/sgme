"""tests/test_update_check.py：ST-34 服务端版本检测模块测试（T-91）。

覆盖：
1. check_latest_version 从 GitHub Releases API 解析 tag_name，与当前版本对比
2. 语义化版本对比（b4 < b5 视为新版本；同版本 = 无更新）
3. 网络不可达/API 报错 → 静默降级（返回 error，不抛异常）
4. update_check 配置段合并（默认值 + 用户覆盖）
5. health 端点扩展字段（T-92，HTTP 只增不改）
"""
from __future__ import annotations

from sgme import config as sgme_config
from sgme.operations import update_check
from sgme.operations.update_check import (
    DEFAULT_UPDATE_CHECK_CONFIG,
    _parse_version,
    check_latest_version,
    _version_gt,
)


# ---------- _parse_version ----------

def test_parse_version_standard():
    assert _parse_version("v1.0.0b4") == (1, 0, 0, "b", 4)
    assert _parse_version("1.0.0") == (1, 0, 0, None, 0)
    assert _parse_version("v0.8.0") == (0, 8, 0, None, 0)


def test_parse_version_invalid():
    assert _parse_version("") is None
    assert _parse_version("garbage") is None
    assert _parse_version("v1.0") is None


# ---------- _version_gt ----------

def test_version_gt_basic():
    # 新版本号更大
    assert _version_gt("v1.0.0b5", "v1.0.0b4") is True
    assert _version_gt("v1.0.0b4", "v1.0.0b5") is False
    # 同版本
    assert _version_gt("v1.0.0b4", "v1.0.0b4") is False
    # 主版本/次版本
    assert _version_gt("v2.0.0", "v1.9.9") is True
    assert _version_gt("v1.1.0", "v1.0.9") is True
    # 预发布 vs 正式版
    assert _version_gt("v1.0.0", "v1.0.0b5") is True  # 正式版 > 预发布


def test_version_gt_invalid():
    assert _version_gt("garbage", "v1.0.0b4") is False
    assert _version_gt("v1.0.0b4", "garbage") is False


# ---------- check_latest_version（httpx MockTransport） ----------

def test_check_latest_version_has_update(monkeypatch):
    """GitHub 返回新版 → update_available=True + latest_version。"""
    import httpx

    def handler(request):
        assert "api.github.com" in str(request.url)
        return httpx.Response(
            200,
            json={"tag_name": "v1.0.0b5", "html_url": "https://github.com/freehul/sgme/releases/tag/v1.0.0b5"},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
    monkeypatch.setattr(update_check, "_build_client", lambda: client)

    result = check_latest_version("v1.0.0b4", cfg={"update_check": {"enabled": True}})
    assert result["update_available"] is True
    assert result["latest_version"] == "v1.0.0b5"
    assert result["update_checked_at"] is not None
    assert result["update_error"] is None


def test_check_latest_version_no_update(monkeypatch):
    """GitHub 返回与当前相同版本 → update_available=False。"""
    import httpx

    def handler(request):
        return httpx.Response(200, json={"tag_name": "v1.0.0b4"})

    client = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
    monkeypatch.setattr(update_check, "_build_client", lambda: client)

    result = check_latest_version("v1.0.0b4", cfg={"update_check": {"enabled": True}})
    assert result["update_available"] is False
    assert result["latest_version"] == "v1.0.0b4"


def test_check_latest_version_api_error_silent(monkeypatch):
    """API 报错（500）→ 静默降级，不抛异常。"""
    import httpx

    def handler(request):
        return httpx.Response(500)

    client = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
    monkeypatch.setattr(update_check, "_build_client", lambda: client)

    result = check_latest_version("v1.0.0b4", cfg={"update_check": {"enabled": True}})
    assert result["update_available"] is False
    assert result["update_error"] is not None


def test_check_latest_version_network_error_silent(monkeypatch):
    """网络不可达（异常）→ 静默降级，不抛异常。"""
    import httpx

    def handler(request):
        raise httpx.ConnectError("network down")

    client = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
    monkeypatch.setattr(update_check, "_build_client", lambda: client)

    result = check_latest_version("v1.0.0b4", cfg={"update_check": {"enabled": True}})
    assert result["update_available"] is False
    assert result["update_error"] is not None


def test_check_latest_version_disabled(monkeypatch):
    """update_check.enabled=False → 不发起请求，直接返回无更新。"""
    import httpx

    def handler(request):
        raise AssertionError("should not be called")

    client = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
    monkeypatch.setattr(update_check, "_build_client", lambda: client)

    result = check_latest_version("v1.0.0b4", cfg={"update_check": {"enabled": False}})
    assert result["update_available"] is False
    assert result["update_error"] is None


# ---------- config 合并 ----------

def test_default_update_check_config():
    """默认 update_check 配置：enabled=True / interval_hours=24 / source=github。"""
    assert DEFAULT_UPDATE_CHECK_CONFIG["enabled"] is True
    assert DEFAULT_UPDATE_CHECK_CONFIG["interval_hours"] == 24
    assert DEFAULT_UPDATE_CHECK_CONFIG["source"] == "github"


def test_load_sgme_config_has_update_check():
    """load_sgme_config 返回 update_check 段（文件缺失时兜底默认）。"""
    cfg = sgme_config.load_sgme_config("/nonexistent/path/sgme.yaml")
    assert "update_check" in cfg
    assert cfg["update_check"]["enabled"] is True
    assert cfg["update_check"]["interval_hours"] == 24
    assert cfg["update_check"]["source"] == "github"


# ---------- 缓存（get_cached / refresh） ----------

def test_get_cached_first_call_checks(monkeypatch):
    """首次 get_cached → 执行检查；再次调用 → 返回缓存不重复请求。"""
    import httpx

    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json={"tag_name": "v1.0.0b5"})

    client = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
    monkeypatch.setattr(update_check, "_build_client", lambda: client)
    monkeypatch.setattr(update_check, "_cached_result", None)

    r1 = update_check.get_cached("v1.0.0b4", cfg={"update_check": {"enabled": True}})
    r2 = update_check.get_cached("v1.0.0b4", cfg={"update_check": {"enabled": True}})
    assert r1["update_available"] is True
    assert r2 == r1  # 缓存命中，结果一致
    assert calls["n"] == 1  # 只请求了一次外部 API


def test_refresh_force_recheck(monkeypatch):
    """refresh 强制重新检查（后台定时任务用）。"""
    import httpx

    versions = iter(["v1.0.0b4", "v1.0.0b5"])
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json={"tag_name": next(versions)})

    def make_client():
        # 每次调用返回新 client（避免 check_latest_version 内 close 后复用）
        return httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)

    monkeypatch.setattr(update_check, "_build_client", make_client)
    monkeypatch.setattr(update_check, "_cached_result", None)

    r1 = update_check.refresh("v1.0.0b4", cfg={"update_check": {"enabled": True}})
    r2 = update_check.refresh("v1.0.0b4", cfg={"update_check": {"enabled": True}})
    assert r1["update_available"] is False
    assert r2["update_available"] is True  # 第二次 refresh 检测到新版本
    assert calls["n"] == 2

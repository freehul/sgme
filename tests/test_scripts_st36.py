"""tests/test_scripts_st36.py：ST-36 M4a/M4b 离线脚本测试。

覆盖：
- migrate_wiki_skills：筛选（active+tags 含 skill）/frontmatter 生成/
  幂等跳过（本地文件与远端探测）/报告分节/main() 接线/API 分支（mock，不发真请求）
- find_atomic_candidates：跨技能段落命中 / 短段过滤 / 近似合并 /
  空白差异归一化 / wiki 页参与扫描 / md+json 双报告

全部离线：API 行为用 monkeypatch 替换 requests，不触碰真实端点。
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _load_script(module_name: str, file_name: str):
    """按路径加载 scripts/ 下脚本为模块（脚本非包成员，直接 import 不可靠）。"""
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS_DIR / file_name)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


mig = _load_script("st36_migrate_wiki_skills", "migrate_wiki_skills.py")
fad = _load_script("st36_find_atomic_candidates", "find_atomic_candidates.py")


# ---------------------------------------------------------------------------
# 夹具数据
# ---------------------------------------------------------------------------

# 首段（>57 字符）：用于验证 description 截断
LONG_DESC = (
    "这是一个足够长的首段用于生成描述字段，必须超过五十七个字符的长度限制"
    "才能验证截断行为是否正确无误，后面还有补充说明文字。"
)

PAGE_A_CONTENT = (
    "---\n"
    "version: 1.2.0\n"
    "---\n"
    "\n"
    "# 视频流水线\n"
    "\n"
    f"{LONG_DESC}\n"
    "\n"
    "## 步骤\n"
    "\n"
    "触发词：视频分析， 抖音下载、whisper\n"
    "\n"
    "第一步先用 yt-dlp 拉流，再交给 whisper 出稿。\n"
)

PAGE_B_BODY_PARA = "正文段落甲，长度必须大于三十个字符才会被算法当成有效段落参与原子化候选的比对流程。"
PAGE_B_CONTENT = (
    "# 编码纪律\n"
    "\n"
    f"{PAGE_B_BODY_PARA}\n"
    "\n"
    "触发词：编码纪律、写代码、review\n"
    "\n"
    "收尾段落。\n"
)

MISTAG_TITLE = "SGME 架构设计笔记"
MISTAG_CONTENT = "# SGME 架构设计笔记\n\n架构约束十二条是项目铁律，违反即返工。\n"


def build_wiki_db(db_path: Path) -> Path:
    """建假 wiki.db：2 个正常 skill 页 + 1 个误挂标签页。"""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE wiki_pages (
          page_id TEXT PRIMARY KEY,
          title TEXT NOT NULL,
          content TEXT NOT NULL,
          category TEXT,
          tags TEXT,
          status TEXT DEFAULT 'active'
        )
        """
    )
    conn.executemany(
        "INSERT INTO wiki_pages (page_id, title, content, category, tags, status)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("w-a", "skill:video-pipeline", PAGE_A_CONTENT, "skill/media", '["skill", "howto"]', "active"),
            ("w-b", "skill:coding-discipline", PAGE_B_CONTENT, "skill/dev", '["skill"]', "active"),
            ("w-x", MISTAG_TITLE, MISTAG_CONTENT, "notes", '["skill"]', "active"),
        ],
    )
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture()
def wiki_db(tmp_path: Path) -> Path:
    return build_wiki_db(tmp_path / "wiki.db")


def make_skill_tree(root: Path, mapping: dict[str, str]) -> Path:
    """造技能树：<root>/<name>/SKILL.md。"""
    for name, content in mapping.items():
        skill_dir = root / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
    return root


def read_frontmatter(text: str) -> dict[str, str]:
    """从生成的 SKILL.md 里解析 frontmatter 键值（测试专用简化版，剥引号）。"""
    lines = text.splitlines()
    assert lines[0].strip() == "---", "产物必须以 frontmatter 开头"
    end = lines.index("---", 1)
    kv: dict[str, str] = {}
    for line in lines[1:end]:
        key, _, value = line.partition(":")
        value = value.strip()
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            value = value[1:-1]
        kv[key.strip()] = value
    return kv


# ---------------------------------------------------------------------------
# M4a：筛选与 frontmatter 生成
# ---------------------------------------------------------------------------


def test_migrate_filter_and_frontmatter(wiki_db: Path, tmp_path: Path):
    """筛选：只迁 2 个带前缀页；误挂标签页进建议清单；frontmatter 字段全对。"""
    out_dir = tmp_path / "out"
    report = mig.migrate(pages=mig.load_pages_from_db(wiki_db), out_dir=out_dir)

    assert report.total == 2
    assert report.migrated == 2
    assert report.failed == 0
    # 误挂标签页：不迁移、进「建议摘标签」清单
    assert len(report.untag_suggestions) == 1
    assert MISTAG_TITLE in report.untag_suggestions[0]

    produced = mig.render_skill_md(mig.build_draft(
        mig.WikiPage(page_id="w-a", title="skill:video-pipeline",
                     content=PAGE_A_CONTENT, category="skill/media", tags=["skill"])
    ))
    fm = read_frontmatter(produced)
    assert fm["name"] == "video-pipeline"
    assert fm["version"] == "1.2.0"            # 取原文 frontmatter 的 version
    assert fm["category"] == "media"           # 原 category 去 skill/ 前缀
    assert fm["description"] == LONG_DESC[:57]  # 首个非标题段前 57 字
    assert "触发词" not in fm                   # 触发词在正文行，不在元数据里
    assert '"视频分析"' in fm["triggers"] and '"whisper"' in fm["triggers"]
    # 原文 body 原样保留
    assert "## 步骤" in produced and "第一步先用 yt-dlp 拉流" in produced

    # 无 frontmatter、无 version 行的页：默认 0.1.0；触发词从正文「触发词」行提取
    draft_b = mig.build_draft(
        mig.WikiPage(page_id="w-b", title="skill:coding-discipline",
                     content=PAGE_B_CONTENT, category="skill/dev", tags=["skill"])
    )
    assert draft_b.version == "0.1.0"
    assert draft_b.triggers == ["编码纪律", "写代码", "review"]
    assert draft_b.category == "dev"
    assert draft_b.description == PAGE_B_BODY_PARA[:57]


def test_migrate_title_without_prefix_normalized(tmp_path: Path):
    """title 无 skill: 前缀但确实该迁的场景不存在——无前缀一律进摘标签清单；
    这里验证归一化函数本身（大小写/非法字符折叠）。"""
    assert mig.normalize_title("Video Pipeline!") == "video-pipeline"
    assert mig.normalize_title("  视频流水线 ") != ""  # 中文名折叠后仍得到合法名
    assert "/" not in mig.normalize_title("a/b\\c")


def test_select_filters_inactive_and_untag():
    """status 非 active 不参与；无前缀挂 skill 标签 → 只进摘标签清单。"""
    pages = [
        mig.WikiPage(page_id="1", title="skill:a", content="x", tags=["skill"], status="active"),
        mig.WikiPage(page_id="2", title="skill:b", content="y", tags=["skill"], status="superseded"),
        mig.WikiPage(page_id="3", title="知识页", content="z", tags=["skill"], status="active"),
        mig.WikiPage(page_id="4", title="skill:c", content="w", tags=["other"], status="active"),
    ]
    skills, untag = mig.select_skill_pages(pages)
    assert [p.page_id for p in skills] == ["1"]
    assert [p.page_id for p in untag] == ["3"]


# ---------------------------------------------------------------------------
# M4a：幂等跳过 + 报告 + main() 接线
# ---------------------------------------------------------------------------


def test_migrate_idempotent_local_skip(wiki_db: Path, tmp_path: Path):
    """第二次跑同名已存在 → 全部记 skipped，文件内容不变。"""
    out_dir = tmp_path / "out"
    first = mig.migrate(pages=mig.load_pages_from_db(wiki_db), out_dir=out_dir)
    assert first.migrated == 2 and first.skipped == 0
    before = {
        p: (out_dir / p / "SKILL.md").read_text(encoding="utf-8")
        for p in ("video-pipeline", "coding-discipline")
    }

    second = mig.migrate(pages=mig.load_pages_from_db(wiki_db), out_dir=out_dir)
    assert second.total == 2
    assert second.migrated == 0
    assert second.skipped == 2
    assert all("SKIP" in entry for entry in second.entries)
    for name, content in before.items():
        assert (out_dir / name / "SKILL.md").read_text(encoding="utf-8") == content


def test_report_sections(wiki_db: Path, tmp_path: Path):
    """markdown 报告分节齐全：总览四项数字 + 明细每项一行 + 摘标签清单。"""
    report = mig.migrate(pages=mig.load_pages_from_db(wiki_db), out_dir=tmp_path / "out")
    text = mig.render_report(report)
    assert "# wiki skill 页迁移报告" in text
    assert "## 总览" in text
    assert "- 总数: 2" in text and "- 成功: 2" in text
    assert "- 跳过: 0" in text and "- 失败: 0" in text
    assert "## 明细" in text
    assert "## 建议摘标签" in text
    assert MISTAG_TITLE in text


def test_main_db_mode_end_to_end(wiki_db: Path, tmp_path: Path):
    """main() --db 模式接线：产物落盘 + 报告文件写出 + 退出码 0。"""
    out_dir = tmp_path / "out"
    report_path = tmp_path / "rpt" / "migration.md"
    rc = mig.main(["--db", str(wiki_db), "--out-dir", str(out_dir), "--report", str(report_path)])
    assert rc == 0
    assert (out_dir / "video-pipeline" / "SKILL.md").exists()
    assert (out_dir / "coding-discipline" / "SKILL.md").exists()
    assert report_path.exists()
    assert "建议摘标签" in report_path.read_text(encoding="utf-8")


def test_main_exit_code_on_failure(tmp_path: Path):
    """同名冲突页 → 记 failed → main 退出码 1。"""
    db_path = tmp_path / "conflict.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE wiki_pages (page_id TEXT PRIMARY KEY, title TEXT NOT NULL,"
        " content TEXT NOT NULL, category TEXT, tags TEXT, status TEXT DEFAULT 'active')"
    )
    body = "#" + "x" * 40
    conn.executemany(
        "INSERT INTO wiki_pages VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("c1", "skill:dup", f"# Dup\n\n{body} 第一份。\n", None, '["skill"]', "active"),
            ("c2", "skill:dup", f"# Dup\n\n{body} 第二份。\n", None, '["skill"]', "active"),
        ],
    )
    conn.commit()
    conn.close()
    rc = mig.main(["--db", str(db_path), "--out-dir", str(tmp_path / "out")])
    assert rc == 1


# ---------------------------------------------------------------------------
# M4a：API 分支（mock，不发真网络请求）
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, status_code: int = 200, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or json.dumps(payload or {})

    def json(self):
        return self._payload


def test_api_apply_flow_mock(monkeypatch, capsys):
    """--apply --api 全链路（mock）：PUT 推技能 + PATCH 置 superseded；
    PATCH 端点缺失（404）→ 打印 SKIP 提示手工处理且不算失败。"""
    calls: list[tuple[str, str]] = []

    def fake_get(url, **kw):
        calls.append(("GET", url))
        return _FakeResp(404, {})  # 远端无同名技能 → 不跳过

    def fake_put(url, **kw):
        calls.append(("PUT", url))
        assert kw["json"]["content"].startswith("---")
        return _FakeResp(200, {})

    def fake_patch(url, **kw):
        calls.append(("PATCH", url))
        return _FakeResp(404, {}, "not found")

    monkeypatch.setattr(mig.requests, "get", fake_get)
    monkeypatch.setattr(mig.requests, "put", fake_put)
    monkeypatch.setattr(mig.requests, "patch", fake_patch)

    page = mig.WikiPage(page_id="w-a", title="skill:video-pipeline",
                        content=PAGE_A_CONTENT, category="skill/media", tags=["skill"])
    report = mig.migrate(pages=[page], apply_remote=True,
                         base_url="http://nas.example:9910", api_key="test-key")
    assert report.migrated == 1 and report.failed == 0
    assert any(method == "PUT" and url.endswith("/v1/admin/skills/video-pipeline")
               for method, url in calls)
    assert any(method == "PATCH" and "/v1/wiki/pages/w-a" in url
               for method, url in calls)
    assert "SKIP 提示" in capsys.readouterr().out  # PATCH 404 → 手工处理提示


def test_api_idempotent_remote_probe_mock(monkeypatch):
    """API 幂等：GET 探测到远端同名技能 → skipped。"""

    def fake_get(url, **kw):
        assert "/v1/admin/skills/" in url
        return _FakeResp(200, {"name": "video-pipeline"})

    monkeypatch.setattr(mig.requests, "get", fake_get)
    page = mig.WikiPage(page_id="w-a", title="skill:video-pipeline",
                        content=PAGE_A_CONTENT, category="skill/media", tags=["skill"])
    report = mig.migrate(pages=[page], apply_remote=True,
                         base_url="http://nas.example:9910", api_key="test-key")
    assert report.migrated == 0 and report.skipped == 1


def test_load_pages_from_api_mock(monkeypatch):
    """列表端点解析：pages 键 + JSON 字符串 tags 都能吃下。"""

    def fake_get(url, **kw):
        assert kw["headers"] == {"X-API-Key": "test-key"}
        return _FakeResp(200, {"pages": [
            {"page_id": "p1", "title": "skill:a", "content": "全文", "category": "skill/x",
             "tags": '["skill"]', "status": "active"},
        ]})

    monkeypatch.setattr(mig.requests, "get", fake_get)
    pages = mig.load_pages_from_api("http://nas.example:9910", "test-key")
    assert len(pages) == 1
    assert pages[0].tags == ["skill"] and pages[0].content == "全文"


# ---------------------------------------------------------------------------
# M4b：扫描算法
# ---------------------------------------------------------------------------

SHARED_PARA = (
    "共享部署段落：先把服务打包上传到目标主机，再执行健康检查探针确认端口就绪，"
    "最后才切换流量入口完成发布动作。"
)
SHORT_SHARED = "短段落：重启即可。"
UNIQUE_ALPHA = "Alpha 独有段落内容，讲的是索引器的 BM25 打分与两步门验收线，与其他技能毫无交集可言。"
UNIQUE_BETA = "Beta 独有段落内容，讲的是提炼管线的分批纪律与费用门禁，同样不与别人重复出现。"


def _std_tree(root: Path) -> Path:
    fm = "---\nname: {}\nversion: 0.1.0\n---\n\n"
    return make_skill_tree(root, {
        "skills-alpha": (fm.format("skills-alpha") + f"# Alpha\n\n{SHARED_PARA}\n\n"
                         f"{SHORT_SHARED}\n\n{UNIQUE_ALPHA}\n"),
        "skills-beta": (fm.format("skills-beta") + f"# Beta\n\n{SHARED_PARA}\n\n"
                        f"{SHORT_SHARED}\n\n{UNIQUE_BETA}\n"),
        "skills-gamma": (fm.format("skills-gamma") + f"# Gamma\n\n"
                         "Gamma 独有的长段落，讨论冷启动包与交接清单的组织方式，绝不与其他技能雷同。\n"),
    })


def test_find_cross_skill_hit_and_short_filter(tmp_path: Path):
    """跨技能相同段落命中为一组；<30 字符短段被过滤不计。"""
    tree = _std_tree(tmp_path / "tree")
    skills = fad.collect_from_dirs([tree])
    assert set(skills) == {"skills-alpha", "skills-beta", "skills-gamma"}

    groups = fad.scan(skills)
    assert len(groups) == 1
    group = groups[0]
    assert set(group.skills) == {"skills-alpha", "skills-beta"}
    assert group.occurrences == 2
    assert group.similarity == 1.0

    # 短段落即使双技能重复也不进候选（防碎渣化）
    norm_short = fad.normalize(SHORT_SHARED)
    assert all(norm_short != p.norm for g in groups for p in g.paras)


def test_find_whitespace_insensitive(tmp_path: Path):
    """空白/大小写差异归一化后仍视为同一段落（SHA1 相同）。"""
    tree = make_skill_tree(tmp_path / "tree", {
        "s1": f"# S1\n\n{SHARED_PARA}\n",
        "s2": "# S2\n\n" + SHARED_PARA.replace("共享部署", "共 享 部 署").replace("。", "。\n") + "\n",
    })
    groups = fad.scan(fad.collect_from_dirs([tree]))
    assert len(groups) == 1
    assert groups[0].occurrences == 2


def test_find_near_merge_groups(tmp_path: Path):
    """近似段落（ratio≥0.85）合并为一组；无关重复另成一组。"""
    var_base = ("回写纪律：任何外部副本改动都必须回写到项目内源文件，"
                "禁止只改运行时目录里的部署拷贝而不回写仓库。")
    variant = var_base.replace("部署拷贝", "部署拷貝").replace("回写仓库", "回写源码库")
    other = "另一组完全不同的共享内容：备份策略要求每日全量加每周增量，异地副本保存在 NAS 上。"
    tree = make_skill_tree(tmp_path / "tree", {
        "s1": f"# S1\n\n{var_base}\n\n{other}\n",
        "s2": f"# S2\n\n{variant}\n\n{other}\n",
    })
    # 先确认两对确实分别命中精确/近似阈值
    assert fad.normalize(var_base) != fad.normalize(variant)
    ratio = __import__("difflib").SequenceMatcher(
        None, fad.normalize(var_base), fad.normalize(variant)).ratio()
    assert ratio >= 0.85

    groups = fad.scan(fad.collect_from_dirs([tree]))
    assert len(groups) == 2
    previews = {g.preview[:10] for g in groups}
    assert len(previews) == 2  # 两组预览互不相同


def test_find_no_candidates_when_only_unique(tmp_path: Path):
    """无跨技能重复 → 零组，报告给统计零值。"""
    tree = make_skill_tree(tmp_path / "tree", {
        "u1": f"# U1\n\n{UNIQUE_ALPHA}\n",
        "u2": f"# U2\n\n{UNIQUE_BETA}\n",
    })
    groups = fad.scan(fad.collect_from_dirs([tree]))
    assert groups == []
    text = fad.render_markdown(groups, total_skills=2)
    assert "未发现跨技能重复段落" in text


def test_find_reports_markdown_and_json(tmp_path: Path):
    """md 报告含每组的重复度/技能列表/预览/次数与结尾统计；--json 出机器可读版。"""
    tree = _std_tree(tmp_path / "tree")
    md_path = tmp_path / "rep" / "atomic.md"
    json_path = tmp_path / "rep" / "atomic.json"
    rc = fad.main(["--dirs", str(tree), "--report", str(md_path), "--json", str(json_path)])
    assert rc == 0

    md = md_path.read_text(encoding="utf-8")
    assert "# 原子化候选扫描报告" in md
    assert "## 统计" in md
    assert "- 组数: 1" in md
    assert "- 涉及技能数: 2" in md
    assert "- 潜在可抽原子数: 1" in md
    assert "`skills-alpha`" in md and "`skills-beta`" in md
    assert "- 出现次数: 2" in md

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["stats"]["groups"] == 1
    assert set(payload["groups"][0]["skills"]) == {"skills-alpha", "skills-beta"}
    assert payload["groups"][0]["occurrences"] == 2
    assert len(payload["groups"][0]["paras"][0]["sha1"]) == 40


def test_find_includes_wiki_pages(tmp_path: Path, wiki_db: Path):
    """--db 把 wiki skill 页当技能参与扫描：wiki 页段落与目录技能撞段即命中。"""
    tree = make_skill_tree(tmp_path / "tree", {
        "consumer": f"# Consumer\n\n{LONG_DESC}\n\n其他独有内容，讲的是信号消费认领与回执的时序约束细节。\n",
    })
    groups = fad.scan({**fad.collect_from_dirs([tree]), **fad.collect_from_db(wiki_db)})
    assert len(groups) == 1
    assert set(groups[0].skills) == {"consumer", "video-pipeline"}
    assert groups[0].occurrences == 2


def test_find_main_with_db_flag(tmp_path: Path, wiki_db: Path):
    """main() --dirs + --db 组合接线可用。"""
    tree = make_skill_tree(tmp_path / "tree", {
        "consumer": f"# Consumer\n\n{LONG_DESC}\n",
    })
    rc = fad.main(["--dirs", str(tree), "--db", str(wiki_db),
                   "--report", str(tmp_path / "md.md"), "--json", str(tmp_path / "j.json")])
    assert rc == 0
    payload = json.loads((tmp_path / "j.json").read_text(encoding="utf-8"))
    assert payload["stats"]["groups"] == 1

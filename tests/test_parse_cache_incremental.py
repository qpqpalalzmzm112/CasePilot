"""迭代 10 / UP-2 解析缓存 + 增量生成测试（全离线）。

验收口径（DoD 2）：
- 解析缓存：同输入二次 gen 命中缓存（日志明示「读取解析缓存」）并跳过重新解析；
- 输入变更自动失效（内容 hash 不匹配 → 重新解析）；缓存损坏同样失效；
- --only 增量生成：只重新生成指定 FR 归属用例，与上次生成结果缓存合并后整体导出；
- 等价性硬要求：同一输入下「--only 逐条触发一遍并合并」==「整份生成」的用例集
  （内容一致、顺序允许不同）；
- 未指定 --only 时行为与整份生成完全一致。
"""

from __future__ import annotations

import logging

import pytest
from typer.testing import CliRunner

from casepilot.cli import app
from casepilot.core import generator
from casepilot.core.generator import (
    generate_from_file,
    input_content_hash,
    load_case_cache,
    merge_incremental_cases,
    parse_document,
    resolve_only_targets,
)
from tests.conftest import BOUNDARY_PRD, EXAMPLES_DIR, LOGIN_PRD, NUMBERED_PRD

LOGIN_FULL_PRD = EXAMPLES_DIR / "prd_login_full.md"

runner = CliRunner()
# 等价性回归样例：单级 / 多级编号混排（FR-1.1 / FR-3 / FR-4.3 / NFR-1.2）+ 自动编号
EQUIV_SAMPLES = [LOGIN_PRD, LOGIN_FULL_PRD, NUMBERED_PRD]


def _combined(result) -> str:
    """兼容 click 不同版本的输出捕获方式。"""
    out = result.output or ""
    try:
        out += result.stderr or ""
    except (ValueError, AttributeError):
        pass
    return out


def _case_keys(cases) -> list[tuple]:
    """用例集内容指纹（全字段），排序后比较（顺序允许不同）。"""
    return sorted(
        (c.title, c.precondition, c.steps, c.expected, c.priority, c.category, c.method, c.module)
        for c in cases
    )


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """把工作目录切到临时目录：output/.cache 缓存随用例隔离。"""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _copy_sample(workdir, path):
    """把样例复制到工作目录（缓存按输入内容 hash 隔离）。"""
    target = workdir / path.name
    target.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    return target


class TestParseCache:
    """解析缓存：命中跳过解析、输入变更 / 损坏自动失效。"""

    def test_cache_file_written_under_output_cache(self, workdir, offline_env):
        prd = _copy_sample(workdir, LOGIN_PRD)
        generate_from_file(prd, mode="mock")
        digest = input_content_hash(prd)
        cache = workdir / "output" / ".cache" / f"{digest}.json"
        assert cache.is_file(), f"解析缓存应落盘 {cache}"

    def test_second_run_hits_cache_and_skips_parsing(self, workdir, offline_env, caplog, monkeypatch):
        prd = _copy_sample(workdir, LOGIN_PRD)
        calls = {"n": 0}
        original = generator.parse_document

        def counting_parse(path, source_type=None):
            calls["n"] += 1
            return original(path, source_type)

        monkeypatch.setattr(generator, "parse_document", counting_parse)
        with caplog.at_level(logging.INFO, logger="casepilot"):
            first = generate_from_file(prd, mode="mock")
            assert calls["n"] == 1
            caplog.clear()
            second = generate_from_file(prd, mode="mock")
        assert calls["n"] == 1, "输入未变时第二次生成应跳过重新解析"
        assert "读取解析缓存" in caplog.text, "日志应明示「读取解析缓存」"
        assert _case_keys(first.cases) == _case_keys(second.cases)

    def test_input_change_invalidates_cache(self, workdir, offline_env, caplog, monkeypatch):
        prd = _copy_sample(workdir, LOGIN_PRD)
        calls = {"n": 0}
        original = generator.parse_document

        def counting_parse(path, source_type=None):
            calls["n"] += 1
            return original(path, source_type)

        monkeypatch.setattr(generator, "parse_document", counting_parse)
        generate_from_file(prd, mode="mock")
        assert calls["n"] == 1
        # 输入变更：新增一个功能点 → 内容 hash 变化 → 缓存自动失效
        prd.write_text(
            prd.read_text(encoding="utf-8") + "\n- 支持扫码登录\n", encoding="utf-8"
        )
        caplog.clear()
        generate_from_file(prd, mode="mock")
        assert calls["n"] == 2, "输入变更后应重新解析"
        assert "读取解析缓存" not in caplog.text
        assert "解析缓存未命中" in caplog.text

    def test_corrupt_cache_invalidated(self, workdir, offline_env, caplog):
        prd = _copy_sample(workdir, LOGIN_PRD)
        generate_from_file(prd, mode="mock")
        digest = input_content_hash(prd)
        cache = workdir / "output" / ".cache" / f"{digest}.json"
        cache.write_text("{ 损坏的 JSON", encoding="utf-8")  # 缓存损坏
        caplog.clear()
        result = generate_from_file(prd, mode="mock")
        assert "解析缓存失效" in caplog.text
        assert len(result.cases) == 27

    def test_hash_mismatch_record_invalidated(self, workdir, offline_env, caplog):
        """缓存记录内的 hash 与当前输入不匹配（篡改 / 过期）→ 失效重解析。"""
        prd = _copy_sample(workdir, LOGIN_PRD)
        generate_from_file(prd, mode="mock")
        digest = input_content_hash(prd)
        cache = workdir / "output" / ".cache" / f"{digest}.json"
        cache.write_text(
            cache.read_text(encoding="utf-8").replace(digest, "0" * 64), encoding="utf-8"
        )
        caplog.clear()
        generate_from_file(prd, mode="mock")
        assert "解析缓存失效" in caplog.text


class TestIncrementalOnly:
    """--only 增量生成：等价性、多值、未知编号、冷启动。"""

    @pytest.mark.parametrize("path", EQUIV_SAMPLES, ids=lambda p: p.stem)
    def test_only_each_fr_merge_equals_full_generation(self, path, workdir, offline_env):
        """等价性硬要求：逐条 --only 触发一遍并合并 == 整份生成（内容一致，顺序允许不同）。"""
        prd = _copy_sample(workdir, path)
        full = generate_from_file(prd, mode="mock")
        fr_ids = [fr.id for fr in parse_document(prd).frs]
        assert fr_ids, f"{path.name} 应有 FR 清单"
        merged = None
        for fr_id in fr_ids:
            merged = generate_from_file(prd, mode="mock", only=[fr_id])
        assert merged is not None
        assert _case_keys(merged.cases) == _case_keys(full.cases), (
            f"{path.name} 逐条 --only 合并结果与整份生成不一致"
        )

    def test_only_multi_value_comma_separated(self, workdir, offline_env):
        """多值逗号分隔（FR-3 / FR-3.1 / NFR-1.2 形态）一次性触发等价。"""
        prd = _copy_sample(workdir, LOGIN_FULL_PRD)
        full = generate_from_file(prd, mode="mock")
        fr_ids = [fr.id for fr in parse_document(prd).frs]
        # 一次传入全部编号（逗号分隔）→ 合并结果 == 整份生成
        merged = generate_from_file(prd, mode="mock", only=[",".join(fr_ids)])
        assert _case_keys(merged.cases) == _case_keys(full.cases)

    def test_only_regenerates_target_fr_subset_only(self, workdir, offline_env):
        """--only FR-3 只重新生成 FR-3 归属用例，其余用例保留缓存原文。"""
        prd = _copy_sample(workdir, LOGIN_FULL_PRD)
        full = generate_from_file(prd, mode="mock")
        by_id = {fr.id: fr for fr in parse_document(prd).frs}
        cached, fresh = merge_incremental_cases(
            full.cases, full.cases, [by_id["FR-3"]]
        )
        assert fresh, "FR-3 应有归属用例"
        assert all("FR-3" in c.title or by_id["FR-3"].text in f"{c.title}{c.expected}" for c in fresh)
        assert _case_keys(cached) == _case_keys(full.cases), "替换语义下合并结果与原集一致"

    def test_unknown_only_id_chinese_error(self, workdir, offline_env):
        prd = _copy_sample(workdir, LOGIN_FULL_PRD)
        with pytest.raises(ValueError) as exc:
            generate_from_file(prd, mode="mock", only=["FR-999"])
        message = str(exc.value)
        assert "增量生成指定的需求编号不存在" in message
        assert "FR-999" in message and "FR-3" in message, "报错应列出可选编号"

    def test_only_without_cache_cold_start_warns(self, workdir, offline_env, caplog):
        """冷启动（无上次生成结果缓存）：仅导出指定 FR 归属用例并中文提示。"""
        prd = _copy_sample(workdir, LOGIN_FULL_PRD)
        with caplog.at_level(logging.WARNING, logger="casepilot"):
            result = generate_from_file(prd, mode="mock", only=["FR-3"])
        assert "未找到上次生成结果缓存" in caplog.text
        assert result.cases, "应导出指定 FR 的归属用例"
        assert "增量生成" in result.fr_coverage_summary

    def test_only_updates_case_cache(self, workdir, offline_env):
        prd = _copy_sample(workdir, LOGIN_FULL_PRD)
        generate_from_file(prd, mode="mock")
        digest = input_content_hash(prd)
        merged = generate_from_file(prd, mode="mock", only=["FR-3"])
        cached = load_case_cache(digest)
        assert cached is not None
        assert _case_keys(cached) == _case_keys(merged.cases)

    def test_no_only_behavior_unchanged(self, workdir, offline_env):
        """未指定 --only：行为与整份生成完全一致（含 FR 对账摘要）。"""
        prd = _copy_sample(workdir, BOUNDARY_PRD)
        first = generate_from_file(prd, mode="mock")
        second = generate_from_file(prd, mode="mock")
        assert _case_keys(first.cases) == _case_keys(second.cases)
        assert first.fr_coverage_summary == second.fr_coverage_summary
        assert first.fr_coverage_summary, "整份生成应输出 FR 对账摘要"


class TestOnlyCLI:
    """CLI：gen --only 退出码与增量摘要；未知编号 exit 1。"""

    def test_cli_only_incremental_summary(self, workdir, offline_env):
        prd = _copy_sample(workdir, LOGIN_FULL_PRD)
        runner.invoke(app, ["gen", "-i", str(prd), "-o", str(workdir / "a.xlsx")])
        result = runner.invoke(
            app, ["gen", "-i", str(prd), "-o", str(workdir / "b.xlsx"), "--only", "FR-3"]
        )
        assert result.exit_code == 0, _combined(result)
        text = _combined(result)
        assert "增量生成摘要" in text and "FR-3" in text

    def test_cli_only_unknown_id_exit_1(self, workdir, offline_env):
        prd = _copy_sample(workdir, LOGIN_FULL_PRD)
        result = runner.invoke(
            app, ["gen", "-i", str(prd), "-o", str(workdir / "c.xlsx"), "--only", "FR-999"]
        )
        assert result.exit_code == 1
        assert "增量生成指定的需求编号不存在" in _combined(result)


class TestResolveOnlyTargets:
    """--only 编号解析：逗号多值、去重、逐字比对。"""

    def test_parse_multi_values(self):
        profile = parse_document(LOGIN_FULL_PRD)
        targets = resolve_only_targets(profile, ["FR-3,FR-4.3", "NFR-1.2"])
        assert [fr.id for fr in targets] == ["FR-3", "FR-4.3", "NFR-1.2"]

    def test_dedup_keeps_order(self):
        profile = parse_document(LOGIN_FULL_PRD)
        targets = resolve_only_targets(profile, ["FR-3", "FR-3,FR-1.1"])
        assert [fr.id for fr in targets] == ["FR-3", "FR-1.1"]

    def test_verbatim_match_required(self):
        """编号逐字比对：fr-3（小写）不匹配 FR-3。"""
        profile = parse_document(LOGIN_FULL_PRD)
        with pytest.raises(ValueError):
            resolve_only_targets(profile, ["fr-3"])

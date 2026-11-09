"""迭代 9 验收定点修复回归（F1-F5，全离线）。

验收 fixture（导演 Agent 自造跨域输入）：
  FR-1：单笔转账金额超过 10000 元或当日累计转账达到 50000 元时，触发短信验证码二次确认。
  FR-2：充电电价在 07:00 切换为谷电价格，22:00 切换为峰电价格。
  FR-3：系统应具备良好的用户体验。
  NFR-1：系统支持 10 万台设备同时在线。

固化断言（与验收口径逐条对应）：
- F1 换皮同义反复命中：judge find_tautologies 语义类抓住「复述条款 + 零可验证行为」
  三句式；fixture 产物经模板整改后 0 同义反复；
- F2 FR-3 未覆盖：纯质量属性条款不产出通用条目，FR-3 状态 = ⚠ 未覆盖（已列清单），
  产物零条 FR-3 用例；
- F3 标题 / 引文完整：「条款「…」」逐字保留完整条款文本，标题为完整语义短语
  （绝不从数字 / 词语中间断开）；
- F4 引文豁免：「」逐字引文内模糊词（真实 PRD 常含「或」）不算命中，fixture
  生成后模糊词命中 0 处、judge 不为引文里的「或」扣分；
- F5 时点不标假设：时点 ±1 推导（06:59 / 07:01）属测试推导，不打「（设计假设）」。
"""

from __future__ import annotations

import pytest

from casepilot.core import quality
from casepilot.core.generator import (
    format_fr_coverage_report,
    generate_from_file,
    parse_document,
    reconcile_fr_coverage,
)
from casepilot.core.quality import find_fuzzy_words, find_tautologies, find_weak_assertions
from evals.judge import rule_review

# 验收 fixture（导演 Agent 自造跨域输入，逐字保留）
FIXTURE = """# 转账与充电混合需求（验收用自造跨域输入）
## 功能需求
FR-1：单笔转账金额超过 10000 元或当日累计转账达到 50000 元时，触发短信验证码二次确认。
FR-2：充电电价在 07:00 切换为谷电价格，22:00 切换为峰电价格。
FR-3：系统应具备良好的用户体验。
## 非功能需求
NFR-1：系统支持 10 万台设备同时在线。
"""

# F1 三个换皮坏句式（复述条款 + 零可验证行为），judge 必须抓住
RESTATEMENT_1 = "条款「支持账号密码登录」：返回结果与「支持账号密码登录」的需求定义一致"
RESTATEMENT_2 = "条款「充电电价切换」：系统按「充电电价切换」的需求规则接受有效输入并完成处理"
RESTATEMENT_3 = (
    "条款「充电电价切换」：系统拒绝「充电电价切换」的无效等价类输入；"
    "条款「充电电价切换」：返回结果与「充电电价切换」的拒绝定义一致"
)


@pytest.fixture
def fixture_result(tmp_path):
    """生成 fixture 用例集（mock 模式，确定性）。"""
    p = tmp_path / "fix.md"
    p.write_text(FIXTURE, encoding="utf-8")
    result = generate_from_file(p, mode="mock")
    profile = parse_document(p)
    return p, profile, result


class TestF1TautologySemantics:
    """F1：同义反复判定为语义类，抓住换皮句式（剥离「」引文后无验证点）。"""

    def test_restatement_class_flagged(self):
        assert len(find_tautologies(RESTATEMENT_1)) == 1
        assert len(find_tautologies(RESTATEMENT_2)) == 1
        assert len(find_tautologies(RESTATEMENT_3)) == 2

    def test_fixture_output_zero_tautology(self, fixture_result):
        """模板整改后 fixture 产物 0 同义反复（通用预期已含具体验证点）。"""
        _, _, result = fixture_result
        for case in result.cases:
            assert find_tautologies(case.expected) == [], case.title

    def test_judge_deducts_on_restatement(self):
        report = rule_review(
            [
                {
                    "title": "验证登录结果",
                    "precondition": "用户已注册且账号状态正常",
                    "steps": "1. 打开登录页\n2. 输入账号密码\n3. 点击登录",
                    "expected": RESTATEMENT_1,
                    "priority": "P1",
                    "category": "功能",
                    "method": "等价类",
                    "module": "登录/功能验证",
                }
            ]
        )
        assert report.scores["可执行性"] <= 2.0
        assert any("同义反复" in s for s in report.suggestions)


class TestF2FR3Uncovered:
    """F2：纯质量属性条款不产通用条目，FR-3 进未覆盖清单。"""

    def test_fr3_status_uncovered(self, fixture_result):
        _, profile, result = fixture_result
        _, report = reconcile_fr_coverage(profile, result.cases)
        by_id = {item.fr_id: item for item in report.items}
        assert by_id["FR-3"].status == "⚠ 未覆盖（已列清单）"
        assert by_id["FR-3"].suggestion.startswith("建议")

    def test_fr3_zero_cases_in_output(self, fixture_result):
        """产物零条 FR-3 用例（标题带 [FR-3] 追溯前缀的用例）——
        场景法流程步引用 FR-3 文本不算 FR-3 用例（不加 [FR-3] 前缀）。"""
        _, _, result = fixture_result
        for case in result.cases:
            assert "[FR-3]" not in case.title, case.title

    def test_fr3_registered_with_design_suggestion(self, fixture_result):
        _, profile, result = fixture_result
        _, report = reconcile_fr_coverage(profile, result.cases)
        text = format_fr_coverage_report(report)
        assert "未覆盖清单" in text
        assert "FR-3" in text
        # 具体设计建议（非空话）：指向缺失的验收要素
        item = next(i for i in report.items if i.fr_id == "FR-3")
        assert "状态码" in item.suggestion or "字段" in item.suggestion or "数值" in item.suggestion or "界面元素" in item.suggestion


class TestF3TitleAndCitationIntact:
    """F3：条款引文完整、标题为完整语义短语（不从数字 / 词语中间断开）。"""

    def test_no_truncated_title(self, fixture_result):
        _, _, result = fixture_result
        for case in result.cases:
            t = case.title
            # 坏断片特征（F3 验收点名）：从数字 / 词语中间断开、丢前缀
            assert not t.startswith("元或"), t
            assert not t.startswith("00 切换"), t
            if "当日累计转账达到" in t:
                assert "单笔转账金额超过" in t, f"标题丢前缀：{t}"
            if "切换为谷电价格" in t:
                assert "充电电价" in t, f"标题丢前缀：{t}"

    def test_citation_keeps_full_clause(self, fixture_result):
        """「条款「…」」引文须是 PRD 条款原文完整子串，且不得省略号截断。"""
        import re

        _, _, result = fixture_result
        source = FIXTURE
        for case in result.cases:
            for text in (case.title, case.expected, case.steps):
                for m in re.finditer(r"「([^」]*)」", text):
                    quoted = m.group(1)
                    assert "…" not in quoted, f"引文含省略号截断：{quoted[:40]}"
                for m in re.finditer(r"条款「([^」]*)」", text):
                    quoted = m.group(1)
                    # 流程引用「完成「X」」剥包装后同样须是 PRD 条款完整子串
                    inner = quoted.replace("完成「", "").replace("」", "")
                    assert quoted in source or inner in source, (
                        f"引文非完整条款原文：{quoted[:40]}"
                    )

    def test_boundary_title_has_threshold_context(self, fixture_result):
        """标题保留阈值上下文（完整语义短语），不从数字中间断开。"""
        _, _, result = fixture_result
        titles = [c.title for c in result.cases]
        assert any("50000" in t for t in titles), "多阈值边界标题应含阈值上下文"
        assert any("10000" in t for t in titles), "多阈值边界标题应含阈值上下文"


class TestF4QuoteExemption:
    """F4：「」引文内模糊词豁免，fixture 模糊词命中 0。"""

    def test_fixture_zero_fuzzy(self, fixture_result):
        _, _, result = fixture_result
        for case in result.cases:
            assert find_fuzzy_words(case.expected) == [], case.title

    def test_quote_internal_or_not_flagged(self):
        # 条款原文含「或」被逐字引用后不算命中（真实 PRD 常含「或」）
        assert find_fuzzy_words("条款「单笔转账超过 10000 元或当日累计 50000 元」：输入 10000 元被接受") == []
        # 引文外的「或」仍命中
        assert find_fuzzy_words("结果为成功或失败") == ["或"]

    def test_judge_not_penalized_for_quoted_or(self, fixture_result):
        _, _, result = fixture_result
        for case in result.cases:
            report = rule_review(
                [
                    {
                        "title": case.title,
                        "precondition": case.precondition,
                        "steps": case.steps,
                        "expected": case.expected,
                        "priority": case.priority,
                        "category": case.category,
                        "method": case.method,
                        "module": case.module,
                    }
                ]
            )
            assert "模糊词" not in "".join(report.suggestions), case.title


class TestF5TimepointNotAssumption:
    """F5：时点 ±1 推导（06:59 / 07:01）是测试推导，不打「（设计假设）」。"""

    def test_timepoint_derivation_not_tagged(self, fixture_result):
        _, _, result = fixture_result
        timepoint_cases = [
            c for c in result.cases if "切换" in c.title or "切换时刻" in c.expected
        ]
        assert timepoint_cases, "应产出时点切换用例"
        for case in timepoint_cases:
            assert case.assumption is False, f"时点 ±1 推导不应标假设：{case.title}"

    def test_derived_behavior_still_tagged(self):
        """PRD 未定义的工程行为（限流降级类）仍打「（设计假设）」（F5 不误伤）。"""
        from casepilot.core.generator import _tag_assumption_cases
        from casepilot.core.schema import TestCase

        profile = parse_document_text()
        cases = [
            TestCase.model_validate(
                {
                    "title": "[FR-1] 验证超容行为（衍生设计）",
                    "precondition": "设备已完成配网",
                    "steps": "1. 设备发起上线注册\n2. 观察注册结果",
                    "expected": "支持设备上线注册；触发限流降级，服务进入只读模式",
                    "priority": "P1",
                    "category": "功能",
                    "method": "等价类",
                    "module": "物联网平台/功能验证",
                }
            )
        ]
        tagged = _tag_assumption_cases(cases, FIXTURE, profile)
        assert tagged[0].assumption is True

    def test_fixture_zero_assumption_marks(self, fixture_result):
        """fixture mock 产物零「（设计假设）」标注（时点推导已豁免）。"""
        _, _, result = fixture_result
        for case in result.cases:
            assert case.assumption is False, case.title


class TestF6TautologyOrAndBehaviorElement:
    """F6：死短语 OR 语义（无条件命中）+ 等价类/异常/性能模板用条款行为要素作验证点。"""

    def test_hard_tautology_unconditional_hit(self):
        """验收 a：「…定义一致」「…状态可逐项核对」死短语出现即命中，不论句中其他内容。"""
        # 任务点名句式（纯复述空话）
        assert len(find_tautologies("返回结果与「X」的需求定义一致且处理状态可逐项核对")) == 1
        # 混入具体验证点（数字/数据/状态码/界面）仍命中——OR 语义，不再「有验证点就豁免」
        assert len(
            find_tautologies("返回结果与「X」的需求定义一致，数据完成更新，状态码 200，页面显示「成功」")
        ) == 1
        assert len(find_tautologies("处理状态可逐项核对，锁定 30 分钟")) == 1
        # 三类死短语各变体均无条件命中
        for frag in (
            "返回结果与「X」的需求定义一致",
            "返回结果与「X」的规则定义一致",
            "返回结果与「X」的拒绝定义一致",
            "处理状态可逐项核对",
        ):
            assert find_tautologies(frag), frag

    def test_mock_output_zero_diluted_tautology(self, fixture_result):
        """验收 b：mock 产物零「返回结果与…定义一致」「处理状态可逐项核对」「对应数据完成更新」。"""
        _, _, result = fixture_result
        for case in result.cases:
            for bad in ("定义一致", "状态可逐项核对", "对应数据完成更新", "数据变化可见"):
                assert bad not in case.expected, f"{case.title} 残留稀释套话「{bad}」"

    def test_equivalence_expected_uses_clause_behavior_element(self, fixture_result):
        """验收 c：等价类/性能预期引用条款行为要素作验证点（非通用后缀）。"""
        _, _, result = fixture_result
        exp_by = {c.title: c.expected for c in result.cases}
        # FR-1 有效等价类含条款行为要素「短信验证码」
        fr1 = next(e for t, e in exp_by.items() if "触发短信验证码" in t)
        assert "短信验证码" in fr1
        # FR-2 有效等价类含「谷电」或「峰电」
        fr2 = next(e for t, e in exp_by.items() if "切换为谷电" in t)
        assert "谷电" in fr2 or "峰电" in fr2
        # NFR-1 含「10万台」/「10 万台」判定标准（勿回归）
        nfr1 = next(
            e for t, e in exp_by.items() if "系统支持 10 万台设备同时在线" in t and "有效" in t
        )
        assert "10万台" in nfr1 or "10 万台" in nfr1


def parse_document_text():
    """fixture 文本解析为 DocumentProfile（供 F5 假设判定测试）。"""
    from casepilot.adapters import prd as prd_adapter

    return prd_adapter.parse_text(FIXTURE, source="fix.md")

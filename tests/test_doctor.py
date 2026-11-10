"""迭代 10 / UP-1 `casepilot doctor` 本地质检测试（全离线）。

验收口径（DoD 1）：
- 坏例集（模糊词 / 弱断言 / 同义反复 / 断链编号 / 缺两侧值）全部命中且中文报告点名；
- 好例零误伤（6 样例 mock 产物逐个过 doctor，零阻断零警告）；
- 退出码正确（有阻断 exit 1，仅警告 / 全过 exit 0）；
- 默认零 LLM 调用（monkeypatch 网关替身断言零调用）；
- --llm-review 无 key 优雅跳过（中文提示），有 key 只复核本地可疑条目并合并进报告。
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from casepilot.cli import app
from casepilot.core import doctor as doctor_core
from casepilot.core import quality
from casepilot.core.doctor import (
    SEVERITY_BLOCK,
    SEVERITY_INFO,
    SEVERITY_WARN,
    case_title_tags,
    find_missing_adjacent,
    load_cases,
    run_local_checks,
    run_llm_review,
)
from casepilot.core.generator import generate_from_file
from casepilot.core.schema import validate_cases
from casepilot.exporters.markdown_export import export_markdown
from casepilot.exporters.zentao_excel import export_zentao_excel
from tests.conftest import (
    BOUNDARY_PRD,
    EXAMPLES_DIR,
    LOGIN_PRD,
    NUMBERED_PRD,
    PETSTORE_OPENAPI,
    SECURITY_PERF_PRD,
)

LOGIN_FULL_PRD = EXAMPLES_DIR / "prd_login_full.md"
runner = CliRunner()
ALL_SAMPLES = [LOGIN_PRD, PETSTORE_OPENAPI, BOUNDARY_PRD, SECURITY_PERF_PRD, NUMBERED_PRD, LOGIN_FULL_PRD]

# --source 用的 PRD（仅含 FR-1 / FR-2 两个原文编号，供断链 / 未引用比对）
SOURCE_PRD = """# 登录模块需求
## 功能需求
FR-1：支持账号密码登录，登录成功后跳转首页。
FR-2：连续 3 次输错密码后锁定账号 5 分钟。
"""


def _combined(result) -> str:
    """兼容 click 不同版本的输出捕获方式。"""
    out = result.output or ""
    try:
        out += result.stderr or ""
    except (ValueError, AttributeError):
        pass
    return out


def _case(title: str, expected: str, method: str = "等价类", **overrides) -> dict:
    """一条用例 dict（8 要素合法，可按需覆盖字段）。"""
    data = {
        "title": title,
        "precondition": "用户已注册且账号状态正常",
        "steps": "1. 打开登录页\n2. 输入账号密码\n3. 点击登录",
        "expected": expected,
        "priority": "P1",
        "category": "功能",
        "method": method,
        "module": "登录/功能验证",
    }
    data.update(overrides)
    return data


# 坏例集：5 类缺陷各 1 条（互相不串扰，逐条可断言命中）
BAD_CASES = [
    _case("验证合法与非法输入提交（模糊词坏例）", "输入合法或非法均可提交"),
    _case("验证提交后界面（弱断言坏例）", "提交后界面显示正确"),
    _case("验证登录规则复述（同义反复坏例）", "返回结果与「支持账号密码登录」的需求定义一致"),
    _case("[FR-9.9] 验证不存在的编号（断链编号坏例）", "页面显示「账号已锁定」"),
    _case("【边界值】密码长度阈值 5 边界（缺两侧值坏例）", "输入 5 时被接受", method="边界值"),
]

# 仅警告坏例：无阻断项（契约发明 + 缺两侧值），exit 0
WARN_ONLY_CASES = [
    _case("验证服务端错误码（契约发明警告）", "接口返回 code=500 时提示「服务器错误」"),
    _case("【边界值】数量阈值 3 边界（缺两侧值警告）", "输入 3 件时被接受", method="边界值"),
]


@pytest.fixture
def bad_xlsx(tmp_path):
    """坏例集导出为禅道 Excel。"""
    cases = validate_cases(BAD_CASES)
    return export_zentao_excel(cases, tmp_path / "bad_cases.xlsx")


@pytest.fixture
def warn_xlsx(tmp_path):
    """仅警告坏例集导出为禅道 Excel。"""
    cases = validate_cases(WARN_ONLY_CASES)
    return export_zentao_excel(cases, tmp_path / "warn_cases.xlsx")


@pytest.fixture
def source_prd(tmp_path):
    """断链 / 未引用比对用 PRD 原文。"""
    p = tmp_path / "prd_source.md"
    p.write_text(SOURCE_PRD, encoding="utf-8")
    return p


def _no_llm(monkeypatch):
    """网关替身：任何 LLM 调用直接失败（断言默认零 LLM 调用）。"""
    def _boom(*args, **kwargs):
        raise AssertionError("doctor 默认必须零 LLM 调用")

    monkeypatch.setattr("casepilot.core.llm.call_chat_completions", _boom)


class TestBadCasesAllHit:
    """DoD 1：坏例集五类缺陷全命中且报告逐条点名。"""

    def test_all_five_defects_detected(self, bad_xlsx, source_prd):
        rows = load_cases(bad_xlsx)
        report = run_local_checks(
            rows,
            source_text=source_prd.read_text(encoding="utf-8"),
            source_frs=[],
            source_name=str(source_prd),
        )
        by_check = {(f.check, f.severity) for f in report.findings}
        assert (doctor_core.CHECK_FUZZY, SEVERITY_BLOCK) in by_check
        assert (doctor_core.CHECK_WEAK, SEVERITY_BLOCK) in by_check
        assert (doctor_core.CHECK_TAUTOLOGY, SEVERITY_BLOCK) in by_check
        assert (doctor_core.CHECK_BOUNDARY, SEVERITY_WARN) in by_check

    def test_fuzzy_word_hit_names_title_and_word(self, bad_xlsx, source_prd):
        rows = load_cases(bad_xlsx)
        report = run_local_checks(rows, source_text=source_prd.read_text(encoding="utf-8"))
        fuzzy = [f for f in report.findings if f.check == doctor_core.CHECK_FUZZY]
        assert len(fuzzy) == 1
        assert "模糊词坏例" in fuzzy[0].title
        assert "或" in fuzzy[0].hit
        assert fuzzy[0].suggestion

    def test_weak_assertion_hit(self, bad_xlsx, source_prd):
        rows = load_cases(bad_xlsx)
        report = run_local_checks(rows, source_text=source_prd.read_text(encoding="utf-8"))
        weak = [f for f in report.findings if f.check == doctor_core.CHECK_WEAK]
        assert len(weak) == 1
        assert "弱断言坏例" in weak[0].title
        assert "显示正确" in weak[0].hit

    def test_tautology_hit(self, bad_xlsx, source_prd):
        rows = load_cases(bad_xlsx)
        report = run_local_checks(rows, source_text=source_prd.read_text(encoding="utf-8"))
        taut = [f for f in report.findings if f.check == doctor_core.CHECK_TAUTOLOGY]
        assert len(taut) == 1
        assert "同义反复坏例" in taut[0].title
        assert "定义一致" in taut[0].hit

    def test_broken_fr_tag_hit(self, bad_xlsx, source_prd):
        """断链编号：[FR-9.9] 在 PRD 原文编号不存在 → 阻断项点名。"""
        rows = load_cases(bad_xlsx)
        report = run_local_checks(
            rows,
            source_text=source_prd.read_text(encoding="utf-8"),
            source_frs=_source_frs(source_prd),
            source_name=str(source_prd),
        )
        broken = [
            f for f in report.findings
            if f.check == doctor_core.CHECK_NUMBERING and f.severity == SEVERITY_BLOCK
        ]
        assert len(broken) == 1
        assert "断链" in broken[0].hit and "FR-9.9" in broken[0].hit
        assert "断链编号坏例" in broken[0].title

    def test_missing_adjacent_hit(self, bad_xlsx, source_prd):
        rows = load_cases(bad_xlsx)
        report = run_local_checks(rows, source_text=source_prd.read_text(encoding="utf-8"))
        boundary = [f for f in report.findings if f.check == doctor_core.CHECK_BOUNDARY]
        assert len(boundary) == 1
        assert "缺两侧值坏例" in boundary[0].title
        assert "缺少两侧相邻值" in boundary[0].hit
        assert "少 1" in boundary[0].hit and "多 1" in boundary[0].hit

    def test_report_sections_and_conclusion(self, bad_xlsx, source_prd):
        rows = load_cases(bad_xlsx)
        report = run_local_checks(rows, source_text=source_prd.read_text(encoding="utf-8"))
        text = report.format_report()
        assert "## 阻断项" in text and "## 警告" in text and "## 提示" in text
        assert "质检结论：不通过" in text
        for title in ("模糊词坏例", "弱断言坏例", "同义反复坏例", "缺两侧值坏例"):
            assert title in text, f"报告应点名用例标题：{title}"


def _source_frs(source_prd):
    """从 PRD 原文解析 FR 清单（doctor --source 同口径）。"""
    from casepilot.adapters import prd as prd_adapter

    return list(prd_adapter.parse_text(source_prd.read_text(encoding="utf-8"), source=str(source_prd)).frs)


class TestGoodCasesZeroFalseHit:
    """DoD 1：好例零误伤——6 样例 mock 产物过 doctor 零阻断零警告。"""

    @pytest.mark.parametrize("path", ALL_SAMPLES, ids=lambda p: p.stem)
    def test_mock_outputs_clean(self, path, tmp_path, offline_env):
        result = generate_from_file(path, mode="mock")
        xlsx = export_zentao_excel(result.cases, tmp_path / f"{path.stem}_cases.xlsx")
        rows = load_cases(xlsx)
        if path.suffix == ".md":
            source_text = path.read_text(encoding="utf-8")
            from casepilot.adapters import prd as prd_adapter

            frs = list(prd_adapter.parse_text(source_text, source=str(path)).frs)
            report = run_local_checks(rows, source_text=source_text, source_frs=frs, source_name=str(path))
            assert report.findings == [], f"{path.name} 好例应零发现：{[(f.check, f.hit) for f in report.findings]}"
        else:
            report = run_local_checks(rows)
            assert report.blockers == [] and report.warnings == [], [
                (f.check, f.hit) for f in report.findings
            ]

    @pytest.mark.parametrize("path", [LOGIN_PRD, NUMBERED_PRD], ids=lambda p: p.stem)
    def test_markdown_roundtrip_clean(self, path, tmp_path, offline_env):
        """Markdown 用例表装载后同样零误伤（xlsx / md 双格式）。"""
        result = generate_from_file(path, mode="mock")
        md = export_markdown(result.cases, tmp_path / f"{path.stem}_cases.md", source=str(path))
        rows = load_cases(md)
        source_text = path.read_text(encoding="utf-8")
        from casepilot.adapters import prd as prd_adapter

        frs = list(prd_adapter.parse_text(source_text, source=str(path)).frs)
        report = run_local_checks(rows, source_text=source_text, source_frs=frs, source_name=str(path))
        assert report.findings == [], [(f.check, f.hit) for f in report.findings]


class TestExitCodes:
    """DoD 1：有阻断 exit 1，仅警告 / 全过 exit 0。"""

    def test_blockers_exit_1(self, bad_xlsx, source_prd):
        result = runner.invoke(app, ["doctor", str(bad_xlsx), "--source", str(source_prd)])
        assert result.exit_code == 1, _combined(result)
        text = _combined(result)
        assert "阻断项" in text and "质检结论：不通过" in text

    def test_warnings_only_exit_0(self, warn_xlsx, source_prd):
        result = runner.invoke(app, ["doctor", str(warn_xlsx), "--source", str(source_prd)])
        assert result.exit_code == 0, _combined(result)
        text = _combined(result)
        assert "质检结论：通过" in text
        assert "契约发明警告" in text and "缺两侧值警告" in text

    def test_good_cases_exit_0(self, tmp_path, offline_env):
        result_cases = generate_from_file(LOGIN_PRD, mode="mock")
        xlsx = export_zentao_excel(result_cases.cases, tmp_path / "good.xlsx")
        result = runner.invoke(app, ["doctor", str(xlsx), "--source", str(LOGIN_PRD)])
        assert result.exit_code == 0, _combined(result)
        assert "质检结论：通过" in _combined(result)

    def test_unsupported_suffix_chinese_error(self, tmp_path):
        bad = tmp_path / "cases.csv"
        bad.write_text("x", encoding="utf-8")
        result = runner.invoke(app, ["doctor", str(bad)])
        assert result.exit_code == 1
        assert "暂不支持的用例文件格式" in _combined(result)

    def test_missing_file_chinese_error(self, tmp_path):
        result = runner.invoke(app, ["doctor", str(tmp_path / "不存在.xlsx")])
        assert result.exit_code == 1
        assert "用例文件不存在" in _combined(result)


class TestZeroLLMByDefault:
    """DoD 1：默认纯本地零 LLM 调用；--llm-review 无 key 优雅跳过。"""

    def test_default_zero_llm_calls(self, bad_xlsx, source_prd, monkeypatch):
        _no_llm(monkeypatch)
        result = runner.invoke(app, ["doctor", str(bad_xlsx), "--source", str(source_prd)])
        assert result.exit_code == 1, _combined(result)

    def test_llm_review_without_key_skips_gracefully(self, bad_xlsx, source_prd, monkeypatch, offline_env):
        _no_llm(monkeypatch)
        result = runner.invoke(
            app, ["doctor", str(bad_xlsx), "--source", str(source_prd), "--llm-review"]
        )
        text = _combined(result)
        assert "--llm-review 已跳过" in text
        assert "纯本地质检结论" in text
        assert result.exit_code == 1, text  # 本地结论不受影响

    def test_llm_review_no_suspicious_needs_no_llm(self, tmp_path, monkeypatch, offline_env):
        monkeypatch.setenv("CASEPILOT_LLM_API_KEY", "sk-test-key")
        _no_llm(monkeypatch)
        cases = validate_cases([_case("验证登录成功", "登录成功并跳转首页")])
        xlsx = export_zentao_excel(cases, tmp_path / "clean.xlsx")
        review = run_llm_review([])
        assert "无需 LLM 复核" in review[0]
        result = runner.invoke(app, ["doctor", str(xlsx), "--llm-review"])
        assert result.exit_code == 0, _combined(result)


class TestLLMReviewMergesConclusions:
    """--llm-review：有 key 时只复核本地可疑条目，结论合并进报告。"""

    def _fake_post(self, monkeypatch, captured):
        import json as _json

        def fake_call(base_url, api_key, model, system, user):
            captured.append(user)
            return _json.dumps(
                {"reviews": [{"n": 1, "verdict": "确认", "comment": "确属模糊表述，执行后无法唯一判定"}]},
                ensure_ascii=False,
            )

        monkeypatch.setattr("casepilot.core.llm.call_chat_completions", fake_call)
        monkeypatch.setenv("CASEPILOT_LLM_API_KEY", "sk-test-key")

    def test_conclusions_merged_into_report(self, bad_xlsx, source_prd, monkeypatch, offline_env):
        captured: list[str] = []
        self._fake_post(monkeypatch, captured)
        result = runner.invoke(
            app, ["doctor", str(bad_xlsx), "--source", str(source_prd), "--llm-review"]
        )
        text = _combined(result)
        assert "LLM 复核结论" in text
        assert "确认" in text
        # 只把可疑条目（阻断 + 警告）送复核：提示类（设计假设标注）不进 LLM
        assert "模糊词" in captured[0]
        assert "本地质检报告" not in captured[0]

    def test_review_failure_does_not_block_local_conclusion(self, bad_xlsx, source_prd, monkeypatch, offline_env):
        def fake_call(*args, **kwargs):
            raise RuntimeError("网络不可达")

        monkeypatch.setattr("casepilot.core.llm.call_chat_completions", fake_call)
        monkeypatch.setenv("CASEPILOT_LLM_API_KEY", "sk-test-key")
        result = runner.invoke(
            app, ["doctor", str(bad_xlsx), "--source", str(source_prd), "--llm-review"]
        )
        text = _combined(result)
        assert "LLM 复核失败" in text
        assert "本地质检结论不受影响" in text
        assert result.exit_code == 1, text

    def test_parse_review_json_fence_tolerant(self):
        reviews = doctor_core.parse_review_json('```json\n{"reviews": [{"n": 1, "verdict": "误报"}]}\n```')
        assert reviews[0]["verdict"] == "误报"
        with pytest.raises(ValueError) as exc:
            doctor_core.parse_review_json("不是 JSON")
        assert "不是合法 JSON" in str(exc.value)
        with pytest.raises(ValueError):
            doctor_core.parse_review_json('{"cases": []}')


class TestNumberingConsistency:
    """编号引用一致性：断链阻断、未引用警告、无 source 提示降级。"""

    def test_unreferenced_source_id_warned(self, source_prd):
        rows = [_case("[FR-1] 验证登录成功", "登录成功并跳转首页")]
        report = run_local_checks(
            rows,
            source_text=source_prd.read_text(encoding="utf-8"),
            source_frs=_source_frs(source_prd),
            source_name=str(source_prd),
        )
        warn = [f for f in report.findings if f.check == doctor_core.CHECK_NUMBERING and f.severity == SEVERITY_WARN]
        assert len(warn) == 1
        assert "FR-2" in warn[0].hit and "未被任何用例" in warn[0].hit

    def test_without_source_downgraded_to_info(self):
        rows = [_case("[FR-9.9] 验证登录", "登录成功")]
        report = run_local_checks(rows)
        assert report.blockers == []
        info = [f for f in report.findings if f.check == doctor_core.CHECK_NUMBERING]
        assert len(info) == 1 and info[0].severity == SEVERITY_INFO
        assert "未提供 --source" in info[0].hit

    def test_case_title_tags_extraction(self):
        assert case_title_tags("[FR-1.1] 验证登录") == ["FR-1.1"]
        assert case_title_tags("[NFR-1.2] 性能验证 [FR-auto-1]") == ["NFR-1.2", "FR-auto-1"]
        assert case_title_tags("【边界值】密码长度验证") == []
        assert case_title_tags("[设计假设] 某用例") == []


class TestBoundaryPairing:
    """边界成对性：v-1 / v+1 成对完整放行，缺失点名；时点 / 科学计数同口径。"""

    def test_pair_complete_passes(self):
        assert find_missing_adjacent("输入 5（恰好达到阈值）时被接受；输入 4（少 1）未达；输入 6（多 1）触发") is None

    def test_missing_both_sides(self):
        msg = find_missing_adjacent("输入 5 时被接受")
        assert msg is not None and "5" in msg and "少 1" in msg and "多 1" in msg

    def test_missing_one_side(self):
        msg = find_missing_adjacent("输入 5 时被接受；输入 4（少 1）被拒绝")
        assert msg is not None and "6" in msg and "多 1" in msg

    def test_timepoint_adjacent_minutes(self):
        assert find_missing_adjacent("06:59 切换未生效；07:00 切换生效；07:01 保持切换生效") is None

    def test_scientific_notation_pair(self):
        """1e+06 边界与两侧相邻值（999999 / 1000001）成对完整（回归锁 openapi 样例）。"""
        assert find_missing_adjacent("输入 1e+06 被接受；999999 被接受；1000001 被拒绝") is None

    def test_no_numeric_at_all(self):
        msg = find_missing_adjacent("输入边界值时被接受")
        assert msg is not None and "未出现任何数值" in msg


class TestLoadersAndAssumptionInfo:
    """装载器：xlsx / md 双格式与「,假设」标注提示。"""

    def test_markdown_loader_reads_fields(self, tmp_path):
        cases = validate_cases([_case("验证登录成功", "登录成功并跳转首页")])
        md = export_markdown(cases, tmp_path / "c.md", source="s")
        rows = load_cases(md)
        assert len(rows) == 1
        assert rows[0]["title"] == "验证登录成功"
        assert rows[0]["method"] == "等价类"
        assert rows[0]["assumption"] is False

    def test_assumption_marked_case_gives_info(self, tmp_path):
        case = validate_cases(
            [_case("验证限流降级（设计假设）", "触发限流降级，服务进入只读模式")]
        )[0].model_copy(update={"assumption": True})
        xlsx = export_zentao_excel([case], tmp_path / "assume.xlsx")
        rows = load_cases(xlsx)
        assert rows[0]["assumption"] is True
        report = run_local_checks(rows)
        info = [f for f in report.findings if f.check == doctor_core.CHECK_ASSUMPTION]
        assert len(info) == 1 and info[0].severity == SEVERITY_INFO
        assert ",假设" in info[0].hit or "假设" in info[0].hit

    def test_empty_xlsx_rejected(self, tmp_path):
        from openpyxl import Workbook

        from casepilot.exporters.zentao_excel import ZENTAO_HEADERS, SHEET_NAME

        wb = Workbook()
        ws = wb.active
        ws.title = SHEET_NAME
        ws.append(ZENTAO_HEADERS)
        path = tmp_path / "empty.xlsx"
        wb.save(path)
        with pytest.raises(ValueError) as exc:
            load_cases(path)
        assert "没有任何用例数据行" in str(exc.value)


class TestCheckReusesQualityWordlists:
    """词表唯一事实来源：doctor 复用 quality 检查器，不复制词表。"""

    def test_fuzzy_via_quality_checker(self):
        rows = [_case("用例一", "输入合法或非法均可提交")]
        report = run_local_checks(rows)
        hit = [f for f in report.findings if f.check == doctor_core.CHECK_FUZZY][0]
        assert "或" in hit.hit
        assert quality.find_fuzzy_words("输入合法或非法均可提交") == ["或"]

    def test_contract_invention_is_warning(self):
        rows = [_case("用例二", "接口返回 code=500 时提示「服务器错误」")]
        report = run_local_checks(rows)
        contract = [f for f in report.findings if f.check == doctor_core.CHECK_CONTRACT]
        assert len(contract) == 1 and contract[0].severity == SEVERITY_WARN
        assert report.blockers == []

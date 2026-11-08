"""Markdown 用例导出测试：8 要素表格、模块分组、方法论标注与统计表（UP-2）。"""

from casepilot.core.generator import generate_from_file
from casepilot.core.schema import TestCase
from casepilot.exporters.markdown_export import (
    ELEMENT_ROWS,
    build_markdown,
    export_markdown,
)
from tests.conftest import BOUNDARY_PRD, LOGIN_PRD, PETSTORE_OPENAPI

SAMPLES = [LOGIN_PRD, PETSTORE_OPENAPI, BOUNDARY_PRD]


def _case(**overrides) -> TestCase:
    base = {
        "title": "验证登录成功",
        "precondition": "用户已注册",
        "steps": "1. 输入账号密码\n2. 点击登录",
        "expected": "登录成功并跳转首页",
        "priority": "P1",
        "category": "功能",
        "method": "场景法",
        "module": "登录模块/场景验证",
    }
    base.update(overrides)
    return TestCase.model_validate(base)


class TestMarkdownStructure:
    def test_title_and_source(self):
        text = build_markdown([_case()], source="prd_login.md")
        assert text.startswith("# 用例集：prd_login.md")
        assert "共 1 条用例" in text

    def test_case_heading_and_elements_table(self):
        case = _case()
        text = build_markdown([case], source="a.md")
        assert "### 验证登录成功" in text
        for row in ELEMENT_ROWS:
            assert f"| {row} |" in text
        # 8 要素表格每条用例恰好 8 行数据（不含表头与分隔行）
        table_rows = [
            l for l in text.splitlines()
            if l.startswith("| ") and l.endswith(" |") and not l.startswith("| ---")
            and not l.startswith("| 要素 ")
            and not l.startswith("| 方法论 ")
        ]
        assert len(table_rows) == len(ELEMENT_ROWS) + 5  # 8 要素 + 统计表 5 行（4 方法 + 合计）

    def test_all_eight_elements_per_case(self):
        cases = [_case(title="第一条"), _case(title="第二条", method="边界值", priority="P2")]
        text = build_markdown(cases, source="a.md")
        sections = text.split("### ")[1:]  # 去掉文件头
        assert len(sections) == 2
        for section in sections:
            rows = [
                l for l in section.splitlines() if l.startswith("| ") and "| ---" not in l
            ]
            # 8 要素行 = 两列表格行且首列为要素名（统计表为三列，自动排除）
            element_rows = [
                l for l in rows
                if len(l.split("|")) == 4 and l.split("|")[1].strip() in ELEMENT_ROWS
            ]
            assert len(element_rows) == len(ELEMENT_ROWS) == 8

    def test_module_grouping(self):
        cases = [
            _case(title="A", module="登录/场景验证"),
            _case(title="B", module="登录/边界验证"),
            _case(title="C", module="登录/场景验证"),
        ]
        text = build_markdown(cases, source="a.md")
        assert "## 登录/场景验证" in text
        assert "## 登录/边界验证" in text
        # 同模块分组在一起（A、C 相邻）
        pos_a = text.index("### A")
        pos_c = text.index("### C")
        pos_b = text.index("### B")
        assert pos_a < pos_c < pos_b

    def test_methodology_statistics_table(self):
        cases = [_case(method="场景法"), _case(method="场景法"), _case(method="边界值")]
        text = build_markdown(cases, source="a.md")
        assert "## 方法论统计" in text
        assert "| 场景法 | 2 | 66.7% |" in text
        assert "| 边界值 | 1 | 33.3% |" in text
        assert "| 合计 | 3 | 100.0% |" in text

    def test_methodology_annotation_in_each_case(self):
        cases = [_case(method=m) for m in ("等价类", "边界值", "场景法", "判定表")]
        text = build_markdown(cases, source="a.md")
        for method in ("等价类", "边界值", "场景法", "判定表"):
            assert f"| 测试方法论 | {method} |" in text

    def test_escape_pipe_and_newline(self):
        case = _case(steps="1. 含竖线|的步骤\n2. 第二步", expected="无 | 脏数据")
        text = build_markdown([case], source="a.md")
        assert "含竖线\\|的步骤<br>2. 第二步" in text
        assert "无 \\| 脏数据" in text

    def test_export_writes_utf8_file(self, tmp_path):
        out = tmp_path / "nested" / "cases.md"
        saved = export_markdown([_case()], out, source="a.md")
        assert saved.exists()
        assert saved.read_text(encoding="utf-8").startswith("# 用例集：a.md")


class TestSamplesMarkdown:
    def test_three_samples_valid_markdown(self, tmp_path, offline_env):
        for sample in SAMPLES:
            result = generate_from_file(sample, mode="mock")
            out = tmp_path / f"{sample.stem}.md"
            export_markdown(result.cases, out, source=str(sample))
            text = out.read_text(encoding="utf-8")

            # 文件标题与用例总数
            assert text.startswith(f"# 用例集：{sample}")
            assert f"共 {len(result.cases)} 条用例" in text

            # 每条用例三级标题 + 8 要素
            assert text.count("### ") == len(result.cases)
            assert text.count("| 用例标题 |") == len(result.cases)
            assert text.count("| 前置条件 |") == len(result.cases)
            assert text.count("| 步骤 |") == len(result.cases)
            assert text.count("| 预期结果 |") == len(result.cases)
            assert text.count("| 优先级 |") == len(result.cases)
            assert text.count("| 用例类型 |") == len(result.cases)
            assert text.count("| 测试方法论 |") == len(result.cases)
            assert text.count("| 所属模块 |") == len(result.cases)

            # 模块分组：每个模块一个二级标题
            for case in result.cases:
                assert f"## {case.module}" in text

            # 尾部方法论统计表
            assert "## 方法论统计" in text
            assert "| 合计 |" in text

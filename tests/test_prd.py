"""中文 PRD 文本解析测试。"""

from casepilot.adapters import prd
from tests.conftest import BOUNDARY_PRD, LOGIN_PRD


class TestBoundaryExtraction:
    def test_range_boundary(self):
        result = prd._extract_boundaries("密码长度 8-20 位，必须包含字母和数字")
        kinds = {b.kind for b in result}
        assert "range_min" in kinds and "range_max" in kinds
        values = sorted(b.value for b in result)
        assert values == [8.0, 20.0]

    def test_keyword_boundary(self):
        result = prd._extract_boundaries("优惠券满 100 减 20，订单金额低于 100 元时不可用")
        values = [b.value for b in result]
        assert 100.0 in values and 20.0 in values

    def test_threshold_boundary(self):
        result = prd._extract_boundaries("连续错误 5 次锁定账号 30 分钟")
        values = {b.value for b in result}
        assert values == {5.0, 30.0}
        assert all(b.kind == "threshold" for b in result)

    def test_no_number_no_boundary(self):
        assert prd._extract_boundaries("这里没有任何数字") == []


class TestParseLoginPrd:
    def setup_method(self):
        self.profile = prd.parse_text(LOGIN_PRD.read_text(encoding="utf-8"), source=str(LOGIN_PRD))

    def test_module_from_heading(self):
        assert self.profile.module == "用户登录模块需求文档"

    def test_features_extracted(self):
        names = [f.name for f in self.profile.features]
        assert "支持账号密码登录" in names
        assert "支持短信验证码登录" in names
        assert len(names) >= 5

    def test_boundaries_extracted(self):
        values = {b.value for b in self.profile.boundaries}
        # 8-20 位密码、连续错误 5 次、锁定 30 分钟
        assert 8.0 in values and 20.0 in values
        assert 5.0 in values and 30.0 in values

    def test_flow_steps(self):
        assert len(self.profile.flows) == 5
        assert self.profile.flows[0] == "打开登录页面"

    def test_source_type(self):
        assert self.profile.source_type == "prd"


class TestParseBoundaryPrd:
    def setup_method(self):
        self.profile = prd.parse_text(BOUNDARY_PRD.read_text(encoding="utf-8"), source=str(BOUNDARY_PRD))

    def test_money_boundary(self):
        values = {b.value for b in self.profile.boundaries}
        # 满 100 减 20
        assert 100.0 in values and 20.0 in values

    def test_quantity_boundary(self):
        values = {b.value for b in self.profile.boundaries}
        # 单次最多 50 件
        assert 50.0 in values


class TestFallbacks:
    def test_bullet_items_without_heading(self):
        profile = prd.parse_text("- 支持导出报表\n- 支持导入数据", source="no_heading.txt")
        assert len(profile.features) == 2
        assert profile.module == "no_heading"

    def test_action_line_fallback(self):
        profile = prd.parse_text("用户可以重置密码。\n允许绑定第三方账号。", source="a.txt")
        names = [f.name for f in profile.features]
        assert "用户可以重置密码" in names
        assert "允许绑定第三方账号" in names

    def test_synthesized_flow_when_missing(self):
        profile = prd.parse_text("## 功能清单\n- 支持登录\n- 支持注册", source="b.md")
        assert profile.flows
        assert all("完成「" in step for step in profile.flows)

    def test_default_flow_when_nothing(self):
        profile = prd.parse_text("没有任何结构化内容", source="c.md")
        assert len(profile.flows) == 1
        assert "核心操作" in profile.flows[0]

    def test_flow_plain_lines(self):
        profile = prd.parse_text("## 主流程\n打开页面\n提交表单\n查看结果", source="d.md")
        assert profile.flows == ["打开页面", "提交表单", "查看结果"]

    def test_boundary_deduplication(self):
        profile = prd.parse_text("限制 5 件。\n再限制 5 件。", source="e.md")
        assert len([b for b in profile.boundaries if b.value == 5.0]) == 1

    def test_feature_deduplication(self):
        profile = prd.parse_text("## 功能清单\n- 支持登录\n- 支持登录", source="f.md")
        assert len(profile.features) == 1

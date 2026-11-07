"""方法论提示词模板测试。"""

from casepilot.core.methodology import (
    METHOD_TEMPLATES,
    PRIORITY_RULES,
    SCHEMA_REQUIREMENT,
    build_system_prompt,
    build_user_prompt,
)
from casepilot.core.schema import METHODS, Boundary, DocumentProfile, Feature


class TestMethodTemplates:
    def test_all_methods_covered(self):
        assert set(METHOD_TEMPLATES) == set(METHODS)

    def test_templates_are_chinese(self):
        for name, tpl in METHOD_TEMPLATES.items():
            assert any("一" <= ch <= "鿿" for ch in tpl), f"{name} 模板应为中文"

    def test_priority_rules_mention_p0(self):
        assert "P0" in PRIORITY_RULES

    def test_schema_requirement_lists_fields(self):
        for field in ("title", "precondition", "steps", "expected", "priority", "category", "method", "module"):
            assert field in SCHEMA_REQUIREMENT


class TestPromptBuilders:
    def _profile(self) -> DocumentProfile:
        return DocumentProfile(
            source="prd_login.md",
            source_type="prd",
            module="登录模块",
            features=[Feature(name="支持账号密码登录")],
            boundaries=[Boundary(text="密码 8-20 位", value=8, kind="range_min", unit="位")],
            flows=["打开登录页"],
        )

    def test_system_prompt_contains_methodology_and_schema(self):
        prompt = build_system_prompt()
        for name in METHODS:
            assert name in prompt
        assert "JSON 数组" in prompt
        assert "method" in prompt

    def test_user_prompt_contains_profile_summary(self):
        prompt = build_user_prompt(self._profile())
        assert "登录模块" in prompt
        assert "支持账号密码登录" in prompt
        assert "JSON 数组" in prompt

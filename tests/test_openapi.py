"""OpenAPI 文档解析测试。"""

import json

import pytest
import yaml

from casepilot.adapters import openapi
from tests.conftest import PETSTORE_OPENAPI


class TestParsePetstore:
    def setup_method(self):
        self.profile = openapi.parse_file(PETSTORE_OPENAPI)

    def test_module_from_info_title(self):
        assert self.profile.module == "Petstore 宠物商店接口"

    def test_features_are_api_kind(self):
        assert len(self.profile.features) == 6
        assert all(f.kind == "api" for f in self.profile.features)
        names = [f.name for f in self.profile.features]
        assert any("POST /pet" in n for n in names)
        assert any("GET /pet/{petId}" in n for n in names)
        assert any("POST /store/order" in n for n in names)
        assert any("POST /user" in n for n in names)

    def test_boundaries_from_schema_constraints(self):
        values = {b.value for b in self.profile.boundaries}
        # quantity minimum 1 / maximum 50，password minLength 8 / maxLength 20
        assert 1.0 in values and 50.0 in values
        assert 8.0 in values and 20.0 in values

    def test_flows_synthesized_from_operations(self):
        assert len(self.profile.flows) == 3
        assert all(step.startswith("调用 ") for step in self.profile.flows)

    def test_source_type(self):
        assert self.profile.source_type == "openapi"


class TestParseVariants:
    def test_json_format(self, tmp_path):
        data = yaml.safe_load(PETSTORE_OPENAPI.read_text(encoding="utf-8"))
        json_path = tmp_path / "petstore.json"
        json_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        profile = openapi.parse_file(json_path)
        assert len(profile.features) == 6

    def test_invalid_top_level_raises(self, tmp_path):
        bad = tmp_path / "bad.yaml"
        bad.write_text("- 不是对象\n", encoding="utf-8")
        with pytest.raises(ValueError) as exc:
            openapi.parse_file(bad)
        assert "结构非法" in str(exc.value)

    def test_module_fallback_without_info(self):
        profile = openapi.parse_dict({"paths": {}})
        assert profile.module == "OpenAPI 接口"
        assert len(profile.flows) == 1

    def test_empty_paths(self):
        profile = openapi.parse_dict({"info": {"title": "X"}, "paths": {}})
        assert profile.features == []
        assert profile.module == "X"


class TestSchemaScan:
    def test_nested_properties_scanned(self):
        schema = {
            "type": "object",
            "properties": {
                "user": {
                    "type": "object",
                    "properties": {"age": {"type": "integer", "minimum": 18, "maximum": 99}},
                }
            },
        }
        boundaries = []
        openapi._scan_schema_constraints(schema, "root", boundaries, 0, set())
        values = {b.value for b in boundaries}
        assert values == {18.0, 99.0}

    def test_self_reference_terminates(self):
        node = {"type": "object"}
        node["items"] = node  # 自引用，验证递归保护
        boundaries = []
        openapi._scan_schema_constraints(node, "root", boundaries, 0, set())
        assert boundaries == []

    def test_bool_not_treated_as_number(self):
        boundaries = []
        openapi._scan_schema_constraints({"minimum": True}, "root", boundaries, 0, set())
        assert boundaries == []

    def test_composition_keywords_scanned(self):
        schema = {"allOf": [{"minimum": 3}], "oneOf": [{"maximum": 7}]}
        boundaries = []
        openapi._scan_schema_constraints(schema, "root", boundaries, 0, set())
        assert {b.value for b in boundaries} == {3.0, 7.0}

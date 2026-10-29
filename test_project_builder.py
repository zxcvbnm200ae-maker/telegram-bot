import json
import pytest

from project_builder import parse_model_json, safe_relative_path


def test_safe_relative_path_accepts_normal_path():
    assert safe_relative_path("src/main.py") == "src/main.py"


@pytest.mark.parametrize("path", ["../secret", "/etc/passwd", "src/../../x", "C:/x", "a\\b"])
def test_safe_relative_path_rejects_escape(path):
    with pytest.raises(ValueError):
        safe_relative_path(path)


def test_parse_model_json_validates_and_normalizes():
    data = parse_model_json(json.dumps({
        "project_name": "Hello World!",
        "summary": "Demo",
        "files": [{"path": "main.py", "content": "print('ok')"}],
        "test_plan": ["compile"],
    }))
    assert data["project_name"] == "Hello-World"
    assert data["files"][0]["path"] == "main.py"


def test_parse_model_json_rejects_path_traversal():
    raw = {"project_name": "bad", "files": [{"path": "../bad.py", "content": "x"}]}
    with pytest.raises(ValueError):
        parse_model_json(json.dumps(raw))

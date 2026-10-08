"""测试 parse_json_response —— 项目最核心的纯函数之一。"""

import pytest

from deep_research.utils import parse_json_response


def test_parse_bare_json():
    """直接 JSON 字符串应正常解析。"""
    result = parse_json_response('{"key": "value", "num": 42}')
    assert result == {"key": "value", "num": 42}


def test_parse_markdown_codeblock():
    """markdown ```json ... ``` 包裹的 JSON 应正常解析。"""
    text = """这是一些说明文字
```json
{"name": "test", "items": [1, 2, 3]}
```
后面的内容"""
    result = parse_json_response(text)
    assert result == {"name": "test", "items": [1, 2, 3]}


def test_parse_plain_codeblock():
    """无语言标记的 ``` ... ``` 包裹的 JSON 应正常解析。"""
    text = '```\n{"x": 1}\n```'
    result = parse_json_response(text)
    assert result == {"x": 1}


def test_parse_embedded_json():
    """混在文本中的 {...} 应被提取出来。"""
    text = '答案是：{"status": "ok", "data": [1, 2]}，请检查。'
    result = parse_json_response(text)
    assert result == {"status": "ok", "data": [1, 2]}


def test_parse_invalid_raises():
    """无效输入应抛出 ValueError。"""
    with pytest.raises(ValueError, match="Failed to parse JSON"):
        parse_json_response("这不是 JSON，也没有任何花括号")


def test_parse_empty_object():
    """空对象 {} 应正常解析。"""
    result = parse_json_response("{}")
    assert result == {}


def test_parse_nested_json():
    """嵌套 JSON 应正确保留层级结构。"""
    text = '{"outer": {"inner": {"deep": true}}}'
    result = parse_json_response(text)
    assert result == {"outer": {"inner": {"deep": True}}}

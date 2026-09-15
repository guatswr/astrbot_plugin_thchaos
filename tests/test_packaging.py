"""打包一致性：插件能否被当成包导入、配置项与 schema 是否对得上。"""

from __future__ import annotations

import ast
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_thchaos.main import ThChaosPlugin  # noqa: E402


def test_plugin_package_imports_with_relative_logic_module():
    # main.py 用的是 `from .logic import ...`：没有 __init__.py 时这条导入在
    # AstrBot 的加载器下会直接失败，插件根本起不来。
    assert (ROOT / "__init__.py").exists()
    assert ThChaosPlugin.__name__ == "ThChaosPlugin"


def test_conf_schema_is_valid_json():
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    assert isinstance(schema, dict) and schema
    for key, spec in schema.items():
        assert "type" in spec, f"{key} 缺少 type"
        assert "description" in spec, f"{key} 缺少 description"
        assert "default" in spec, f"{key} 缺少 default"


# AstrBot 的 astrbot/core/config/default.py 里 DEFAULT_VALUE_MAP 的键。
# 少了任何一个都会在插件加载时抛 TypeError，整个插件起不来：
#   TypeError: 不受支持的配置类型 boolean。支持的类型有：dict_keys([...])
# 写 "boolean" 而不是 "bool" 是最容易犯的错——JSON Schema 本来就叫 boolean，
# 但 AstrBot 只认 bool。
ASTRBOT_SCHEMA_TYPES = {
    "int",
    "float",
    "bool",
    "string",
    "text",
    "list",
    "file",
    "object",
    "template_list",
    "dict",
}


def test_conf_schema_types_are_supported_by_astrbot():
    """每个配置项的 type 都必须是 AstrBot 认识的那几种。

    AstrBot 的 _parse_schema 只校验顶层 type（"object" 的 items 才会递归），
    所以这里也只对顶层做断言，不需要递归进 items。
    """
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    bad = {
        key: spec["type"]
        for key, spec in schema.items()
        if spec.get("type") not in ASTRBOT_SCHEMA_TYPES
    }
    assert not bad, f"这些配置项用了 AstrBot 不认识的类型，会导致插件加载失败：{bad}"


def test_lifecycle_hook_is_initialize_not_on_astrbot_loaded():
    """建连必须放在 initialize()，不能用 filter.on_astrbot_loaded()。

    on_astrbot_loaded 只在 AstrBot **进程启动**时触发一次（core_lifecycle.start()），
    面板里重载插件、保存插件配置都只走 plugin_manager.reload()，不会再触发它。
    用了那个钩子的话，重载之后插件照常收群消息、却从不连接后端，而且连启动
    日志都不打——表现就是「插件明明在跑，群里什么都没发生」。

    这里按装饰器而不是全文匹配：文档字符串里提到这个名字是应该的，
    真正要禁的是把它当成钩子用。
    """

    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    decorated = [
        ast.unparse(decorator)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        for decorator in node.decorator_list
    ]
    assert not [d for d in decorated if "on_astrbot_loaded" in d], decorated

    methods = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
        for node in node.body
        if isinstance(node, ast.AsyncFunctionDef)
    }
    assert {"initialize", "terminate"} <= methods


def test_every_config_key_read_by_code_exists_in_the_schema():
    """两边必须一一对应。

    代码读了但 schema 里没有 → 用户在 WebUI 里根本看不到这个开关；
    schema 里有但代码不读 → 配置界面上的死选项。两种都很难自己发现。
    """

    schema_keys = set(json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8")))
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    code_keys = set(re.findall(r'_config\.get\(\s*"([a-zA-Z_]+)"', source))
    assert code_keys == schema_keys, f"仅代码有：{code_keys - schema_keys}；仅 schema 有：{schema_keys - code_keys}"

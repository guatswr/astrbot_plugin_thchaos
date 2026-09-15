"""打包一致性：插件能否被当成包导入、配置项与 schema 是否对得上。"""

from __future__ import annotations

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


def test_every_config_key_read_by_code_exists_in_the_schema():
    """两边必须一一对应。

    代码读了但 schema 里没有 → 用户在 WebUI 里根本看不到这个开关；
    schema 里有但代码不读 → 配置界面上的死选项。两种都很难自己发现。
    """

    schema_keys = set(json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8")))
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    code_keys = set(re.findall(r'_config\.get\(\s*"([a-zA-Z_]+)"', source))
    assert code_keys == schema_keys, f"仅代码有：{code_keys - schema_keys}；仅 schema 有：{schema_keys - code_keys}"

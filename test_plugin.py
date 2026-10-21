"""真实 SDK + 模拟能力接口回归测试，不向 QQ 发消息。"""
import ast
import base64
import copy
import hashlib
import importlib.util
import json
import os
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
ROOT = Path(__file__).resolve().parent
WORKSPACE = next((p for p in ROOT.parents if (p / "MaiBotData").is_dir()), None)
SITE = None
if WORKSPACE is not None:
    os.environ.setdefault("MAIBOT_INSTANCE_DIR", str(WORKSPACE / "MaiBotData/core"))
    SITE = next((WORKSPACE / "MaiBotData/core/.onekey/environments").glob("*/Lib/site-packages"), None)
    if SITE is not None:
        sys.path.insert(0, str(SITE))
spec = importlib.util.spec_from_file_location("guard_target", ROOT / "plugin.py")
mod = importlib.util.module_from_spec(spec);sys.modules[spec.name] = mod;spec.loader.exec_module(mod)
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="

class ParsingTests(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(mod.extract_labels("好[表情包: 得意,开心]"), ("好", ["得意", "开心"]))
        self.assertEqual(mod.extract_labels("好[表情包：开心][表情包: 得意]"), ("好", ["开心", "得意"]))
    def test_literals(self):
        for text in ["`[表情包: 开心]`", "```text\n[表情包: 开心]\n```", "“[表情包: 开心]”", "这是格式[表情包: 开心]", "为什么发送了[表情包: 开心]", "（骄傲）", "[表情包: 开心]是什么意思", "> [表情包: 开心]", "价格（含税）"]:
            with self.subTest(text=text):self.assertEqual(mod.extract_labels(text), (text, []))
    def test_invalid(self):
        for text in ["[表情包:]", "[表情包: 开心。多余说明]", "[表情包: a,b,c,d,e,f,g,h,i]"]:
            self.assertEqual(mod.extract_labels(text), (text, []))
    def test_manifest(self):
        if SITE is None:
            self.skipTest("宿主集成检查需要本地 MaiBot 实例")
        from src.plugin_runtime.runner.manifest_validator import PluginManifest
        result = PluginManifest.model_validate(json.loads((ROOT / "_manifest.json").read_text(encoding="utf-8")))
        self.assertEqual(result.id, "local.emoji-label-guard")
    def test_host_binary_decoder(self):
        if SITE is None:
            self.skipTest("宿主集成检查需要本地 MaiBot 实例")
        path = SITE / "src/plugin_runtime/host/message_utils.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "PluginMessageUtils")
        func = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "_build_binary_component")
        func.decorator_list = []
        ns = {"Any": object, "Dict": dict, "StandardMessageComponents": object, "base64": base64, "hashlib": hashlib}
        exec(compile(ast.Module(body=[func], type_ignores=[]), str(path), "exec"), ns)
        c = ns["_build_binary_component"](SimpleNamespace, mod.emoji_component(PNG))
        self.assertEqual(c.binary_data, base64.b64decode(PNG));self.assertTrue(c.binary_hash)

class HookTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.p = mod.create_plugin();self.p._plugin_config_instance = mod.Settings()
        self.api = SimpleNamespace(get_emotions=AsyncMock(return_value=["开心", "得意"]), get_by_description=AsyncMock(return_value={"base64": PNG, "description": "得意,开心"}))
        self.p._ctx = SimpleNamespace(emoji=self.api)
    def message(self, text, stream="s", mid="m"):
        return {"message_id": mid, "timestamp": 100.5, "session_id": stream, "raw_message": [{"type": "text", "data": text}], "processed_plain_text": text}
    async def prepare(self, text, stream="s"):
        r = await self.p.prepare(response=text, session_id=stream, output_items=[])
        return r.get("modified_kwargs", {}).get("response", text)
    async def test_split_and_attach(self):
        text = await self.prepare("画好了[表情包: 得意,开心]");self.assertEqual(text, "画好了")
        msg = self.message(text);await self.p.bind(message=msg, stream_id="s")
        msg["raw_message"][0]["data"] = "画好"
        r = await self.p.attach(message=msg, stream_id="s", processed_plain_text="画好")
        raw = r["modified_kwargs"]["message"]["raw_message"]
        self.assertEqual([c["type"] for c in raw], ["text", "emoji"])
        self.assertEqual(raw[-1]["binary_data_base64"], PNG)
        self.assertNotIn("modified_kwargs", await self.p.attach(message=msg, stream_id="s"))
    async def test_pure_label(self):
        text = await self.prepare("[表情包: 得意]")
        r = await self.p.bind(message=self.message(text), stream_id="s")
        self.assertEqual(r["modified_kwargs"]["message"]["raw_message"][0]["type"], "emoji")
        self.assertEqual(r["modified_kwargs"]["processed_plain_text"], "")
    async def test_no_match(self):
        self.api.get_emotions.return_value = []
        self.assertEqual(await self.prepare("好了[表情包: 得意]"), "好了")
        text = await self.prepare("[表情包: 得意]")
        self.assertEqual((await self.p.bind(message=self.message(text), stream_id="s"))["action"], "abort")
        self.api.get_by_description.assert_not_awaited()
    async def test_failure(self):
        self.api.get_emotions.side_effect = RuntimeError("offline")
        self.assertEqual(await self.prepare("好了[表情包: 开心]"), "好了")
    async def test_nested_payload(self):
        self.api.get_emotions.return_value = {"emotions": ["开心"]}
        self.api.get_by_description.return_value = {"success": True, "emoji": {"base64": PNG, "description": "开心"}}
        await self.prepare("好[表情包: 开心]");self.assertTrue(self.p._prepared[0].image)
    async def test_wrong_image(self):
        self.api.get_by_description.return_value = {"base64": PNG, "description": "生气"}
        await self.prepare("好[表情包: 开心]");self.assertFalse(self.p._prepared[0].image)
    async def test_corrupt_image(self):
        self.api.get_by_description.return_value = {"base64": "not an image", "description": "开心"}
        await self.prepare("好[表情包: 开心]");self.assertFalse(self.p._prepared[0].image)
    async def test_session_isolation(self):
        text = await self.prepare("好[表情包: 开心]")
        self.assertNotIn("modified_kwargs", await self.p.bind(message=self.message(text, "other"), stream_id="other"))
        self.assertEqual(len(self.p._prepared), 1)
    async def test_existing_media(self):
        text = await self.prepare("好[表情包: 开心]");msg = self.message(text)
        msg["raw_message"].append({"type": "image", "data": "existing"});before = copy.deepcopy(msg)
        await self.p.bind(message=msg, stream_id="s")
        self.assertNotIn("modified_kwargs", await self.p.attach(message=msg, stream_id="s"));self.assertEqual(msg, before)
    async def test_disabled(self):
        self.p._plugin_config_instance.plugin.enabled = False
        self.assertNotIn("modified_kwargs", await self.p.prepare(response="[表情包: 开心]", session_id="s"))
        self.api.get_emotions.assert_not_awaited()
    async def test_command_untouched(self):
        text = await self.prepare("[表情包: 开心]");msg = self.message(text);msg["is_command"] = True
        self.assertNotIn("modified_kwargs", await self.p.bind(message=msg, stream_id="s"))
    async def test_duplicate_response_queue(self):
        text = await self.prepare("好[表情包: 开心]");await self.prepare("好[表情包: 开心]")
        for mid in ["one", "two"]:
            msg = self.message(text, mid=mid);await self.p.bind(message=msg, stream_id="s")
            r = await self.p.attach(message=msg, stream_id="s")
            self.assertEqual(len(r["modified_kwargs"]["message"]["raw_message"]), 2)
        self.assertFalse(self.p._prepared);self.assertFalse(self.p._bound)
    async def test_expiry_unload(self):
        await self.prepare("好[表情包: 开心]");self.p._prepared[0].created = time.monotonic() - 181
        self.p._prune();self.assertFalse(self.p._prepared)
        await self.prepare("好[表情包: 开心]");await self.p.on_unload();self.assertFalse(self.p._prepared)
    async def test_lifecycle(self):
        await self.p.on_load()
        await self.p.on_config_update("self", {}, "1.0.0")
        await self.p.on_unload()

    def test_sdk_registration(self):
        from maibot_sdk import MaiBotPlugin
        for method in ["on_load", "on_unload", "on_config_update"]:
            self.assertIsNot(getattr(type(self.p), method), getattr(MaiBotPlugin, method))
        hooks = self.p.get_components()
        self.assertEqual({h["name"]:h["metadata"]["order"] for h in hooks}, {"guard_prepare":"early", "guard_bind":"early", "guard_attach":"late"})

if __name__ == "__main__":unittest.main(verbosity=2)

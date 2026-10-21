"""独立插件：把模型泄漏的句末内部表情标签转成实际图片组件。"""
from __future__ import annotations
import asyncio
import base64
import copy
import logging
import re
import time
from dataclasses import dataclass
from maibot_sdk import HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import HookOrder
from pydantic import Field

logger = logging.getLogger("emoji_label_guard")
LABEL = re.compile(r"\[表情包\s*[:：]\s*([^\[\]\n]{1,100})\]")
PROTECTED = re.compile(r"```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]*`|[“‘\"'][^\n]*?[”’\"']")
EXPLANATION = re.compile(r"标签|占位|格式|示例|字符串|字面|代码|日志|为什么|怎么|如何|不要输出|发送了")


def extract_labels(text: str) -> tuple[str, list[str]]:
    """只识别句末内部标签，保留引用、代码和格式讨论。"""
    protected = [m.span() for m in PROTECTED.finditer(text)]
    spans, tags = [], []
    tail = len(text.rstrip())
    for match in reversed(list(LABEL.finditer(text))):
        if text[match.end():tail].strip():
            continue
        if any(start <= match.start() < end for start, end in protected):
            break
        prefix = text[text.rfind("\n", 0, match.start()) + 1:match.start()]
        if prefix.lstrip().startswith(">") or EXPLANATION.search(prefix):
            break
        values = [v.strip() for v in re.split(r"[,，、]", match[1]) if v.strip()]
        if not values or len(values) > 8 or any(not re.fullmatch(r"[\w -]{1,20}", v) for v in values):
            break
        spans.append(match.span())
        tags[0:0] = values
        tail = match.start()
    for start, end in spans:
        text = text[:start] + text[end:]
    return text.rstrip(), list(dict.fromkeys(tags))


def text_key(text: str) -> str:
    return re.sub(r"\s+", "", text)


def message_key(message: dict, stream: str) -> tuple[str, str, str]:
    return stream, str(message.get("message_id", "")), str(message.get("timestamp", ""))


def emoji_component(image: str) -> dict:
    # SDK Hook 载荷使用 binary_data_base64；data 是显示描述，不是图片字节。
    return {"type": "emoji", "data": "", "binary_data_base64": image}


class PluginSettings(PluginConfigBase):
    enabled: bool = Field(default=True, description="启用内部表情标签转换")
    config_version: str = "1.0.0"


class Settings(PluginConfigBase):
    plugin: PluginSettings = Field(default_factory=PluginSettings)


@dataclass
class Prepared:
    stream: str
    text: str
    image: str
    created: float


class EmojiLabelGuard(MaiBotPlugin):
    config_model = Settings

    def __init__(self):
        super().__init__()
        self._prepared: list[Prepared] = []
        self._bound: dict[tuple[str, str, str], Prepared] = {}

    def _prune(self):
        cutoff = time.monotonic() - 180
        self._prepared = [p for p in self._prepared if p.created > cutoff][-128:]
        self._bound = {k: v for k, v in self._bound.items() if v.created > cutoff}

    async def on_load(self):
        logger.info("表情标签修复已加载，使用现有表情库，不增加 LLM 请求")

    async def on_unload(self):
        self._prepared.clear()
        self._bound.clear()

    async def on_config_update(self, scope: str, config_data: dict, version: str):
        if scope == "self":
            self._prepared.clear()
            self._bound.clear()

    async def _lookup(self, tags: list[str]) -> str:
        try:
            async with asyncio.timeout(4):
                available = await self.ctx.emoji.get_emotions()
                if isinstance(available, dict):
                    available = available.get("emotions", [])
                if not isinstance(available, list):
                    return ""
                matched = [tag for tag in tags if tag in available]
                if not matched:
                    logger.info("未匹配到已注册的情绪标签，将只保留正文")
                    return ""
                result = await self.ctx.emoji.get_by_description(",".join(matched))
                if not isinstance(result, dict) or result.get("success") is False:
                    return ""
                result = result.get("emoji", result)
                if not isinstance(result, dict):
                    return ""
                desc = str(result.get("description") or result.get("emotion") or "")
                if not set(re.split(r"[,，、\s]+", desc)).intersection(matched):
                    logger.warning("宿主返回的图片没有目标情绪，已跳过")
                    return ""
                image = str(result.get("base64") or "")
                if image:
                    base64.b64decode(image, validate=True)
                return image
        except Exception as exc:
            logger.warning("表情查询失败，内部标签不会作为文字发送: %s", exc)
            return ""

    @HookHandler("maisaka.replyer.after_response", name="guard_prepare",
                 order=HookOrder.EARLY, timeout_ms=6000,
                 description="在智能分段前移除句末内部表情标签并查询真实图片")
    async def prepare(self, response: str = "", session_id: str = "", **kwargs):
        if not self.config.plugin.enabled or not session_id:
            return {"action": "continue"}
        cleaned, tags = extract_labels(response)
        if not tags:
            return {"action": "continue"}
        self._prune()
        image = await self._lookup(tags)
        # 纯标签保留到发送 Hook，避免空正文被宿主判为生成失败。
        target = cleaned or response
        self._prepared.append(Prepared(session_id, text_key(target), image, time.monotonic()))
        logger.info("已处理内部标签，标签数=%d，匹配图片=%s", len(tags), bool(image))
        return {"action": "continue", "modified_kwargs": {**kwargs, "response": target, "session_id": session_id}}

    @HookHandler("send_service.after_build_message", name="guard_bind",
                 order=HookOrder.EARLY,
                 description="在分段改写前绑定回复，纯标签转为图片或中止空消息")
    async def bind(self, message: dict | None = None, stream_id: str = "",
                   processed_plain_text: str = "", **kwargs):
        if not self.config.plugin.enabled or not isinstance(message, dict) or message.get("is_command"):
            return {"action": "continue"}
        self._prune()
        stream = stream_id or str(message.get("session_id") or "")
        raw = message.get("raw_message", [])
        if not isinstance(raw, list):
            return {"action": "continue"}
        text = "".join(str(c.get("data", "")) for c in raw if isinstance(c, dict) and c.get("type") == "text") or processed_plain_text
        prepared = next((p for p in self._prepared if p.stream == stream and p.text == text_key(text)), None)
        if prepared is None:
            return {"action": "continue"}
        self._prepared.remove(prepared)
        cleaned, tags = extract_labels(text)
        if tags and not cleaned:
            other = [c for c in raw if c.get("type") != "text"]
            if prepared.image and not any(c.get("type") in {"emoji", "image"} for c in other):
                other.append(emoji_component(prepared.image))
            if not other:
                logger.info("纯标签无匹配图片，已中止空消息")
                return {"action": "abort"}
            updated = copy.deepcopy(message)
            updated.update(raw_message=other, processed_plain_text="",
                           is_emoji=len(other) == 1 and other[0].get("type") == "emoji",
                           is_picture=any(c.get("type") in {"emoji", "image"} for c in other))
            return {"action": "continue", "modified_kwargs": {**kwargs, "message": updated, "stream_id": stream, "processed_plain_text": ""}}
        if prepared.image and (message.get("message_id") or message.get("timestamp")):
            self._bound[message_key(message, stream)] = prepared
        return {"action": "continue"}

    @HookHandler("send_service.after_build_message", name="guard_attach",
                 order=HookOrder.LATE,
                 description="分段后将真实表情组件附在首段文字之后，由宿主投递")
    async def attach(self, message: dict | None = None, stream_id: str = "", **kwargs):
        if not self.config.plugin.enabled or not isinstance(message, dict):
            return {"action": "continue"}
        stream = stream_id or str(message.get("session_id") or "")
        prepared = self._bound.pop(message_key(message, stream), None)
        if prepared is None:
            return {"action": "continue"}
        updated = copy.deepcopy(message)
        raw = updated.get("raw_message")
        if not isinstance(raw, list) or any(c.get("type") in {"emoji", "image"} for c in raw):
            return {"action": "continue"}
        raw.append(emoji_component(prepared.image))
        logger.info("已附加真实表情组件，交由宿主发送")
        return {"action": "continue", "modified_kwargs": {**kwargs, "message": updated, "stream_id": stream}}


def create_plugin():
    return EmojiLabelGuard()

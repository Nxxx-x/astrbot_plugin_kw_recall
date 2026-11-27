"""消息撤回 · 面向 QQ 官方机器人适配器（qq_official / qq_official_webhook）。

支持四类命中：关键词、5 位及以上连续数字、链接、二维码图片，每类可单独开关并
配置各自的撤回提示。群消息命中后调用官方 DELETE 接口撤回。

官机撤回的三个前提，写死在 README 里，代码这边只做兜底：
1. 只能撤回 2 分钟内发送的消息，超时返回 40064004；
2. 机器人必须是群管理员才能撤回群成员的消息，否则 40062003；
3. 群主需在群设置里放开「接收全量消息」，否则机器人只看得到 @ 它的消息。

配置统一存在 KV（data 目录）里，Web 界面和群指令改的是同一份数据。
"""

from __future__ import annotations

import asyncio
import os
import re
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.api.web import error_response, json_response, request

try:  # 官机适配器依赖 botpy，理论上必装；缺失时插件仍可加载
    from botpy.http import Route
except Exception:  # pragma: no cover
    Route = None  # type: ignore[assignment]

PLUGIN_NAME = "astrbot_plugin_kw_recall"
STATE_KEY = "state"

QQ_OFFICIAL_TYPES = (
    filter.PlatformAdapterType.QQOFFICIAL
    | filter.PlatformAdapterType.QQOFFICIAL_WEBHOOK
)

MAX_KEYWORDS = 200
MAX_KEYWORD_LEN = 50
MAX_REPLY_LEN = 200

# 5 位及以上连续数字。中间被空格、标点断开的（如 "123 45"）不算，
# 因为 \d{5,} 要求这几位数字彼此紧邻。
NUMBER_RE = re.compile(r"\d{5,}")

# 链接：带协议头的、www 开头的、常见域名后缀的，以及裸 IP。
# 域名部分用负向断言避开 "3.14"、"main.py" 这类不含常见后缀的内容。
LINK_RE = re.compile(
    r"(?:https?://|www\.)\S+"
    r"|(?<![\w.])(?:[\w-]+\.)+"
    r"(?:com|cn|net|org|top|xyz|vip|cc|io|me|tv|info|shop|site|club|link|app|"
    r"dev|fun|online|store|tech|pro|live|biz|mobi|ws|tk|ml|ga|gq|co|so|pw|"
    r"lu|in|us|uk|de|jp|kr|ru)(?![\w])"
    r"|(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?(?![\w.])",
    re.IGNORECASE,
)

TYPE_KEYS = ("keyword", "number", "link", "qrcode", "emoji")
TYPE_LABELS = {
    "keyword": "关键词",
    "number": "号码",
    "link": "链接",
    "qrcode": "二维码",
    "emoji": "Emoji",
}
TYPE_COMMANDS = {
    "keyword": "撤回关键词",
    "number": "撤回号码",
    "link": "撤回链接",
    "qrcode": "撤回二维码",
    "emoji": "撤回表情",
}
DEFAULT_REPLIES = {
    "keyword": "消息含有关键词，已撤回",
    "number": "请勿发送号码，已撤回",
    "link": "请勿发送链接，已撤回",
    "qrcode": "请勿发送二维码，已撤回",
    "emoji": "请勿刷屏表情，已撤回",
}

# QQ 官机的原生表情（含超级表情、收藏的表情包）不以 Unicode 字符下发，
# 适配器会把 <faceType=4,...> 转成 [表情:描述] 或 [表情]。这类属于 QQ 自带
# 表情，不纳入 emoji 撤回；匹配前统一剥掉，免得标签里的描述文字干扰关键词。
FACE_TAG_RE = re.compile(r"\[表情(?::[^\]]*)?\]")

# 官机的 @ 同样以标签形式留在文本里（适配器只会提前剥掉 @ 机器人自己的标记）：
#   <qqbot-at-user id="openid" />   <@!openid>   <@openid>
# openid 常带一长串数字，不剥掉会被号码规则误判成号码；里面的字母也会误命中关键词。
AT_TAG_RE = re.compile(r"<qqbot-at-user\b[^>]*>|<@!?[^>]*>", re.IGNORECASE)


def _strip_markup(text: str) -> str:
    """剥掉官机的 @ 与表情标签，只留用户真正打的文字。"""
    return AT_TAG_RE.sub("", FACE_TAG_RE.sub("", text))

# 标准库 re 没有 Unicode 属性，只能按区段兜底；装了 regex 就用字素簇 +
# Extended_Pictographic，能正确把 👨‍👩‍👧、👍🏽、🇨🇳 各算成一个。
try:  # pragma: no cover - 取决于环境
    import regex as _regex

    _CLUSTER_RE: Any = _regex.compile(r"\X")
    _PICTO_RE: Any = _regex.compile(
        r"\p{Extended_Pictographic}|\p{Regional_Indicator}|\u20E3"
    )
except Exception:  # pragma: no cover
    _CLUSTER_RE = None
    _PICTO_RE = None

FALLBACK_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"
    "\U0001F1E6-\U0001F1FF"
    "\u2600-\u27BF"
    "\u2B00-\u2BFF"
    "\u2194-\u2199\u21A9-\u21AA"
    "\u2300-\u23FF"
    "\u25AA-\u25AB\u25B6\u25C0\u25FB-\u25FE"
    "\u00A9\u00AE\u203C\u2049\u2122\u2139\u24C2"
    "\u3030\u303D\u3297\u3299\u20E3"
    "]"
)

# 1️⃣ #️⃣ 这类键帽表情在降级模式下要整段匹配，否则会漏掉或把数字当正文
KEYCAP_RE = re.compile(r"[0-9#*]\ufe0f?\u20e3")


def _count_emoji(text: str) -> int:
    """统计 Unicode emoji 字符个数，如 😀 ❤️ 👍。

    QQ 原生表情 / 超级表情（适配器转成的 [表情:捂脸]、[表情]）不算 emoji，
    这类是 QQ 自带表情，不属于本规则；统计前先把这些标签剥掉，避免标签里的
    描述文字（如「满头问号」）被当成消息正文。@ 标签同理。
    """
    rest = _strip_markup(text)

    if _CLUSTER_RE is not None and _PICTO_RE is not None:
        return sum(
            1 for item in _CLUSTER_RE.finditer(rest) if _PICTO_RE.search(item.group())
        )

    keycaps = len(KEYCAP_RE.findall(rest))
    return len(FALLBACK_EMOJI_RE.findall(KEYCAP_RE.sub("", rest))) + keycaps

# 单条消息最多解析几张图，避免一条九宫格把事件循环拖住
MAX_IMAGES_PER_MESSAGE = 4
MAX_IMAGE_BYTES = 10 * 1024 * 1024


def _default_types() -> dict[str, dict[str, Any]]:
    return {
        key: {"enabled": key == "keyword", "reply": DEFAULT_REPLIES[key]}
        for key in TYPE_KEYS
    }


def _default_state() -> dict[str, Any]:
    return {
        "enabled": True,
        "notify": False,
        "notify_at": False,
        "keywords": [],
        "types": _default_types(),
        "count": 0,
        "last_error": "",
    }


def _clean_reply(value: Any) -> str:
    return str(value or "").strip()[:MAX_REPLY_LEN]


class KeywordRecall(Star):
    """群消息命中关键词/号码/链接/二维码就撤回。"""

    def __init__(self, context: Context):
        super().__init__(context)
        self._lock = asyncio.Lock()
        self._cache: dict[str, Any] | None = None
        self._qr_lock = asyncio.Lock()
        self._qr_detector = None
        self._qr_missing_warned = False

        # 插件可视化视图的后端接口，视图端通过 bridge.apiGet / apiPost 调用
        context.register_web_api(
            f"/{PLUGIN_NAME}/state", self.api_get_state, ["GET"], "读取撤回配置"
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/state/save",
            self.api_save_state,
            ["POST"],
            "保存撤回配置",
        )

    # ------------------------------------------------------------------
    # 状态读写
    # ------------------------------------------------------------------

    async def _load(self) -> dict[str, Any]:
        return self._normalize(await self.get_kv_data(STATE_KEY, None))

    @staticmethod
    def _normalize(raw: Any) -> dict[str, Any]:
        state = _default_state()
        if not isinstance(raw, dict):
            return state

        for flag in ("enabled", "notify", "notify_at"):
            if flag in raw:
                state[flag] = bool(raw[flag])

        types_raw = raw.get("types")
        if isinstance(types_raw, dict):
            for key in TYPE_KEYS:
                item = types_raw.get(key)
                if not isinstance(item, dict):
                    continue
                if "enabled" in item:
                    state["types"][key]["enabled"] = bool(item["enabled"])
                if "reply" in item:
                    state["types"][key]["reply"] = _clean_reply(item["reply"])
        else:
            # 旧版本只有 recall_numbers / notify_text，升级时平滑迁移
            for key, flat in (
                ("number", "recall_numbers"),
                ("link", "recall_links"),
                ("qrcode", "recall_qrcodes"),
            ):
                if flat in raw:
                    state["types"][key]["enabled"] = bool(raw[flat])
            if raw.get("notify_text"):
                state["types"]["keyword"]["reply"] = _clean_reply(raw["notify_text"])

        keywords = raw.get("keywords")
        if not isinstance(keywords, list):
            keywords = []
        state["keywords"] = [
            str(k).strip()[:MAX_KEYWORD_LEN] for k in keywords if str(k).strip()
        ][:MAX_KEYWORDS]

        try:
            state["count"] = int(raw.get("count") or 0)
        except (TypeError, ValueError):
            state["count"] = 0

        state["last_error"] = str(raw.get("last_error") or "")[:200]
        return state

    async def _save(self, state: dict[str, Any]) -> None:
        await self.put_kv_data(STATE_KEY, state)

    async def _state(self) -> dict[str, Any]:
        """高频路径走内存缓存，避免每条群消息都读一次 KV。"""
        if self._cache is None:
            self._cache = await self._load()
        return self._cache

    # ------------------------------------------------------------------
    # 匹配
    # ------------------------------------------------------------------

    def _match_text(
        self, text: str, state: dict[str, Any]
    ) -> tuple[str, str] | None:
        """按 关键词 → 链接 → 号码 → emoji 的顺序匹配，返回 (类型, 命中说明)。"""
        types = state["types"]

        # QQ 原生表情是 [表情:描述] 标签、@ 是 <qqbot-at-user id="…"/> 标签，
        # 两者都不是用户打的字：标签里的描述文字和 openid 不能当正文参与匹配，
        # 否则「[表情:笑哭]」会被「笑」误命中、@ 别人的 openid 会被当成号码。
        text = _strip_markup(text)

        if types["keyword"]["enabled"]:
            lowered = text.lower()
            for kw in state["keywords"]:
                if kw.lower() in lowered:
                    return "keyword", f"关键词「{kw}」"

        if types["link"]["enabled"]:
            found = LINK_RE.search(text)
            if found:
                return "link", f"链接 {found.group(0)[:40]}"

        if types["number"]["enabled"]:
            found = NUMBER_RE.search(text)
            if found:
                return "number", f"号码 {found.group(0)}"

        if types["emoji"]["enabled"]:
            count = _count_emoji(text)
            if count:
                return "emoji", f"{count} 个 emoji"

        return None

    async def _match_qrcode(self, event: AstrMessageEvent) -> tuple[str, str] | None:
        """下载消息里的图片并解码，识别到二维码就命中。"""
        getter = getattr(event, "get_messages", None)
        if not callable(getter):
            return None
        try:
            components = getter() or []
        except Exception:
            return None

        images = [c for c in components if isinstance(c, Comp.Image)]
        for image in images[:MAX_IMAGES_PER_MESSAGE]:
            path = await self._image_path(image)
            if not path:
                continue
            content = await self._decode_qr(path)
            if content:
                return "qrcode", f"二维码（{content[:30]}）"
        return None

    async def _image_path(self, image: Any) -> str:
        convert = getattr(image, "convert_to_file_path", None)
        if not callable(convert):
            return ""
        try:
            path = await asyncio.wait_for(convert(), timeout=8)
        except Exception as exc:
            logger.debug(f"[kw_recall] 图片下载失败：{exc}")
            return ""

        try:
            if not path or not os.path.isfile(path):
                return ""
            if os.path.getsize(path) > MAX_IMAGE_BYTES:
                logger.debug("[kw_recall] 图片过大，跳过二维码识别")
                return ""
        except OSError:
            return ""
        return str(path)

    def _get_qr_detector(self):
        if self._qr_detector is not None:
            return self._qr_detector
        try:
            import cv2  # type: ignore
        except Exception:
            if not self._qr_missing_warned:
                self._qr_missing_warned = True
                logger.warning(
                    "[kw_recall] 未安装 opencv-python-headless，二维码撤回不可用；"
                    "请 pip install opencv-python-headless 后重启"
                )
            return None
        self._qr_detector = cv2.QRCodeDetector()
        return self._qr_detector

    @staticmethod
    def _qr_candidates(image, cv2):
        """原图、灰度、缩放各试一遍，兼顾大图和过小的二维码。"""
        yield image
        try:
            yield cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        except Exception:
            pass

        height, width = image.shape[:2]
        long_side = max(height, width)
        if long_side > 1280:
            scale = 1280 / long_side
            small = cv2.resize(
                image,
                (max(1, int(width * scale)), max(1, int(height * scale))),
                interpolation=cv2.INTER_AREA,
            )
            yield small
            yield cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        elif long_side < 600:
            big = cv2.resize(
                image,
                (width * 2, height * 2),
                interpolation=cv2.INTER_CUBIC,
            )
            yield big

    async def _decode_qr(self, path: str) -> str:
        detector = self._get_qr_detector()
        if detector is None:
            return ""
        # 解码是 CPU 密集操作，放线程池；detector 复用，加锁避免并发调用
        async with self._qr_lock:
            return await asyncio.to_thread(self._decode_qr_sync, detector, path)

    @staticmethod
    def _decode_qr_sync(detector, path: str) -> str:
        try:
            import cv2  # type: ignore
            import numpy as np  # type: ignore
        except Exception:
            return ""
        try:
            buffer = np.fromfile(path, dtype=np.uint8)
            if buffer.size == 0:
                return ""
            image = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
        except Exception as exc:
            logger.debug(f"[kw_recall] 图片读取失败：{exc}")
            return ""
        if image is None:
            return ""

        for candidate in KeywordRecall._qr_candidates(image, cv2):
            try:
                ok, texts, _points, _ = detector.detectAndDecodeMulti(candidate)
            except Exception:
                try:
                    text, _points, _ = detector.detectAndDecode(candidate)
                except Exception:
                    continue
                if text:
                    return text
                continue
            if ok and texts:
                for item in texts:
                    if item:
                        return item
        return ""

    # ------------------------------------------------------------------
    # 官机相关
    # ------------------------------------------------------------------

    @staticmethod
    def _is_group_admin(event: AstrMessageEvent) -> bool:
        """群主 / 管理员发的消息不撤，避免得罪人和无权限报错。"""
        raw = getattr(event.message_obj, "raw_message", None)
        author = getattr(raw, "author", None)
        return getattr(author, "member_role", "member") in ("owner", "admin")

    async def _recall_group_message(
        self, event: AstrMessageEvent, message_id: str
    ) -> None:
        """调用官机撤回接口。AstrBot 没有封装群消息撤回，只能自己发 DELETE。"""
        if Route is None:
            raise RuntimeError("未找到 botpy，当前环境无法撤回")
        bot = getattr(event, "bot", None)
        if bot is None:
            raise RuntimeError("当前适配器不支持撤回")

        route = Route(
            "DELETE",
            "/v2/groups/{group_openid}/messages/{message_id}",
            group_openid=event.get_group_id(),
            message_id=message_id,
        )
        await bot.api._http.request(route)

    # ------------------------------------------------------------------
    # 消息监听
    # ------------------------------------------------------------------

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.platform_adapter_type(QQ_OFFICIAL_TYPES)
    async def on_group_message(self, event: AstrMessageEvent):
        state = await self._state()
        if not state["enabled"]:
            return

        types = state["types"]
        active = [key for key in TYPE_KEYS if types[key]["enabled"]]
        if not active:
            return
        if active == ["keyword"] and not state["keywords"]:
            # 只剩关键词但词表是空的，没什么可匹配
            return

        text = event.message_str or ""
        hit = self._match_text(text, state) if text.strip() else None
        if hit is None and types["qrcode"]["enabled"]:
            hit = await self._match_qrcode(event)
        if hit is None:
            return

        hit_type, reason = hit

        if self._is_group_admin(event):
            return

        message_id = getattr(event.message_obj, "message_id", "")
        if not message_id:
            return

        try:
            await self._recall_group_message(event, message_id)
        except Exception as exc:  # 撤回失败不影响后续消息处理
            logger.warning(f"[kw_recall] 撤回失败（{reason}）：{exc}")
            async with self._lock:
                current = await self._load()
                current["last_error"] = str(exc)[:200]
                await self._save(current)
                self._cache = current
            return

        logger.info(f"[kw_recall] 已撤回消息，原因：{reason}")

        async with self._lock:
            current = await self._load()
            current["count"] = int(current.get("count") or 0) + 1
            current["last_error"] = ""
            await self._save(current)
            self._cache = current

        reply_text = state["types"][hit_type]["reply"]
        if state.get("notify") and reply_text:
            if state.get("notify_at"):
                # 官机会把 At 转成 <qqbot-at-user id="openid" /> 内联进文本，
                # openid 正好就是发送者 ID。
                yield event.chain_result(
                    [Comp.At(qq=event.get_sender_id()), Comp.Plain(f" {reply_text}")]
                )
            else:
                yield event.plain_result(reply_text)

    # ------------------------------------------------------------------
    # 群指令（手机上临时改配置用）
    # ------------------------------------------------------------------

    @staticmethod
    def _arg(event: AstrMessageEvent, index: int = 1) -> str:
        parts = (event.message_str or "").strip().split(maxsplit=index)
        return parts[index].strip() if len(parts) > index else ""

    def _status_lines(self, state: dict[str, Any]) -> list[str]:
        types = state["types"]
        kws = state["keywords"]
        lines = [
            f"总开关：{'开启' if state['enabled'] else '关闭'}",
            f"关键词：{'开启' if types['keyword']['enabled'] else '关闭'}"
            f"（{len(kws)} 个）",
            f"号码：{'开启' if types['number']['enabled'] else '关闭'}"
            "（5 位及以上连续数字）",
            f"链接：{'开启' if types['link']['enabled'] else '关闭'}",
            f"二维码：{'开启' if types['qrcode']['enabled'] else '关闭'}",
            f"Emoji：{'开启（有就撤）' if types['emoji']['enabled'] else '关闭'}",
            f"撤回后提示：{'开启' if state['notify'] else '关闭'}"
            f"{'（@ 被撤回的人）' if state['notify_at'] else ''}",
            f"累计撤回 {state['count']} 条",
        ]
        return lines

    @filter.command("撤回词", alias={"kw"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def manage(self, event: AstrMessageEvent):
        """管理关键词：/撤回词 查看 | /撤回词 +词 | /撤回词 -词"""
        state = await self._state()
        arg = self._arg(event)

        if not arg:
            kws = state["keywords"]
            body = "、".join(kws) if kws else "（空）"
            reply = "\n".join(
                self._status_lines(state)
                + [f"词表：{body}", "用法：/撤回词 +新词、/撤回词 -旧词"]
            )
            yield event.plain_result(reply)
            return

        reply = ""
        async with self._lock:
            current = await self._load()
            kws = current["keywords"]

            if arg.startswith("+"):
                kw = arg[1:].strip()[:MAX_KEYWORD_LEN]
                if not kw:
                    reply = "关键词不能为空"
                elif kw in kws:
                    reply = f"「{kw}」已经在了"
                elif len(kws) >= MAX_KEYWORDS:
                    reply = f"最多只能有 {MAX_KEYWORDS} 个关键词"
                else:
                    kws.append(kw)
                    await self._save(current)
                    self._cache = current
                    reply = f"已添加「{kw}」，当前 {len(kws)} 个"
            elif arg.startswith("-"):
                kw = arg[1:].strip()
                if kw not in kws:
                    reply = f"没有「{kw}」"
                else:
                    kws.remove(kw)
                    await self._save(current)
                    self._cache = current
                    reply = f"已删除「{kw}」，当前 {len(kws)} 个"
            else:
                reply = "用法：/撤回词 +新词 或 /撤回词 -旧词"

        yield event.plain_result(reply)

    async def _toggle(self, event: AstrMessageEvent, key: str, tip: str):
        arg = self._arg(event).lower()
        label = TYPE_LABELS[key]

        reply = ""
        async with self._lock:
            state = await self._load()
            if arg in ("开", "开启", "on", "1", "true"):
                state["types"][key]["enabled"] = True
                await self._save(state)
                self._cache = state
                reply = f"{label}撤回已开启{tip}"
            elif arg in ("关", "关闭", "off", "0", "false"):
                state["types"][key]["enabled"] = False
                await self._save(state)
                self._cache = state
                reply = f"{label}撤回已关闭"
            elif arg:
                reply = f"用法：/{TYPE_COMMANDS[key]} 开 或 关"
            else:
                current = "开启" if state["types"][key]["enabled"] else "关闭"
                reply = (
                    f"{label}撤回当前{current}。用法：/{TYPE_COMMANDS[key]} 开|关"
                )

        yield event.plain_result(reply)

    @filter.command("撤回号码", alias={"kwnum"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def toggle_numbers(self, event: AstrMessageEvent):
        """开关号码撤回：/撤回号码 开 | /撤回号码 关"""
        async for result in self._toggle(
            event, "number", "：消息含 5 位及以上连续数字会被撤回"
        ):
            yield result

    @filter.command("撤回链接", alias={"kwnlink"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def toggle_links(self, event: AstrMessageEvent):
        """开关链接撤回：/撤回链接 开 | /撤回链接 关"""
        async for result in self._toggle(event, "link", "：消息含网址会被撤回"):
            yield result

    @filter.command("撤回二维码", alias={"kwnqr"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def toggle_qrcodes(self, event: AstrMessageEvent):
        """开关二维码撤回：/撤回二维码 开 | /撤回二维码 关"""
        async for result in self._toggle(event, "qrcode", "：消息含二维码图片会被撤回"):
            yield result

    @filter.command("撤回表情", alias={"kwnemoji"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def toggle_emoji(self, event: AstrMessageEvent):
        """开关表情撤回：/撤回表情 开 | /撤回表情 关"""
        async for result in self._toggle(event, "emoji", "：消息含 emoji 字符就会被撤回"):
            yield result

    @filter.command("撤回关键词", alias={"kwnkw"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def toggle_keywords(self, event: AstrMessageEvent):
        """开关关键词撤回：/撤回关键词 开 | /撤回关键词 关"""
        async for result in self._toggle(event, "keyword", ""):
            yield result

    @filter.command("撤回回复", alias={"kwnreply"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def set_reply(self, event: AstrMessageEvent):
        """设置各类撤回提示：/撤回回复 链接 文案（文案填 - 表示清空）"""
        arg = self._arg(event)
        if not arg:
            state = await self._state()
            lines = [
                f"{TYPE_LABELS[key]}：{state['types'][key]['reply'] or '（不发提示）'}"
                for key in TYPE_KEYS
            ]
            yield event.plain_result(
                "\n".join(
                    [f"撤回后提示：{'开启' if state['notify'] else '关闭'}"]
                    + lines
                    + ["用法：/撤回回复 链接 请勿发链接"]
                )
            )
            return

        parts = arg.split(maxsplit=1)
        name = parts[0]
        content = parts[1].strip() if len(parts) > 1 else ""

        key = next(
            (
                k
                for k in TYPE_KEYS
                if name in (k, TYPE_LABELS[k]) or (k == "emoji" and name == "表情")
            ),
            None,
        )
        if key is None:
            yield event.plain_result(
                "类型只能是：关键词、号码、链接、二维码、Emoji。例：/撤回回复 链接 请勿发链接"
            )
            return
        if not content:
            yield event.plain_result(
                f"用法：/撤回回复 {TYPE_LABELS[key]} 文案，文案填 - 表示不发送提示"
            )
            return

        text = "" if content == "-" else _clean_reply(content)
        async with self._lock:
            state = await self._load()
            state["types"][key]["reply"] = text
            await self._save(state)
            self._cache = state

        if text:
            yield event.plain_result(f"{TYPE_LABELS[key]}撤回提示已改为：{text}")
        else:
            yield event.plain_result(f"{TYPE_LABELS[key]}撤回后不再发送提示")

    # ------------------------------------------------------------------
    # Web API
    # ------------------------------------------------------------------

    async def api_get_state(self):
        return json_response(await self._state())

    async def api_save_state(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象", status_code=400)

        for flag in ("enabled", "notify", "notify_at", "recall_numbers"):
            if flag in payload and not isinstance(payload[flag], bool):
                return error_response(f"{flag} 必须是布尔值", status_code=400)

        if "keywords" in payload and not isinstance(payload["keywords"], list):
            return error_response("keywords 必须是数组", status_code=400)

        types_payload = payload.get("types")
        if types_payload is not None and not isinstance(types_payload, dict):
            return error_response("types 必须是对象", status_code=400)
        if isinstance(types_payload, dict):
            for key, item in types_payload.items():
                if key not in TYPE_KEYS:
                    return error_response(f"未知的撤回类型：{key}", status_code=400)
                if not isinstance(item, dict):
                    return error_response(f"types.{key} 必须是对象", status_code=400)
                if "enabled" in item and not isinstance(item["enabled"], bool):
                    return error_response(
                        f"types.{key}.enabled 必须是布尔值", status_code=400
                    )
                if "reply" in item and not isinstance(item["reply"], str):
                    return error_response(
                        f"types.{key}.reply 必须是字符串", status_code=400
                    )

        async with self._lock:
            state = await self._load()

            if "enabled" in payload:
                state["enabled"] = payload["enabled"]
            if "notify" in payload:
                state["notify"] = payload["notify"]
            if "notify_at" in payload:
                state["notify_at"] = payload["notify_at"]

            if isinstance(types_payload, dict):
                for key, item in types_payload.items():
                    if "enabled" in item:
                        state["types"][key]["enabled"] = item["enabled"]
                    if "reply" in item:
                        state["types"][key]["reply"] = _clean_reply(item["reply"])
            elif "recall_numbers" in payload:
                state["types"]["number"]["enabled"] = payload["recall_numbers"]
            if "notify_text" in payload:
                state["types"]["keyword"]["reply"] = _clean_reply(payload["notify_text"])

            if "keywords" in payload:
                cleaned: list[str] = []
                seen: set[str] = set()
                for item in payload["keywords"]:
                    kw = str(item).strip()[:MAX_KEYWORD_LEN]
                    if not kw or kw in seen:
                        continue
                    seen.add(kw)
                    cleaned.append(kw)
                    if len(cleaned) >= MAX_KEYWORDS:
                        break
                state["keywords"] = cleaned

            await self._save(state)
            self._cache = state

        return json_response(state)

    async def terminate(self):
        logger.info("[kw_recall] 插件已卸载")

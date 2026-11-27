"""消息撤回 · 面向 QQ 官方机器人适配器（qq_official / qq_official_webhook）。

支持五类命中：关键词、5 位及以上连续数字、链接、二维码图片、Emoji，每类可单独
开关并配置各自的撤回提示。群消息命中后调用官方 DELETE 接口撤回。

所有配置项定义在 _conf_schema.json，在 AstrBot 管理面板 → 插件 → 本插件 → 配置
里修改，保存后即时生效（插件读取的是同一份 config 对象）。

官机撤回的三个前提，写死在 README 里，代码这边只做兜底：
1. 只能撤回 2 分钟内发送的消息，超时返回 40064004；
2. 机器人必须是群管理员才能撤回群成员的消息，否则 40062003；
3. 群主需在群设置里放开「接收全量消息」，否则机器人只看得到 @ 它的消息。
"""

from __future__ import annotations

import asyncio
import os
import re
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

try:  # 官机适配器依赖 botpy，理论上必装；缺失时插件仍可加载
    from botpy.http import Route
except Exception:  # pragma: no cover
    Route = None  # type: ignore[assignment]

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
# 只有关键词默认开启，其余四类默认关闭，避免装完就误伤
DEFAULT_ENABLED = {
    "keyword": True,
    "number": False,
    "link": False,
    "qrcode": False,
    "emoji": False,
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

# 单条消息最多解析几张图，避免一条九宫格把事件循环拖住
MAX_IMAGES_PER_MESSAGE = 4
MAX_IMAGE_BYTES = 10 * 1024 * 1024


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


def _parse_keywords(raw: Any) -> list[str]:
    """把配置里的多行文本解析成词表：去空行、去重、限长限量。"""
    if isinstance(raw, (list, tuple)):
        lines = [str(item) for item in raw]
    else:
        lines = str(raw or "").splitlines()

    words: list[str] = []
    for line in lines:
        word = line.strip()[:MAX_KEYWORD_LEN]
        if word and word not in words:
            words.append(word)
        if len(words) >= MAX_KEYWORDS:
            break
    return words


class MessageRecall(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        # AstrBotConfig 继承自 dict；缺失时退化为空字典，靠代码里的默认值兜底
        self.config = config if isinstance(config, dict) else {}
        self._count = 0
        self._last_error = ""
        self._qr_detector = None
        self._qr_missing_warned = False
        self._qr_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # 配置读取
    # ------------------------------------------------------------------

    def _group(self, key: str) -> dict[str, Any]:
        raw = self.config.get(key)
        return raw if isinstance(raw, dict) else {}

    def _enabled(self) -> bool:
        return bool(self.config.get("enabled", True))

    def _notify(self) -> bool:
        return bool(self.config.get("notify", False))

    def _notify_at(self) -> bool:
        return bool(self.config.get("notify_at", False))

    def _on(self, key: str) -> bool:
        raw = self._group(key).get("enabled", DEFAULT_ENABLED[key])
        return bool(raw)

    def _reply(self, key: str) -> str:
        raw = self._group(key).get("reply", DEFAULT_REPLIES[key])
        if raw is None:
            raw = DEFAULT_REPLIES[key]
        return str(raw).strip()[:MAX_REPLY_LEN]

    def _keywords(self) -> list[str]:
        return _parse_keywords(self._group("keyword").get("list", ""))

    def _active_types(self) -> list[str]:
        return [key for key in TYPE_KEYS if self._on(key)]

    async def _persist(self) -> bool:
        """把改动写回 data/config/<plugin>_config.json。"""
        try:
            saver = getattr(self.config, "save_config_async", None)
            if callable(saver):
                await saver()
            else:
                self.config.save_config()  # type: ignore[attr-defined]
            return True
        except Exception as exc:
            logger.warning(f"[kw_recall] 配置保存失败：{exc}")
            return False

    def _set_group(self, key: str, field: str, value: Any) -> None:
        group = self.config.get(key)
        if not isinstance(group, dict):
            group = {}
            self.config[key] = group
        group[field] = value

    # ------------------------------------------------------------------
    # 匹配
    # ------------------------------------------------------------------

    def _match_text(self, text: str) -> tuple[str, str] | None:
        """按 关键词 → 链接 → 号码 → emoji 的顺序匹配，返回 (类型, 命中说明)。"""
        # QQ 原生表情是 [表情:描述] 标签、@ 是 <qqbot-at-user id="…"/> 标签，
        # 两者都不是用户打的字：标签里的描述文字和 openid 不能当正文参与匹配，
        # 否则「[表情:笑哭]」会被「笑」误命中、@ 别人的 openid 会被当成号码。
        text = _strip_markup(text)

        if self._on("keyword"):
            lowered = text.lower()
            for kw in self._keywords():
                if kw.lower() in lowered:
                    return "keyword", f"关键词「{kw}」"

        if self._on("link"):
            found = LINK_RE.search(text)
            if found:
                return "link", f"链接 {found.group(0)[:40]}"

        if self._on("number"):
            found = NUMBER_RE.search(text)
            if found:
                return "number", f"号码 {found.group(0)}"

        if self._on("emoji"):
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

        for candidate in MessageRecall._qr_candidates(image, cv2):
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
        if not self._enabled():
            return

        active = self._active_types()
        if not active:
            return
        if active == ["keyword"] and not self._keywords():
            # 只剩关键词但词表是空的，没什么可匹配
            return

        text = event.message_str or ""
        hit = self._match_text(text) if text.strip() else None
        if hit is None and self._on("qrcode"):
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
            self._last_error = str(exc)[:200]
            return

        logger.info(f"[kw_recall] 已撤回消息，原因：{reason}")
        self._count += 1
        self._last_error = ""

        reply_text = self._reply(hit_type)
        if self._notify() and reply_text:
            if self._notify_at():
                # 官机会把 At 转成 <qqbot-at-user id="openid" /> 内联进文本，
                # openid 正好就是发送者 ID。
                yield event.chain_result(
                    [Comp.At(qq=event.get_sender_id()), Comp.Plain(f" {reply_text}")]
                )
            else:
                yield event.plain_result(reply_text)

    # ------------------------------------------------------------------
    # 群指令（手机上临时改配置用，改动会写回插件配置文件）
    # ------------------------------------------------------------------

    @staticmethod
    def _arg(event: AstrMessageEvent, index: int = 1) -> str:
        parts = (event.message_str or "").strip().split(maxsplit=index)
        return parts[index].strip() if len(parts) > index else ""

    def _status_lines(self) -> list[str]:
        kws = self._keywords()
        lines = [
            f"总开关：{'开启' if self._enabled() else '关闭'}",
            f"关键词：{'开启' if self._on('keyword') else '关闭'}（{len(kws)} 个）",
            f"号码：{'开启' if self._on('number') else '关闭'}（5 位及以上连续数字）",
            f"链接：{'开启' if self._on('link') else '关闭'}",
            f"二维码：{'开启' if self._on('qrcode') else '关闭'}",
            f"Emoji：{'开启（有就撤）' if self._on('emoji') else '关闭'}",
            f"撤回后提示：{'开启' if self._notify() else '关闭'}"
            f"{'（@ 被撤回的人）' if self._notify_at() else ''}",
            f"本次运行累计撤回 {self._count} 条",
        ]
        if self._last_error:
            lines.append(f"最近一次失败：{self._last_error}")
        return lines

    @filter.command("撤回词", alias={"kw"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def manage(self, event: AstrMessageEvent):
        """管理关键词：/撤回词 查看 | /撤回词 +词 | /撤回词 -词"""
        arg = self._arg(event)

        if not arg:
            kws = self._keywords()
            body = "、".join(kws) if kws else "（空）"
            yield event.plain_result(
                "\n".join(
                    self._status_lines()
                    + [
                        f"词表：{body}",
                        "用法：/撤回词 +新词、/撤回词 -旧词",
                    ]
                )
            )
            return

        action = arg[0]
        word = arg[1:].strip()[:MAX_KEYWORD_LEN]
        if action not in ("+", "-") or not word:
            yield event.plain_result(
                "用法：/撤回词 +新词 或 /撤回词 -旧词"
            )
            return

        words = self._keywords()
        if action == "+":
            if word in words:
                yield event.plain_result(f"「{word}」已经在词表里了")
                return
            if len(words) >= MAX_KEYWORDS:
                yield event.plain_result(f"词表已满（{MAX_KEYWORDS} 个），先删几个")
                return
            words.append(word)
            message = f"已添加「{word}」，当前 {len(words)} 个"
        else:
            if word not in words:
                yield event.plain_result(f"词表里没有「{word}」")
                return
            words.remove(word)
            message = f"已删除「{word}」，当前 {len(words)} 个"

        self._set_group("keyword", "list", "\n".join(words))
        await self._persist()
        yield event.plain_result(message)

    async def _toggle(self, event: AstrMessageEvent, key: str, tip: str):
        arg = self._arg(event).lower()
        label = TYPE_LABELS[key]
        current = self._on(key)

        if arg in ("开", "开启", "on", "1", "true", "启用"):
            target = True
        elif arg in ("关", "关闭", "off", "0", "false", "停用"):
            target = False
        elif not arg:
            yield event.plain_result(
                f"{label}撤回当前{'开启' if current else '关闭'}。"
                f"用法：/{TYPE_COMMANDS[key]} 开|关"
            )
            return
        else:
            yield event.plain_result(f"用法：/{TYPE_COMMANDS[key]} 开|关")
            return

        if target == current:
            yield event.plain_result(
                f"{label}撤回{'已经是开启' if target else '已经是关闭'}状态"
            )
            return

        self._set_group(key, "enabled", target)
        ok = await self._persist()
        suffix = "" if ok else "（配置写回失败，重启后可能丢失）"
        yield event.plain_result(
            f"{label}撤回已{'开启' if target else '关闭'}{tip}{suffix}"
        )

    @filter.command("撤回关键词", alias={"kwnkw"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def toggle_keywords(self, event: AstrMessageEvent):
        """开关关键词撤回：/撤回关键词 开 | /撤回关键词 关"""
        async for result in self._toggle(event, "keyword", "：消息包含词表内容会被撤回"):
            yield result

    @filter.command("撤回号码", alias={"kwnnum"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def toggle_numbers(self, event: AstrMessageEvent):
        """开关号码撤回：/撤回号码 开 | /撤回号码 关"""
        async for result in self._toggle(
            event, "number", "：5 位及以上连续数字会被撤回"
        ):
            yield result

    @filter.command("撤回链接", alias={"kwnlink"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def toggle_links(self, event: AstrMessageEvent):
        """开关链接撤回：/撤回链接 开 | /撤回链接 关"""
        async for result in self._toggle(event, "link", "：消息含链接会被撤回"):
            yield result

    @filter.command("撤回二维码", alias={"kwnqr"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def toggle_qrcodes(self, event: AstrMessageEvent):
        """开关二维码撤回：/撤回二维码 开 | /撤回二维码 关"""
        async for result in self._toggle(
            event, "qrcode", "：消息含二维码图片会被撤回"
        ):
            yield result

    @filter.command("撤回表情", alias={"kwnemoji"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def toggle_emoji(self, event: AstrMessageEvent):
        """开关表情撤回：/撤回表情 开 | /撤回表情 关"""
        async for result in self._toggle(event, "emoji", "：消息含 emoji 字符会被撤回"):
            yield result

    @filter.command("撤回回复", alias={"kwnreply"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def set_reply(self, event: AstrMessageEvent):
        """设置各类撤回提示：/撤回回复 链接 文案（文案填 - 表示清空）"""
        arg = self._arg(event)
        if not arg:
            lines = [
                f"{TYPE_LABELS[key]}：{self._reply(key) or '（不发提示）'}"
                for key in TYPE_KEYS
            ]
            yield event.plain_result(
                "\n".join(
                    [f"撤回后提示：{'开启' if self._notify() else '关闭'}"]
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
                "类型只能是：关键词、号码、链接、二维码、Emoji。"
                "例：/撤回回复 链接 请勿发链接"
            )
            return
        if not content:
            yield event.plain_result(
                f"用法：/撤回回复 {TYPE_LABELS[key]} 文案，文案填 - 表示不发送提示"
            )
            return

        text = "" if content == "-" else content.strip()[:MAX_REPLY_LEN]
        self._set_group(key, "reply", text)
        ok = await self._persist()
        suffix = "" if ok else "（配置写回失败，重启后可能丢失）"

        if text:
            yield event.plain_result(f"{TYPE_LABELS[key]}撤回提示已改为：{text}{suffix}")
        else:
            yield event.plain_result(f"{TYPE_LABELS[key]}撤回后不再发送提示{suffix}")

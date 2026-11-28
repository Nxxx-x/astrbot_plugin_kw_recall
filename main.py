"""消息撤回 · 面向 QQ 官方机器人适配器（qq_official / qq_official_webhook）。

支持五类命中：关键词、5 位及以上连续数字、链接、二维码图片、Emoji，五类默认全部
开启，命中后调用官方 DELETE 接口撤回。

配置全部在 _conf_schema.json，到 AstrBot 管理面板 → 插件 → 本插件 → 配置里改，
保存即生效（插件读的就是同一份 config 对象）。插件不注册任何聊天指令，群里发
/撤回xx 之类不会被拦截。

官机撤回的三个前提，写死在 README 里，代码这边只做兜底：
1. 只能撤回 2 分钟内发送的消息，超时返回 40064004；
2. 机器人必须是群管理员才能撤回群成员的消息，否则 40062003；
3. 群主需在群设置里放开「接收全量消息」，否则机器人只看得到 @ 它的消息。
"""

from __future__ import annotations

import asyncio
import os
import random
import re
from collections import OrderedDict
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

# 按钮面板依赖较新的 AstrBot（能解析出 ButtonInteraction）。
# 老版本没有这个组件时，"菜单"自动降级为纯文本状态，其余功能不受影响。
try:
    from astrbot.api.message_components import ButtonInteraction
except Exception:  # pragma: no cover - 取决于 AstrBot 版本
    ButtonInteraction = None  # type: ignore[assignment]

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
    "emoji": "表情",
}
# 面板上的全部开关：五类规则 + 两个提示开关
SWITCH_KEYS = TYPE_KEYS + ("notify", "notify_at")
SWITCH_LABELS = {
    **TYPE_LABELS,
    "notify": "撤回提示",
    "notify_at": "提示@人",
}
# QQ 键盘最多 5 行、每行最多 5 个按钮；这里一行放一个按钮，所以一条键盘最多 5 个开关
SWITCHES_PER_KEYBOARD = 5
# 群里发这两个字就弹出开关面板
MENU_WORD = "菜单"
MENU_TITLE = "撤回开关 · 点按钮切换（仅群主/管理员可点）"
NOTIFY_TITLE = "提示设置 · 点按钮切换（仅群主/管理员可点）"
BUTTON_ID_PREFIX = "kwrecall_"
# 按钮权限：0 指定用户、1 仅管理者、2 所有人、3 指定身份组（仅频道）
# AstrBot 自己下发键盘时写死 2（所有人可点），这里自己拼 keyboard 用 1。
PERMISSION_ADMIN_ONLY = 1
DEFAULT_REPLIES = {
    "keyword": "消息含有关键词，已撤回",
    "number": "请勿发送号码，已撤回",
    "link": "请勿发送链接，已撤回",
    "qrcode": "请勿发送二维码，已撤回",
    "emoji": "请勿发送emoji，已撤回",
}
# 五类规则默认全部开启，装完即生效；不想要哪一类就去配置里单独关掉
DEFAULT_ENABLED = {
    "keyword": True,
    "number": True,
    "link": True,
    "qrcode": True,
    "emoji": True,
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


# 标准库 re 没有 Unicode 属性，只能按区段兜底；装了 regex 就用
# Extended_Pictographic / Regional_Indicator 精确判断，标签描述里的汉字不会被误判。
try:  # pragma: no cover - 取决于环境
    import regex as _regex

    _PICTO_RE: Any = _regex.compile(
        r"\p{Extended_Pictographic}|\p{Regional_Indicator}|\u20E3"
    )
except Exception:  # pragma: no cover
    _PICTO_RE = None

FALLBACK_EMOJI_RE = re.compile(
    "["
    "\U0001f000-\U0001faff"
    "\U0001f1e6-\U0001f1ff"
    "\u2600-\u27bf"
    "\u2b00-\u2bff"
    "\u2194-\u2199\u21a9-\u21aa"
    "\u2300-\u23ff"
    "\u25aa-\u25ab\u25b6\u25c0\u25fb-\u25fe"
    "\u00a9\u00ae\u203c\u2049\u2122\u2139\u24c2"
    "\u3030\u303d\u3297\u3299\u20e3"
    "]"
)

# 1️⃣ #️⃣ 这类键帽表情在降级模式下要整段匹配，否则会漏掉或把数字当正文
KEYCAP_RE = re.compile(r"[0-9#*]\ufe0f?\u20e3")

# 单条消息最多解析几张图，避免一条九宫格把事件循环拖住
MAX_IMAGES_PER_MESSAGE = 4
MAX_IMAGE_BYTES = 10 * 1024 * 1024

# 单张图的下载与解码上限，防止一张超大图长期占着线程
IMAGE_DOWNLOAD_TIMEOUT = 8
QR_DECODE_TIMEOUT = 10
# 同一张图（同一 URL / 文件名）只解码一次，群里反复转发的表情包不再重复算
QR_CACHE_SIZE = 256
QR_CONCURRENCY = 2

# 官机撤回的两类"预期内"失败：消息已超 2 分钟（40064004）、机器人没有群管理
# 权限（40062003）。这类不刷 warning，否则群里一条漏撤就报一次，日志会被冲爆。
RECALL_EXPIRED = "40064004"
RECALL_FORBIDDEN = "40062003"


def _has_emoji(text: str) -> bool:
    """判断文本里有没有 Unicode emoji（😀 ❤️ 👍 这类），有一个就算命中。

    规则只关心"有没有"，找到第一个就返回，不做全量计数——群里大部分消息
    不含表情，短路判断比逐簇统计省一大截。
    QQ 原生表情 / 超级表情（适配器转成的 [表情:捂脸]、[表情]）不算 emoji，
    这些标签和 @ 标签在判断前统一剥掉，避免标签里的描述文字干扰。
    """
    rest = _strip_markup(text)

    if _PICTO_RE is not None:
        return _PICTO_RE.search(rest) is not None

    return bool(FALLBACK_EMOJI_RE.search(rest) or KEYCAP_RE.search(rest))


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
        self._qr_detector = None
        self._qr_missing_warned = False
        # 解码是 CPU 密集操作，用信号量限制并发；同时避免把事件循环线程池占满
        self._qr_sem = asyncio.Semaphore(QR_CONCURRENCY)
        self._qr_cache: OrderedDict[str, str] = OrderedDict()
        # 词表解析结果缓存：配置对象每被保存一次，原始文本才变，届时自动重算
        self._kw_raw: Any = None
        self._kw_pairs: list[tuple[str, str]] = []

    # ------------------------------------------------------------------
    # 配置读取
    # ------------------------------------------------------------------

    def _group(self, key: str) -> dict[str, Any]:
        raw = self.config.get(key)
        return raw if isinstance(raw, dict) else {}

    def _notify(self) -> bool:
        return bool(self.config.get("notify", True))

    def _notify_at(self) -> bool:
        return bool(self.config.get("notify_at", True))

    def _on(self, key: str) -> bool:
        raw = self._group(key).get("enabled", DEFAULT_ENABLED[key])
        return bool(raw)

    def _reply(self, key: str) -> str:
        raw = self._group(key).get("reply", DEFAULT_REPLIES[key])
        if raw is None:
            raw = DEFAULT_REPLIES[key]
        return str(raw).strip()[:MAX_REPLY_LEN]

    def _keyword_pairs(self) -> list[tuple[str, str]]:
        """返回 (原词, 小写词)；配置没改时直接复用缓存，避免每条消息重解析+重 lower。"""
        raw = self._group("keyword").get("list", "")
        if raw == self._kw_raw:
            return self._kw_pairs
        self._kw_raw = raw
        self._kw_pairs = [(word, word.lower()) for word in _parse_keywords(raw)]
        return self._kw_pairs

    def _active_types(self) -> list[str]:
        return [key for key in TYPE_KEYS if self._on(key)]

    # ------------------------------------------------------------------
    # 开关面板（群主/管理员点按钮改配置）
    # ------------------------------------------------------------------

    def _switch_on(self, key: str) -> bool:
        """取某个开关的当前状态，五类规则和两个提示项统一走这里。"""
        if key in TYPE_KEYS:
            return self._on(key)
        if key == "notify":
            return self._notify()
        return self._notify_at()

    def _switch_line(self, keys: tuple[str, ...]) -> str:
        """一行显示这些开关的状态，如「关键词 开 · 号码 关 · …」。"""
        return " · ".join(
            f"{SWITCH_LABELS[key]} {'开' if self._switch_on(key) else '关'}"
            for key in keys
        )

    def _status_line(self) -> str:
        return self._switch_line(TYPE_KEYS)

    @staticmethod
    def _keyboard_rows(keys: tuple[str, ...], on_of) -> list[dict[str, Any]]:
        """一行一个按钮，开=蓝(1)、关=灰(0)；permission 用 1=仅管理者可操作。"""
        rows: list[dict[str, Any]] = []
        for key in keys:
            on = on_of(key)
            label = f"{SWITCH_LABELS[key]} {'开' if on else '关'}"
            rows.append(
                {
                    "buttons": [
                        {
                            "id": f"{BUTTON_ID_PREFIX}{key}",
                            "render_data": {
                                "label": label,
                                "visited_label": label,
                                "style": 1 if on else 0,
                            },
                            "action": {
                                "type": 1,  # 回调按钮，点了会推 INTERACTION_CREATE
                                "permission": {"type": PERMISSION_ADMIN_ONLY},
                                "data": f"{BUTTON_ID_PREFIX}{key}",
                            },
                        }
                    ]
                }
            )
        return rows

    async def _send_keyboard(
        self, event: AstrMessageEvent, text: str, rows: list[dict[str, Any]]
    ) -> bool:
        """直接调 botpy 发带按钮的消息。

        AstrBot 自己的 keyboard 序列化把 permission 写死成 2（所有人可点），
        所以这里绕开它自己拼 payload，换成 1（仅群主/管理员可点）。
        markdown 被平台拒绝时退到纯文本正文，按钮照发。
        """
        api = getattr(getattr(event, "bot", None), "api", None)
        group_id = event.get_group_id()
        if api is None or not group_id or not rows:
            return False

        msg_id = getattr(event.message_obj, "message_id", "") or ""
        keyboard = {"content": {"rows": rows}}
        base: dict[str, Any] = {
            "group_openid": group_id,
            "keyboard": keyboard,
            "msg_seq": random.randint(1, 10000),
        }
        if msg_id:
            base["msg_id"] = msg_id

        # 依次尝试：markdown 被动回复 → markdown 主动发送 → 纯文本被动 → 纯文本主动
        candidates: list[dict[str, Any]] = []
        for with_md in (True, False):
            for with_id in (True, False) if msg_id else (False,):
                payload = dict(base)
                if with_md:
                    payload["msg_type"] = 2
                    payload["markdown"] = {"content": text}
                else:
                    payload["msg_type"] = 0
                    payload["content"] = text
                if not with_id:
                    payload.pop("msg_id", None)
                candidates.append(payload)

        last: Exception | None = None
        for payload in candidates:
            try:
                await api.post_group_message(**payload)
                return True
            except Exception as exc:  # 换一种发法再试
                last = exc
        logger.warning(f"[kw_recall] 菜单发送失败：{last}")
        return False

    async def _send_menu(self, event: AstrMessageEvent) -> bool:
        """发开关面板。7 个开关、一条键盘最多 5 行，所以拆成两条消息。"""
        pages = (
            (MENU_TITLE, TYPE_KEYS),
            (NOTIFY_TITLE, ("notify", "notify_at")),
        )
        sent = False
        for title, keys in pages:
            text = f"{title}\n{self._switch_line(keys)}"
            rows = self._keyboard_rows(keys, self._switch_on)
            sent = await self._send_keyboard(event, text, rows) or sent
        return sent

    def _toggle_flag(self, key: str) -> bool:
        """翻转一个开关并返回新状态；配置里缺字段时补出来。"""
        if key in TYPE_KEYS:
            group = self.config.get(key)
            if not isinstance(group, dict):
                group = {}
                self.config[key] = group
            group["enabled"] = not self._on(key)
            return bool(group["enabled"])

        self.config[key] = not self._switch_on(key)
        return bool(self.config[key])

    async def _save_config(self) -> bool:
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

    @staticmethod
    def _interaction_key(event: AstrMessageEvent) -> str:
        """从按钮点击事件里取出被点的开关名；不是按钮事件或参数不认识时返回空串。

        权限不在代码里判：键盘是我们自己下发的，按钮带 permission.type=1，
        QQ 只在群主/管理员点击时才把 INTERACTION_CREATE 推过来，普通成员点了
        客户端直接拦掉，事件根本到不了这里。
        """
        if ButtonInteraction is None:
            return ""
        try:
            components = event.get_messages() or []
        except Exception:
            return ""

        for component in components:
            if not isinstance(component, ButtonInteraction):
                continue

            key = ""
            data = getattr(component, "data", None)
            if isinstance(data, dict):
                key = str(data.get("k", ""))
            elif isinstance(data, str):
                key = data.removeprefix(BUTTON_ID_PREFIX)
            if key not in SWITCH_KEYS:
                # 兜底：只带按钮 id 时按 id 反解
                key = str(getattr(component, "action_id", "")).removeprefix(
                    BUTTON_ID_PREFIX
                )
            return key if key in SWITCH_KEYS else ""
        return ""

    async def _apply_toggle(self, key: str) -> str:
        """切换开关、写回配置，返回给群里的那行提示。"""
        enabled = self._toggle_flag(key)
        saved = await self._save_config()
        suffix = "" if saved else "（保存失败，重启后可能复原）"
        keys = TYPE_KEYS if key in TYPE_KEYS else ("notify", "notify_at")
        return (
            f"{SWITCH_LABELS[key]}已{'开启' if enabled else '关闭'}{suffix}\n"
            f"{self._switch_line(keys)}"
        )

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
            for word, lowered_word in self._keyword_pairs():
                if lowered_word in lowered:
                    return "keyword", f"关键词「{word}」"

        if self._on("link"):
            found = LINK_RE.search(text)
            if found:
                return "link", f"链接 {found.group(0)[:40]}"

        if self._on("number"):
            found = NUMBER_RE.search(text)
            if found:
                return "number", f"号码 {found.group(0)}"

        if self._on("emoji") and _has_emoji(text):
            return "emoji", "含 emoji"

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
            key = self._image_key(image)
            if key and key in self._qr_cache:
                content = self._qr_cache[key]
                if content:
                    return "qrcode", f"二维码（{content[:30]}）"
                continue

            path = await self._image_path(image)
            if not path:
                continue
            content = await self._decode_qr(path)
            if key:
                self._remember_qr(key, content)
            if content:
                return "qrcode", f"二维码（{content[:30]}）"
        return None

    @staticmethod
    def _image_key(image: Any) -> str:
        """给图片取一个稳定标识，用来命中解码缓存。

        官机图片的 url 带时效签名，同一张图重新转发时 url 会变，所以优先用
        框架落盘路径与文件名（AstrBot 按内容命名），url 只作兜底。
        """
        for attr in ("path", "file", "url"):
            value = getattr(image, attr, "")
            if isinstance(value, str) and value:
                return f"{len(value)}:{value[:180]}"
        return ""

    def _remember_qr(self, key: str, content: str) -> None:
        """记下"这张图有没有二维码"，超出容量按最久未用淘汰。"""
        self._qr_cache[key] = content
        self._qr_cache.move_to_end(key)
        while len(self._qr_cache) > QR_CACHE_SIZE:
            self._qr_cache.popitem(last=False)

    async def _image_path(self, image: Any) -> str:
        convert = getattr(image, "convert_to_file_path", None)
        if not callable(convert):
            return ""
        try:
            path = await asyncio.wait_for(convert(), timeout=IMAGE_DOWNLOAD_TIMEOUT)
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
        # 解码是 CPU 密集操作，放线程池并限并发；超时直接放弃，别拖住消息处理
        async with self._qr_sem:
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(self._decode_qr_sync, detector, path),
                    timeout=QR_DECODE_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.debug("[kw_recall] 二维码解码超时，跳过")
                return ""
            except Exception as exc:
                logger.debug(f"[kw_recall] 二维码解码异常：{exc}")
                return ""

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

        # 只判定"有没有码 + 码里是什么"，取第一个即可，不需要 multi 解码
        for candidate in MessageRecall._qr_candidates(image, cv2):
            try:
                text, _points, _ = detector.detectAndDecode(candidate)
            except Exception:
                continue
            if text:
                return text
        return ""

    # ------------------------------------------------------------------
    # 官机相关
    # ------------------------------------------------------------------

    @staticmethod
    def _is_group_admin(event: AstrMessageEvent) -> bool:
        """群主 / 管理员发的消息不撤，避免得罪人和无权限报错。"""
        raw = getattr(event.message_obj, "raw_message", None)
        author = getattr(raw, "author", None)
        role = getattr(author, "member_role", "") or ""
        return role.lower() in ("owner", "admin", "administrator")

    async def _recall_group_message(
        self, event: AstrMessageEvent, message_id: str
    ) -> None:
        """调用官机撤回接口。AstrBot 没有封装群消息撤回，只能自己发 DELETE。"""
        if Route is None:
            raise RuntimeError("未找到 botpy，当前环境无法撤回")
        bot = getattr(event, "bot", None)
        if bot is None:
            raise RuntimeError("当前适配器不支持撤回")

        # botpy 各版本挂载位置略有差异：新版在 api._http，旧版在 api.http
        api = getattr(bot, "api", None)
        http = getattr(api, "_http", None) or getattr(api, "http", None)
        if http is None:
            raise RuntimeError("当前 botpy 版本不支持该撤回接口")

        route = Route(
            "DELETE",
            "/v2/groups/{group_openid}/messages/{message_id}",
            group_openid=str(event.get_group_id()),
            message_id=str(message_id),
        )
        await http.request(route)

    # ------------------------------------------------------------------
    # 消息监听
    # ------------------------------------------------------------------

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.platform_adapter_type(QQ_OFFICIAL_TYPES)
    async def on_group_message(self, event: AstrMessageEvent):
        """群消息入口：菜单、按钮点击、五类规则。"""
        # 1. 按钮点击：message_str 是空的，消息体是 ButtonInteraction 组件
        key = (
            self._interaction_key(event)
            if not (event.message_str or "").strip()
            else ""
        )
        if key:
            yield event.plain_result(await self._apply_toggle(key))
            event.stop_event()
            return

        text = event.message_str or ""

        # 2. 菜单：先于规则判断，免得「菜单」两个字被关键词规则顺手撤掉。
        # 谁能发都行，面板照发；点不点得动由按钮的 permission.type=1 决定。
        if text.strip() == MENU_WORD:
            if not await self._send_menu(event):
                # 发不出带按钮的消息（老版本/接口拒绝）时退成一条纯文本状态
                yield event.plain_result(f"{MENU_TITLE}\n{self._status_line()}")
            event.stop_event()
            return

        # 3. 五类规则
        active = self._active_types()
        if not active:
            return
        if active == ["keyword"] and not self._keyword_pairs():
            # 只剩关键词但词表是空的，没什么可匹配
            return

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
            detail = str(exc)
            if RECALL_EXPIRED in detail or RECALL_FORBIDDEN in detail:
                logger.debug(f"[kw_recall] 未能撤回（{reason}）：{detail[:120]}")
            else:
                logger.warning(f"[kw_recall] 撤回失败（{reason}）：{detail[:200]}")
            return

        logger.info(f"[kw_recall] 已撤回消息，原因：{reason}")

        reply_text = self._reply(hit_type)
        if self._notify() and reply_text:
            sender_id = event.get_sender_id()
            if self._notify_at() and sender_id:
                # 官机会把 At 转成 <qqbot-at-user id="openid" /> 内联进文本，
                # openid 正好就是发送者 ID。
                yield event.chain_result(
                    [Comp.At(qq=sender_id), Comp.Plain(f" {reply_text}")]
                )
            else:
                yield event.plain_result(reply_text)

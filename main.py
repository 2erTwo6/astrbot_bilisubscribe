"""Bilibili 直播订阅插件。

通过 LLM 工具搜索 B 站主播、按直播间号订阅 / 取消订阅，并在开播、下播时
向订阅时所在的会话推送提醒。开播消息会附带一张当前直播画面截图。
"""

from __future__ import annotations

import asyncio
import hashlib
import time
import urllib.parse
from functools import reduce
from typing import Any

import aiohttp

from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image
from astrbot.api.star import Context, Star

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
WEB_REFERER = "https://www.bilibili.com/"
LIVE_REFERER = "https://live.bilibili.com/"
SEARCH_URL = "https://api.bilibili.com/x/web-interface/wbi/search/type"
NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
ROOM_INFO_URL = "https://api.live.bilibili.com/room/v1/Room/get_info"
MASTER_INFO_URL = "https://api.live.bilibili.com/live_user/v1/Master/info"

# B 站 WBI 签名使用的重排表，用于把 img_key + sub_key 混淆成 mixin key。
MIXIN_KEY_ENC_TAB = [
    46,
    47,
    18,
    2,
    53,
    8,
    23,
    32,
    15,
    50,
    10,
    31,
    58,
    3,
    45,
    35,
    27,
    43,
    5,
    49,
    33,
    9,
    42,
    19,
    29,
    28,
    14,
    39,
    12,
    38,
    41,
    13,
    37,
    48,
    7,
    16,
    24,
    55,
    40,
    61,
    26,
    17,
    0,
    1,
    60,
    51,
    30,
    4,
    22,
    25,
    54,
    21,
    56,
    59,
    6,
    63,
    57,
    62,
    11,
    36,
    20,
    34,
    44,
    52,
]

# 0=未开播，1=直播中，2=轮播（主播已下播，平台在放录像）。
LIVE_STATUS_OFF = 0
LIVE_STATUS_ON = 1


def _mixin_key(raw: str) -> str:
    """按 WBI 重排表从 img_key + sub_key 生成 mixin key。

    Args:
        raw: img_key 与 sub_key 拼接后的 64 位字符串。

    Returns:
        前 32 位混淆后的 mixin key。
    """
    return reduce(lambda acc, index: acc + raw[index], MIXIN_KEY_ENC_TAB, "")[:32]


def _sign_wbi(params: dict[str, Any], img_key: str, sub_key: str) -> dict[str, Any]:
    """为请求参数追加 WBI 签名（wts 与 w_rid）。

    Args:
        params: 原始查询参数。
        img_key: nav 接口返回的 img_url 文件名。
        sub_key: nav 接口返回的 sub_url 文件名。

    Returns:
        追加签名后的新参数字典。
    """
    mixin = _mixin_key(img_key + sub_key)
    signed = dict(params)
    signed["wts"] = int(time.time())
    # 先过滤特殊字符并按 key 排序，才能和 B 站服务端算出同一个摘要。
    signed = {
        key: "".join(ch for ch in str(value) if ch not in "!'()*")
        for key, value in sorted(signed.items())
    }
    query = urllib.parse.urlencode(signed)
    signed["w_rid"] = hashlib.md5((query + mixin).encode()).hexdigest()
    return signed


def _format_fans(fans: int) -> str:
    """把粉丝数格式化成便于阅读的形式。

    Args:
        fans: 原始粉丝数。

    Returns:
        例如 ``20860070（2086.0 万）``；粉丝数小于一万时只返回原始数字。
    """
    if fans >= 10000:
        return f"{fans}（{fans / 10000:.1f} 万）"
    return str(fans)


def _format_duration(seconds: float) -> str:
    """把直播时长格式化为中文描述。

    Args:
        seconds: 直播时长（秒）。

    Returns:
        例如 ``3 小时 25 分钟``。
    """
    total_minutes = max(int(seconds // 60), 0)
    if total_minutes < 1:
        return "不到 1 分钟"
    hours, minutes = divmod(total_minutes, 60)
    if hours:
        return f"{hours} 小时 {minutes} 分钟"
    return f"{minutes} 分钟"


def _normalize_cookie(raw: str) -> str:
    """整理配置里的 Cookie 字符串。

    用户可能从浏览器直接复制整段 Cookie，也可能只粘贴 ``SESSDATA=...`` 的值。

    Args:
        raw: 原始配置值。

    Returns:
        可直接放入 Cookie 请求头的字符串；未配置时返回空字符串。
    """
    cookie = (raw or "").strip()
    if not cookie:
        return ""
    # 只粘贴了 SESSDATA 的值时补上键名，否则请求头无效。
    if "=" not in cookie:
        return f"SESSDATA={cookie}"
    return cookie


def _parse_live_time(value: Any) -> int:
    """把接口里的开播时间统一转换成 Unix 时间戳。

    Args:
        value: 接口返回值，可能是时间戳，也可能是 ``0000-00-00 00:00:00``。

    Returns:
        开播时间戳；无法解析或未开播时返回 0。
    """
    if isinstance(value, (int, float)) and value > 0:
        return int(value)
    if isinstance(value, str) and value.strip() not in ("", "0000-00-00 00:00:00"):
        try:
            return int(time.mktime(time.strptime(value.strip(), "%Y-%m-%d %H:%M:%S")))
        except ValueError:
            return 0
    return 0


class BiliApi:
    """B 站直播相关的只读接口封装。

    只依赖传入的 aiohttp 会话，方便在不启动 AstrBot 的情况下单独测试。
    """

    def __init__(self, session: aiohttp.ClientSession, cookie: str = "") -> None:
        self._session = session
        self._cookie = _normalize_cookie(cookie)
        self._wbi_keys: tuple[str, str] | None = None
        self._wbi_expire = 0.0

    @property
    def has_cookie(self) -> bool:
        """是否配置了 Cookie。"""
        return bool(self._cookie)

    async def _get_json(
        self,
        url: str,
        params: Any = None,
        referer: str = LIVE_REFERER,
    ) -> dict[str, Any] | None:
        """发起 GET 请求并解析 JSON，任何异常都只记录日志并返回 None。

        Args:
            url: 请求地址。
            params: 查询参数。
            referer: Referer 请求头。

        Returns:
            解析后的 JSON 对象，失败时为 None。
        """
        headers = {"User-Agent": USER_AGENT, "Referer": referer}
        if self._cookie:
            headers["Cookie"] = self._cookie
        try:
            async with self._session.get(
                url,
                params=params,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    return None
                return await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return None

    async def _get_wbi_keys(self) -> tuple[str, str] | None:
        """获取并缓存 WBI 签名所需的 img_key / sub_key。

        未登录时 nav 接口的 code 为 -101，但 data.wbi_img 仍然有效，因此不校验 code。

        Returns:
            (img_key, sub_key)，获取失败时为 None。
        """
        if self._wbi_keys and time.time() < self._wbi_expire:
            return self._wbi_keys
        payload = await self._get_json(NAV_URL, referer=WEB_REFERER)
        if not payload:
            return None
        wbi_img = (payload.get("data") or {}).get("wbi_img") or {}
        img_url = wbi_img.get("img_url") or ""
        sub_url = wbi_img.get("sub_url") or ""
        if not img_url or not sub_url:
            return None
        self._wbi_keys = (
            img_url.rsplit("/", 1)[-1].split(".")[0],
            sub_url.rsplit("/", 1)[-1].split(".")[0],
        )
        # 官方密钥按天轮换，缓存 6 小时足够安全。
        self._wbi_expire = time.time() + 6 * 3600
        return self._wbi_keys

    async def search_users(self, keyword: str, limit: int = 5) -> list[dict[str, Any]]:
        """按关键词搜索 B 站用户。

        Args:
            keyword: 搜索关键词，通常是主播昵称。
            limit: 最多返回的用户数量。

        Returns:
            用户信息列表，每项包含 mid、uname、fans、room_id、is_live。
        """
        keys = await self._get_wbi_keys()
        if not keys:
            return []
        params = _sign_wbi(
            {"search_type": "bili_user", "keyword": keyword, "page": 1},
            keys[0],
            keys[1],
        )
        payload = await self._get_json(SEARCH_URL, params=params, referer=WEB_REFERER)
        if not payload or payload.get("code") != 0:
            return []
        users: list[dict[str, Any]] = []
        for item in ((payload.get("data") or {}).get("result") or [])[:limit]:
            users.append(
                {
                    "mid": int(item.get("mid") or 0),
                    "uname": item.get("uname") or "",
                    "fans": int(item.get("fans") or 0),
                    "room_id": int(item.get("room_id") or 0),
                    "is_live": int(item.get("is_live") or 0),
                }
            )
        return users

    async def fetch_room_info(self, room_id: int) -> dict[str, Any] | None:
        """查询直播间状态。

        Args:
            room_id: 直播间号，短号也可以。

        Returns:
            含 uid、room_id、uname、title、live_status、live_time、keyframe、
            cover、attention 的字典；直播间不存在时为 None。
        """
        payload = await self._get_json(ROOM_INFO_URL, params={"room_id": room_id})
        if not payload or payload.get("code") != 0:
            return None
        data = payload.get("data") or {}
        uid = int(data.get("uid") or 0)
        if not uid:
            return None
        return {
            "uid": uid,
            "room_id": int(data.get("room_id") or room_id),
            "uname": await self.fetch_uname(uid),
            "title": data.get("title") or "",
            "live_status": int(data.get("live_status") or 0),
            "live_time": _parse_live_time(data.get("live_time")),
            "keyframe": data.get("keyframe") or "",
            "cover": data.get("user_cover") or "",
            "attention": int(data.get("attention") or 0),
        }

    async def fetch_uname(self, uid: int) -> str:
        """查询主播昵称。

        直播 get_info 接口不返回昵称，需要用主播信息接口补一次。

        Args:
            uid: 主播 UID。

        Returns:
            主播昵称，失败时返回空字符串。
        """
        payload = await self._get_json(MASTER_INFO_URL, params={"uid": uid})
        if not payload or payload.get("code") != 0:
            return ""
        return ((payload.get("data") or {}).get("info") or {}).get("uname") or ""

    async def fetch_image(self, url: str) -> bytes | None:
        """下载直播画面截图。

        Args:
            url: 截图地址。

        Returns:
            图片二进制数据，失败或内容过小时返回 None。
        """
        if not url:
            return None
        headers = {"User-Agent": USER_AGENT, "Referer": LIVE_REFERER}
        if self._cookie:
            headers["Cookie"] = self._cookie
        try:
            async with self._session.get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.read()
                # 过小的响应基本是错误页，直接丢弃。
                return data if len(data) > 1024 else None
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None


class BiliSubscribePlugin(Star):
    """B 站直播订阅插件主类。"""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        config = config or {}
        self.poll_interval = max(int(config.get("poll_interval", 60)), 30)
        self.max_search_results = min(
            max(int(config.get("max_search_results", 5)), 1),
            10,
        )
        self.send_screenshot = bool(config.get("send_screenshot", True))
        self.cookie = _normalize_cookie(str(config.get("cookie", "")))

        # 结构: {unified_msg_origin: {room_id_str: 订阅信息}}
        self.subscriptions: dict[str, dict[str, dict[str, Any]]] = {}
        self._session: aiohttp.ClientSession | None = None
        self._api: BiliApi | None = None
        self._poll_task: asyncio.Task | None = None

    async def initialize(self) -> None:
        """插件激活时加载订阅数据并启动轮询任务。"""
        saved = await self.get_kv_data("subscriptions", {})
        if isinstance(saved, dict):
            self.subscriptions = {
                str(umo): {str(rid): sub for rid, sub in rooms.items()}
                for umo, rooms in saved.items()
                if isinstance(rooms, dict)
            }
        self._session = aiohttp.ClientSession()
        self._api = BiliApi(self._session, self.cookie)
        self._poll_task = asyncio.create_task(self._poll_loop())
        self.logger.info(
            "B 站直播订阅插件已启动，轮询间隔 %s 秒，已加载 %s 个会话的订阅，"
            "Cookie 状态：%s。",
            self.poll_interval,
            len(self.subscriptions),
            "已配置" if self._api.has_cookie else "未配置（使用游客身份）",
        )

    async def terminate(self) -> None:
        """插件停用或重载时停止轮询并释放 HTTP 会话。"""
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        if self._session:
            await self._session.close()
            self._session = None
        self._api = None

    async def _save(self) -> None:
        """把订阅数据写入插件 KV 存储。"""
        try:
            await self.put_kv_data("subscriptions", self.subscriptions)
        except Exception as exc:  # 存储失败不应影响推送逻辑
            self.logger.error("保存直播订阅数据失败: %s", exc)

    async def _poll_loop(self) -> None:
        """周期轮询所有被订阅直播间的状态。"""
        # 稍作延迟，避免和插件加载、平台连接初始化抢资源。
        await asyncio.sleep(10)
        while True:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # 任何单轮异常都不应终止轮询
                self.logger.error("直播状态轮询异常: %s", exc, exc_info=True)
            await asyncio.sleep(self.poll_interval)

    async def _poll_once(self) -> None:
        """执行一轮状态检查，处理开播与下播的推送。"""
        if not self._api:
            return
        # 同一直播间可能被多个会话订阅，按 room_id 去重后只查一次。
        room_ids: set[str] = set()
        for rooms in self.subscriptions.values():
            room_ids.update(rooms)
        if not room_ids:
            return

        semaphore = asyncio.Semaphore(3)

        async def check(room_id: str) -> tuple[str, dict[str, Any] | None]:
            async with semaphore:
                return room_id, await self._api.fetch_room_info(int(room_id))

        results = await asyncio.gather(*(check(rid) for rid in room_ids))
        changed = False
        for room_id, info in results:
            if not info:
                continue
            for umo, rooms in self.subscriptions.items():
                sub = rooms.get(room_id)
                if sub and await self._handle_status(umo, sub, info):
                    changed = True
        if changed:
            await self._save()

    async def _handle_status(
        self,
        umo: str,
        sub: dict[str, Any],
        info: dict[str, Any],
    ) -> bool:
        """根据最新状态判断是否需要推送开播 / 下播消息。

        Args:
            umo: 订阅所属会话。
            sub: 订阅记录（会被就地更新）。
            info: 接口返回的最新直播间信息。

        Returns:
            订阅记录是否发生变化（用于决定是否持久化）。
        """
        live_status = info["live_status"]
        is_live = live_status == LIVE_STATUS_ON
        was_live = int(sub.get("last_status", live_status)) == LIVE_STATUS_ON

        if is_live and not was_live:
            sub["live_status"] = live_status
            sub["last_status"] = live_status
            sub["live_started_at"] = info["live_time"] or int(time.time())
            sub["uname"] = info["uname"] or sub.get("uname", "")
            sub["title"] = info["title"] or sub.get("title", "")
            await self._notify_live(umo, sub, info)
            return True
        if not is_live and was_live:
            sub["last_status"] = live_status
            sub["live_status"] = live_status
            started_at = int(sub.get("live_started_at") or 0)
            duration = int(time.time()) - started_at if started_at else 0
            sub["live_started_at"] = 0
            await self._notify_offline(umo, sub, duration)
            return True
        if live_status != sub.get("last_status"):
            sub["last_status"] = live_status
            sub["live_status"] = live_status
            return True
        if info["uname"] and sub.get("uname") != info["uname"]:
            sub["uname"] = info["uname"]
            return True
        return False

    async def _notify_live(
        self,
        umo: str,
        sub: dict[str, Any],
        info: dict[str, Any],
    ) -> None:
        """发送开播提醒，附带当前直播画面截图。

        Args:
            umo: 目标会话。
            sub: 订阅记录。
            info: 最新直播间信息。
        """
        uname = sub.get("uname") or "主播"
        title = info.get("title") or sub.get("title") or "无标题"
        chain = MessageChain().message(
            f"你订阅的主播{uname}开播啦！\n直播间标题：{title}"
        )
        if self.send_screenshot and self._api:
            # keyframe 是 B 站为该直播间生成的实时画面截图；取不到时退回封面图。
            image_bytes = await self._api.fetch_image(
                info.get("keyframe") or info.get("cover") or "",
            )
            if image_bytes:
                chain.chain.append(Image.fromBytes(image_bytes))
            else:
                self.logger.warning("未能获取 %s 的直播画面截图。", uname)
        await self._send(umo, chain)

    async def _notify_offline(
        self,
        umo: str,
        sub: dict[str, Any],
        duration: int,
    ) -> None:
        """发送下播提醒。

        Args:
            umo: 目标会话。
            sub: 订阅记录。
            duration: 直播时长（秒）。
        """
        uname = sub.get("uname") or "主播"
        await self._send(
            umo,
            MessageChain().message(
                f"{uname}下播啦。直播时长：{_format_duration(duration)}。"
            ),
        )

    async def _send(self, umo: str, chain: MessageChain) -> None:
        """向指定会话主动发送消息。

        Args:
            umo: unified_msg_origin。
            chain: 消息链。
        """
        try:
            sent = await self.context.send_message(umo, chain)
            if not sent:
                self.logger.warning("未能找到会话 %s 对应的平台，消息未发送。", umo)
        except Exception as exc:
            self.logger.error("发送直播提醒失败 %s: %s", umo, exc, exc_info=True)

    def _session_subs(self, event: AstrMessageEvent) -> dict[str, dict[str, Any]]:
        """获取当前会话的订阅记录。

        Args:
            event: 消息事件。

        Returns:
            room_id 字符串到订阅信息的映射（返回内部引用，可直接修改）。
        """
        return self.subscriptions.setdefault(event.unified_msg_origin, {})

    @filter.llm_tool(name="bilibili_search_user")
    async def search_bili_user(self, event: AstrMessageEvent, keyword: str) -> str:
        """按关键词搜索 B 站主播，返回主播的粉丝量和直播间号。

        Args:
            keyword(string): 要搜索的主播昵称或关键词
        """
        if not self._api:
            return "插件尚未初始化完成，请稍后再试。"
        keyword = keyword.strip()
        if not keyword:
            return "请提供要搜索的主播昵称。"
        users = await self._api.search_users(keyword, self.max_search_results)
        if not users:
            return f"没有搜索到与「{keyword}」相关的 B 站用户。"

        lines = [f"搜索「{keyword}」的结果："]
        for index, user in enumerate(users, start=1):
            state = "直播中" if user["is_live"] else "未开播"
            room_text = (
                f"直播间号 {user['room_id']}（{state}）"
                if user["room_id"]
                else "未开通直播间"
            )
            lines.append(
                f"{index}. {user['uname']}（UID {user['mid']}）"
                f"粉丝数 {_format_fans(user['fans'])}，{room_text}"
            )
        return "\n".join(lines)

    @filter.llm_tool(name="bilibili_subscribe_live")
    async def subscribe_live(self, event: AstrMessageEvent, room_id: int) -> str:
        """按直播间号订阅主播，开播和下播时会在当前会话收到提醒。

        Args:
            room_id(number): B 站直播间号（数字）
        """
        if not self._api:
            return "插件尚未初始化完成，请稍后再试。"
        # 订阅会持续占用轮询与推送资源，仅允许管理员操作。
        if not event.is_admin():
            return "只有 AstrBot 管理员才能订阅直播间，请联系管理员操作。"
        room_id = int(room_id)
        if room_id <= 0:
            return "直播间号必须是正整数。"
        rooms = self._session_subs(event)
        if str(room_id) in rooms:
            return f"当前会话已经订阅了直播间 {room_id}，无需重复订阅。"

        info = await self._api.fetch_room_info(room_id)
        if not info:
            return f"没有找到直播间 {room_id}，请确认直播间号是否正确。"

        status = info["live_status"]
        rooms[str(info["room_id"])] = {
            "room_id": info["room_id"],
            "uid": info["uid"],
            "uname": info["uname"],
            "title": info["title"],
            "last_status": status,
            "live_status": status,
            # 订阅时正在直播则记录开播时间，保证下播时能算出完整时长。
            "live_started_at": info["live_time"] if status == LIVE_STATUS_ON else 0,
            "subscribed_at": int(time.time()),
        }
        await self._save()
        return (
            f"订阅成功：已订阅主播 {info['uname'] or info['uid']} 的直播间 "
            f"{info['room_id']}（当前共 {len(rooms)} 个订阅）。"
            "开播和下播时会在本会话收到提醒。"
        )

    @filter.llm_tool(name="bilibili_unsubscribe_live")
    async def unsubscribe_live(self, event: AstrMessageEvent, room_id: int) -> str:
        """取消订阅指定的直播间。

        Args:
            room_id(number): 要取消订阅的 B 站直播间号（数字）
        """
        # 取消订阅和订阅对称，同样只允许管理员操作，避免普通成员清空群订阅。
        if not event.is_admin():
            return "只有 AstrBot 管理员才能取消订阅，请联系管理员操作。"
        room_id = int(room_id)
        rooms = self._session_subs(event)
        sub = rooms.pop(str(room_id), None)
        if not sub:
            return f"当前会话没有订阅直播间 {room_id}。"
        await self._save()
        return (
            f"已取消订阅主播 {sub.get('uname') or ''} 的直播间 {room_id}"
            f"（当前剩余 {len(rooms)} 个订阅）。"
        )

    @filter.llm_tool(name="bilibili_list_subscriptions")
    async def list_subs_tool(self, event: AstrMessageEvent) -> str:
        """查看当前会话已经订阅的 B 站直播间列表，包含主播昵称、直播间号和直播状态。"""
        rooms = self._session_subs(event)
        if not rooms:
            return "当前会话还没有订阅任何直播间。"

        lines = [f"当前会话共订阅了 {len(rooms)} 个直播间："]
        for index, sub in enumerate(rooms.values(), start=1):
            is_live = sub.get("last_status") == LIVE_STATUS_ON
            details = [
                f"直播间号 {sub.get('room_id')}",
                "直播中" if is_live else "未开播",
            ]
            # 直播中才显示开播时间，未开播时这个字段是 0，格式化出来会是 1970 年。
            started_at = int(sub.get("live_started_at") or 0)
            if is_live and started_at:
                details.append(
                    f"开播于 {time.strftime('%Y-%m-%d %H:%M', time.localtime(started_at))}",
                )
            subscribed_at = int(sub.get("subscribed_at") or 0)
            if subscribed_at:
                details.append(
                    f"订阅于 {time.strftime('%Y-%m-%d %H:%M', time.localtime(subscribed_at))}",
                )
            lines.append(
                f"{index}. {sub.get('uname') or '未知主播'}（{'，'.join(details)}）",
            )
        return "\n".join(lines)

    @filter.command_group("bilisub")
    def bilisub(self):
        """B 站直播订阅管理。"""

    @bilisub.command("list")
    async def list_subscriptions(self, event: AstrMessageEvent):
        """查看当前会话的直播订阅列表。"""
        rooms = self._session_subs(event)
        if not rooms:
            yield event.plain_result(
                "当前会话还没有订阅任何直播间。可以让 AI 搜索主播并订阅。",
            )
            return
        lines = ["当前会话的直播订阅："]
        for sub in rooms.values():
            state = "直播中" if sub.get("last_status") == LIVE_STATUS_ON else "未开播"
            lines.append(
                f"- {sub.get('uname') or '未知主播'}"
                f"（直播间 {sub.get('room_id')}，{state}）",
            )
        yield event.plain_result("\n".join(lines))

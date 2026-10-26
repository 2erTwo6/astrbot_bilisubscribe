"""本地手工联调脚本（非单元测试）。

直接对真实 B 站接口跑一遍搜索、订阅、开播 / 下播推送的完整流程，
用于在没有启动 AstrBot 的情况下验证插件逻辑。需要联网。

用法（在 AstrBot 运行环境里执行）::

    python astrbot_bilisubscribe/tests/test_live_flow.py
"""

import asyncio
import sys
import time
from pathlib import Path

# 插件目录在父级，直接按文件路径导入 main.py，避免依赖 AstrBot 的插件加载机制。
PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

from main import (  # noqa: E402
    LIVE_STATUS_ON,
    BiliSubscribePlugin,
    _format_duration,
    _normalize_cookie,
    _parse_live_time,
)


class FakeResult:
    """模拟 event.plain_result 的返回值。"""

    def __init__(self, text):
        self.text = text

    def __repr__(self):
        return f"PlainResult({self.text!r})"


class FakeEvent:
    """最小可用的消息事件替身。"""

    unified_msg_origin = "aiocqhttp:FriendMessage:10001"

    def plain_result(self, text):
        return FakeResult(text)


class FakeContext:
    """记录所有主动发送消息的 Context 替身。"""

    def __init__(self):
        self.sent = []

    async def send_message(self, umo, chain):
        self.sent.append((umo, chain))
        return True


async def main() -> None:
    ctx = FakeContext()
    plugin = BiliSubscribePlugin(ctx, {"poll_interval": 60, "send_screenshot": True})

    # 用内存字典代替插件 KV 存储，脱离 AstrBot 运行时也能跑。
    store = {}

    async def fake_put(key, value):
        store[key] = value

    async def fake_get(key, default):
        return store.get(key, default)

    plugin.put_kv_data = fake_put
    plugin.get_kv_data = fake_get
    await plugin.initialize()
    event = FakeEvent()

    print("--- 搜索主播 ---")
    print(await plugin.search_bili_user(event, keyword="嘉然"))
    print("--- 空关键词 ---")
    print(await plugin.search_bili_user(event, keyword="  "))
    print("--- 订阅不存在的直播间 ---")
    print(await plugin.subscribe_live(event, room_id=999999999))
    print("--- 订阅 544641 ---")
    print(await plugin.subscribe_live(event, room_id=544641))
    print("--- 重复订阅 ---")
    print(await plugin.subscribe_live(event, room_id=544641))
    print("--- 订阅 21852（未开播）---")
    print(await plugin.subscribe_live(event, room_id=21852))
    print("--- 取消未订阅的直播间 ---")
    print(await plugin.unsubscribe_live(event, room_id=1234567))
    print("--- /bilisub list ---")
    async for result in plugin.list_subscriptions(event):
        print(result)

    rooms = plugin._session_subs(event)
    live_info = await plugin._api.fetch_room_info(544641)
    print("--- 开播推送（真实直播间 544641）---")
    await plugin._notify_live(event.unified_msg_origin, rooms["544641"], live_info)
    umo, chain = ctx.sent[-1]
    print("umo:", umo)
    print("消息组件:", [type(c).__name__ for c in chain.chain])
    print("文本:", chain.chain[0].text)

    print("--- 真实轮询一轮 ---")
    await plugin._poll_once()
    print("订阅数据:", plugin.subscriptions)

    print("--- 强制下播转换 ---")
    rooms["544641"]["last_status"] = LIVE_STATUS_ON
    rooms["544641"]["live_started_at"] = int(time.time()) - 3720
    offline_info = dict(live_info)
    offline_info["live_status"] = 0
    await plugin._handle_status(event.unified_msg_origin, rooms["544641"], offline_info)
    print("下播文本:", ctx.sent[-1][1].chain[0].text)

    print("--- 辅助函数 ---")
    print(_format_duration(0), "|", _format_duration(90), "|", _format_duration(3720))
    print(
        _parse_live_time("2026-10-26 14:05:35"),
        _parse_live_time("0000-00-00 00:00:00"),
        _parse_live_time(None),
    )
    print("--- Cookie 归一化 ---")
    for raw in ("", "  ", "abc123", "SESSDATA=abc123; bili_jct=xyz"):
        print(repr(raw), "->", repr(_normalize_cookie(raw)))
    print("游客模式 has_cookie:", plugin._api.has_cookie)

    await plugin.terminate()
    print("done")


if __name__ == "__main__":
    asyncio.run(main())

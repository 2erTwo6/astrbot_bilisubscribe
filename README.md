# astrbot_bilisubscribe

AstrBot 的 B 站直播订阅插件：让 AI 帮你搜索主播、订阅直播间，并在开播 / 下播时向你所在的会话推送提醒。

## 功能

插件向 LLM 注册了 3 个工具，直接用自然语言对话即可使用：

| 工具 | 说明 |
| --- | --- |
| `bilibili_search_user` | 按关键词搜索 B 站用户，返回粉丝量、直播间号和当前是否在播 |
| `bilibili_subscribe_live` | 按**直播间号**订阅，订阅关系绑定到当前会话 |
| `bilibili_unsubscribe_live` | 按直播间号取消订阅 |

被订阅的直播间开播时会推送：

```
你订阅的主播xxx开播啦！
直播间标题：xxxx
[直播画面]
```

下播时推送：

```
xxx下播啦。直播时长：xxx。
```

另外提供 `/bilisub list` 指令查看当前会话的订阅列表。

## 实现要点

- **搜索**走 B 站的 `wbi/search/type` 接口，需要 WBI 签名。插件从 `nav` 接口拿 `img_key` / `sub_key`（未登录时 `code=-101` 但密钥依然有效），按官方重排表算出 mixin key 后对参数做 MD5 签名，密钥缓存 6 小时。
- **状态轮询**用 `room/v1/Room/get_info`，单房间一次请求即可拿到 `live_status`、`live_time`、`keyframe`、`user_cover`。没有用批量接口 `get_status_info_by_uids`，因为该接口会漏掉隐藏 / 加密直播间（返回空列表），而 `get_info` 对这些房间仍然可查。
- **昵称**不在 `get_info` 的返回里，订阅时通过 `live_user/v1/Master/info` 补一次。
- **直播画面**使用官方为该直播间生成的 `keyframe` 实时截图，直接下载后作为 base64 图片发送；取不到时退回封面图。没有在设备上跑 ffmpeg 拉流截图，避免 ARM 设备上的额外负载和拉流失败。
- **下播判定**：`live_status=1` 视为在播，`0`（未开播）和 `2`（轮播）都视为不在播。订阅时如果主播正在直播会记录开播时间，保证下播消息里的时长完整。
- **Cookie 可选**：配置后所有请求（含图片下载）都会带上 Cookie 请求头；不配置时走游客身份，功能不受影响。`SESSDATA` 的值会被自动补全为完整的 Cookie 键值对。
- 订阅数据存放在插件 KV 存储（`data/plugin_data` 下），重装或更新插件不会丢失。

## 配置

`_conf_schema.json` 提供四项配置，可在 WebUI 插件配置页修改：

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `poll_interval` | 60 | 轮询间隔（秒），最小 30 |
| `max_search_results` | 5 | 搜索结果条数上限，1–10 |
| `send_screenshot` | true | 开播提醒是否附带直播画面 |
| `cookie` | 空 | B 站 Cookie，可选 |

### 关于 Cookie

**不填也能用**，插件的全部功能在游客身份下都可用，实测搜索、查状态、下载截图均正常。

填 Cookie 的目的是抗风控：未登录访问 B 站接口更容易撞上 `-352` / `-412` 限流，尤其在轮询频率较高时。填入登录态 Cookie 可以降低这个概率。

获取方式：浏览器登录 B 站 → F12 → Application → Cookies → `https://www.bilibili.com` → 复制 `SESSDATA` 的值。配置项只粘贴 `SESSDATA` 的值也可以，插件会自动补上键名；粘贴完整 Cookie 字符串同样支持。

Cookie 会以密码框形式展示（`secret: true`），但请注意它只是界面遮罩，**不会加密存储**在配置文件里，请勿把配置文件公开分享。

## 使用示例

```
你：帮我搜一下 B 站的老番茄
Bot：（调用工具）老番茄（UID 546195）粉丝数 20860070（2086.0 万），直播间号 21852（未开播）……
你：订阅他的直播间
Bot：（调用工具）订阅成功：已订阅主播 老番茄 的直播间 21852……
```

## 安装

把整个目录放到 AstrBot 的 `data/plugins/` 下，然后在 WebUI 插件页重载插件（或重启 AstrBot）。

依赖只有 `aiohttp`，AstrBot 本身已自带，一般无需额外安装。

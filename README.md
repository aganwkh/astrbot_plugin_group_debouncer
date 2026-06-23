# AstrBot 群聊连续消息防抖插件

这是一个发送者级碎片消息合并器：只合并同一群内、同一发送者在短时间内连续发送的文本。它不决定机器人是否回复；启用 Heartflow 时，Heartflow 只会接收到最终合并并放行的事件。

## 安装

将本目录放到 AstrBot 插件目录（例如 `/opt/AstrBot/data/plugins/`），重启 AstrBot 或通过插件管理页面重新加载。

## 关键配置

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `window_seconds` | `3.0` | 普通消息的防抖窗口。 |
| `direct_trigger_window_seconds` | `1.5` | 明确 `@Bot` 或命中别名时的窗口。 |
| `reset_timer` | `true` | 新消息从该消息时刻重新计算窗口。 |
| `max_messages` | `5` | 达到条数后立即合并放行。 |
| `inject_strategy` | `preserve_last_non_plain` | 合并文本时保留最后一条消息中的图片、@、表情等组件。 |
| `strict_at_match` | `true` | 只把明确指向 Bot 的 At 视为 `@Bot`。 |
| `heartflow_compat_mode` | `true` | 与 Heartflow 共用时禁用本插件复读和冷场首条直通，并强制严格 @ 匹配和保留最后非文本组件。 |
| `cleanup_interval_seconds` | `300` | 状态清理检查间隔。 |
| `inactive_state_ttl_seconds` | `1800` | 空闲状态回收时间。 |
| `debounce_enabled_groups` | `""` | 防抖白名单；为空表示所有群。 |
| `debounce_disabled_groups` | `""` | 防抖黑名单；优先于白名单。 |

完整配置以 [_conf_schema.json](_conf_schema.json) 为准。复读配置仍可使用；但若同时使用 Heartflow，推荐保持 `heartflow_compat_mode=true`，由 Heartflow 负责主动发言。

## 与 Heartflow 共存

本插件在 `GROUP_MESSAGE` 阶段使用 priority `2000`；Heartflow 常用 priority 为 `1000`。因此，先由本插件合并同一人的碎片消息，旧事件会停止传播，随后 Heartflow 只判断最终事件。插件不会读取或修改 Heartflow 的内部状态。

## 命令与组件

以 `/`、`!`、`！`、`#`、`＃` 开头的消息不防抖。默认的组件保留策略是 `preserve_last_non_plain`；如果需要旧行为可设置 `inject_strategy=plain_replace`，如果需要保留窗口内全部非文本组件可设置 `preserve_all_non_plain`。

## 验收

部署前请执行自动化测试，并按 [docs/testing.md](docs/testing.md) 完成至少一次群内手工验收。

## 版本

- 当前版本：`2.4.0`
- 兼容 AstrBot：`>=4.16`

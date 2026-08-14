# 邻舍桥接插件适配器

将 MaiBot 与邻舍.EXE 连接起来，让 MaiBot 可以接入一个持续生活着的 AI 角色世界。

## 邻舍.EXE 是什么

邻舍.EXE 是一款受 Galgame 启发的 AI 角色陪伴应用。用户可以创建具有独立人格、情绪、记忆和生活节奏的角色，与她们聊天，并在朋友圈、信箱、日程、群聊和随机事件中持续互动。角色还能够根据对话和当前场景主动调用 ComfyUI 生成图片，让文字交流自然延伸成视觉体验。

它想解决的问题很简单：普通 AI 聊天机器人往往只是在“回答问题”，而邻舍.EXE 希望让角色表现得更像一个持续生活着的人。

邻舍.EXE 将单聊、群聊、朋友圈、日程、奇遇、相册和角色管理整合在同一个界面中。用户可以像使用日常社交软件一样与不同角色保持联系，同时进入由 AI 驱动的角色世界。

> 展示视频：【【邻舍 2.0】😈既然是在本地AI生成，那凑成什么CP可就随我说了算了】[https://www.bilibili.com/video/BV1wsNu61EX6/](https://www.bilibili.com/video/BV1wsNu61EX6/)

## 插件功能

- **人格注入**：使用邻舍角色的 `base_prompt` 替换 MaiBot 请求中的 system 人格，并注入行为风格与表达风格；Planner 请求中的默认行为风格同样会被邻舍行为风格覆盖，MaiBot 自带表达习惯不再保留，请求中的机器人昵称也会替换为邻舍角色的 `display_name`。
- **记忆同步**：将 MaiBot 的对话回复交给邻舍处理，保存长期记忆和滚动摘要。
- **按需配图**：由邻舍判断是否需要配图，异步生成后由 MaiBot 发送图片消息。
- **人格管理**：插件设置页展示当前激活角色的 `display_name`，并提供邻舍管理页入口，调整角色、人格、记忆和配图参数。

## 依赖

- MaiBot，且已启用插件运行时：`[plugin_runtime] enabled = true`
- 已启动邻舍服务，并提供 MaiBot 桥接接口：
  - `GET /api/maibot/characters`
  - `POST /api/maibot/chat`
  - `POST /api/maibot/derive-style`
  - `GET /api/maibot/tasks/:id`
  - `GET/PUT /api/maibot/plugin-persona`

## 安装

将本仓库复制到 MaiBot 的 `plugins/linshe_bridge` 目录，或通过 MaiBot WebUI 的插件市场安装。安装后重启 MaiBot，或者重新加载插件。

插件设置页会展示当前激活角色的 `display_name`，并提供邻舍管理页入口；详细参数请到管理页调整。

## 配置

插件运行时会自动生成 `config.toml`（该文件不随仓库分发）。常用参数如下：

| 配置项 | 说明 |
| --- | --- |
| `plugin.enabled` | 是否启用插件 |
| `bridge.base_url` | 邻舍服务地址，默认 `http://127.0.0.1:3099` |
| `bridge.character_name` | 邻舍角色显示名（`display_name`，兼容 `characters.name`）；留空则跳过全部流程 |
| `memory.memory_curation` | 是否启用对话记忆摘要 |
| `image.image_mode` | `auto` 由邻舍判断、`off` 关闭、`always` 总是配图 |
| `image.context_max_messages` | 传给邻舍判断/生图的上下文消息条数，默认 2（含用户和 Agent） |
| `image.poll_interval_sec` | 生图任务轮询间隔，单位为秒 |
| `image.poll_timeout_sec` | 生图任务轮询超时，单位为秒 |

保存人格信息后，插件会将数据写入 MaiBot SDK 分配的插件数据目录：

```text
data/plugins/github.icecranberry.linshe-bridge/persona_store.json
```

## 工作流程

```text
用户消息
  -> MaiBot 构造模型请求
  -> 插件注入邻舍角色人格与风格
  -> 模型生成回复
  -> 插件异步同步记忆并判断是否需要配图
  -> 需要配图时轮询任务并由 MaiBot 发出图片
```

## 目录结构

```text
_manifest.json   插件市场清单
plugin.py        插件实现
config.toml      首次加载自动生成，不入库
assets/icon.png  插件图标
CHANGELOG.md     版本变更记录
ARCHITECTURE.md  详细架构说明
```

更多接口、数据存储和调试说明见 [ARCHITECTURE.md](./ARCHITECTURE.md)。

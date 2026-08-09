# 更新日志

## 1.2.2

- 修复：首页人格管理卡片链接改为跟随 `bridge.base_url`，不再硬编码 `127.0.0.1:3099`。
- 修复：图片下载兼容绝对 URL，邻舍返回完整地址时不再错误拼接 `base_url`。
- 变更：`host_application.max_version` 更新至 `1.1.4`。

## 1.2.1

- 变更：人格数据目录固定使用 MaiBot 插件默认目录，管理页与插件运行时不再存在路径分叉。
- 变更：移除邻舍管理页中的“插件数据目录”配置及 `maibot_plugin_data_dir` 设置读写。
- 变更：人格数据改由插件公开 API 读写，并写入 SDK 分配的标准持久化目录；邻舍侧不再直接访问 MaiBot 文件系统。
- 变更：移除尚未发布版本中的旧人格数据迁移兼容逻辑。

## 1.2.0

- 变更：行为/表达风格与 base_prompt 本地副本改为按角色保存在插件数据目录 persona_store.json（不再写入邻舍数据库）。
- 变更：移除全局覆盖字段（persona_base_prompt_override / behavior_style_override / reply_style_override），覆盖入口统一为插件本地数据（人格管理页或直接编辑数据文件）。
- 新增：首次注入时保存邻舍 base_prompt 到本地副本，用户可修改后固定人格原文；行为/表达风格提炼后自动保存，已保存时不再重复提炼。
- 变更：移除过时的 style_cache_ttl_sec / style_refresh_tick 配置（风格改为按角色持久化到本地数据，解析时直接读取，无需缓存时长与刷新标记）。

## 1.1.0

- 新增：人格原文覆盖字段 persona_base_prompt_override，留空使用邻舍角色 base_prompt，填写后整条替换 system 人格。
- 新增：行为/表达风格覆盖字段 behavior_style_override、reply_style_override，留空使用邻舍提炼结果，填写后注入时优先使用覆盖值。
- 新增：提炼出的行为/表达风格按角色保存到邻舍侧（人格管理页可见可覆盖），插件本地保留离线缓存，日后可在配置或管理页覆盖。

## 1.0.0

- 新增：人格注入，将 system 人格整条替换为邻舍角色的 base_prompt。
- 新增：调用邻舍 derive-style 提炼行为风格与表达风格并注入模型请求。
- 新增：回复生成后将本轮对话写入邻舍记忆库（不动情绪/好感度）。
- 新增：由邻舍判断是否配图，异步轮询生图任务并发送图片。

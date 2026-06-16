<div align="center">

[![Moe Counter](https://count.getloli.com/get/@lirundong093-glitch?theme=moebooru)](https://github.com/lirundong093-glitch/astrbot_plugin_sensitive_guard)

</div>

# 🛡️ 敏感词自动禁言插件 (astrbot_plugin_sensitive_guard)

基于 AstrBot 框架开发的群消息敏感词检测与自动禁言插件。使用 AC 自动机 + 拼音匹配实现高效检测，支持渐进式处罚策略，词库来源于开源敏感词仓库。

<p align="center">
  <img src="https://img.shields.io/badge/version-v1.0.0-blue" alt="Version">
  <img src="https://img.shields.io/badge/Python-3.8%2B-blue" alt="Python">
  <img src="https://img.shields.io/badge/AstrBot-%E6%8F%92%E4%BB%B6%E6%A1%86%E6%9E%B6-brightgreen" alt="AstrBot">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="License">
</p>

## ✨ 主要特性

- **17 类词库可选**：从开源仓库拉取，可自由组合所需词库类别，默认启用反动词库、政治类型、暴恐词库、贪腐词库、涉枪涉爆五类。
- **AC 自动机高效匹配**：基于 pyahocorasick 一次遍历检出所有命中，支持干扰符号过滤（防「加 微 信」类绕过）。
- **拼音同音检测**：检测拼音相同的变体写法，防止谐音绕过。
- **jieba 分词边界校验**：过滤跨越分词边界的误命中（如「反动」出现在「自动反」中）。
- **安全上下文过滤**：基于 THUOCL 清华大学开放中文词库，放行日常生活语境中的命中（如「腐败的食物」）。
- **渐进式处罚**：按群 + 用户 + 天累计，第 1 次禁言 60s → 第 2 次禁言 600s → 第 3 次踢出，每天 00:00 重置。
- **LLM 输出过滤**：可选过滤 LLM 回复中的敏感词，自动替换为等长 `*`。
- **自动撤回**：可选开启，检测到违规消息后自动撤回（仅 QQ 平台）。
- **繁简自动转换**：内置 OpenCC，繁体消息自动转简体后检测。
- **群组/用户白名单**：可指定仅监控特定群聊，排除特定用户。
- **独立检测日志**：所有命中记录写入 `detection.log`，便于事后追溯。

## 📥 安装与配置

1. 将插件放入 AstrBot 的 `data/plugins` 目录。
2. 安装依赖：`pip install -r requirements.txt`（pyahocorasick、pypinyin、jieba）。
3. 重启 AstrBot，在 WebUI 插件配置页面调整参数。

首次启动时会自动从 GitHub 下载词库和安全上下文词表，可能需要几秒时间。

## ⚙️ 配置项

| 配置项 | 类型 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- |
| `enabled_groups` | list | `[]` | 监控群号列表，留空则监控所有群 |
| `whitelist_users` | list | `[]` | 用户白名单，这些用户的消息完全跳过检测 |
| `word_banks` | string | `"1,2,3,4,5"` | 词库选择，用逗号分隔数字（见下方词库表） |
| `max_warn_count` | int | `3` | 第 N 次触发时踢出群聊（2次以上的禁言统一使用二次禁言的秒数） |
| `first_ban_seconds` | int | `60` | 首次触发的禁言秒数 |
| `second_ban_seconds` | int | `600` | 第二次触发的禁言秒数 |
| `filter_llm_output` | bool | `true` | 是否过滤 LLM 输出中的敏感词 |
| `auto_revoke` | bool | `false` | 是否自动撤回违规消息（默认不启动，QQ撤回可能引发额外审核） |

## 📋 词库编号表

| 编号 | 词库 | 编号 | 词库 |
| :---: | :--- | :---: | :--- |
| 1 | 反动词库 | 10 | 新思想启蒙 |
| 2 | 政治类型 | 11 | 民生词库 |
| 3 | 暴恐词库 | 12 | 网易前端过滤 |
| 4 | 贪腐词库 | 13 | 色情类型 |
| 5 | 涉枪涉爆 | 14 | 色情词库 |
| 6 | COVID-19 | 15 | 补充词库 |
| 7 | GFW 补充 | 16 | 零时-Tencent |
| 8 | 其他词库 | 17 | 非法网址 |
| 9 | 广告类型 | | |

> 所有词库均来自 [konsheng/Sensitive-lexicon](https://github.com/konsheng/Sensitive-lexicon) 仓库的 `Vocabulary` 目录。配置如 `"1,2,14"` 即仅启用反动 + 政治 + 色情词库。

## 🔍 检测流水线

```
消息 → 繁转简 → 去干扰符号 → AC 自动机全匹配
    → jieba 分词边界校验（跨词误杀过滤）
    → 拼音同音检测（谐音绕过检测）
    → 安全上下文过滤（日常生活语境放行）
    → 命中处置
```

## 📐 处罚策略

| 第 N 次触发 | 处罚 | 说明 |
| :---: | :--- | :--- |
| 1 | 禁言 `first_ban_seconds` 秒 | 默认 60 秒 |
| 2 | 禁言 `second_ban_seconds` 秒 | 默认 600 秒（10 分钟） |
| 3+ | 踢出群聊 | 触发次数由 `max_warn_count` 控制 |

计数器按 **群号 + 用户 QQ + 当日日期（北京时间）** 维度累计，每天 00:00 自动清零。

## 🛠️ 开发与依赖

- **框架**：AstrBot API
- **词库来源**：[konsheng/Sensitive-lexicon](https://github.com/konsheng/Sensitive-lexicon)
- **安全词表**：[THUOCL 清华大学开放中文词库](https://github.com/thunlp/THUOCL)
- **AC 自动机**：pyahocorasick
- **繁简转换**：OpenCC (opencc-python-reimplemented)
- **分词**：jieba
- **拼音**：pypinyin

## 📝 许可证

[MIT License](LICENSE)

## 🙏 鸣谢

- [konsheng/Sensitive-lexicon](https://github.com/konsheng/Sensitive-lexicon) 提供开源敏感词库。
- [THUOCL](https://github.com/thunlp/THUOCL) 提供安全上下文词表，有效降低误杀率。

---

<p align="center">Made with ❤️ by <a href="https://github.com/lirundong093-glitch">Lucy</a></p>

"""
sensitive_guard — 群消息敏感词检测 + 渐进式禁言插件

监听规则：仅检测「反动词库 / 政治类型 / 暴恐词库 / 贪腐词库 / 涉枪涉爆」五类。
处罚策略（按群 + 用户 + 天累计）：
  第 1 次 → 禁言 60 秒
  第 2 次 → 禁言 600 秒
  第 3 次 → 踢出群聊
  每天 00:00 重置计数（北京时间）
"""

import asyncio
import json
import os
import re
from datetime import datetime, timezone, timedelta
from urllib.request import urlopen, Request
from urllib.parse import quote

import ahocorasick
from pypinyin import lazy_pinyin
import jieba

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.event.filter import EventMessageType
from astrbot.api.provider import LLMResponse
from astrbot.api.star import Context, Star

# ── 常量 ──────────────────────────────────────────────
CDN_BASE = (
    "https://edgeone.gh-proxy.org/https:/raw.githubusercontent.com"
    "/konsheng/Sensitive-lexicon/master/Vocabulary"
)

CATEGORY_FILES: dict[str, str] = {
    "subversive":   "反动词库.txt",
    "political":    "政治类型.txt",
    "violence":     "暴恐词库.txt",
    "corruption":   "贪腐词库.txt",
    "weapons":      "涉枪涉爆.txt",
    "covid19":      "COVID-19词库.txt",
    "gfw_supp":     "GFW补充词库.txt",
    "misc":         "其他词库.txt",
    "advertising":  "广告类型.txt",
    "new_thought":  "新思想启蒙.txt",
    "livelihood":   "民生词库.txt",
    "netease":      "网易前端过滤敏感词库.txt",
    "porn_type":    "色情类型.txt",
    "porn_words":   "色情词库.txt",
    "supplement":   "补充词库.txt",
    "zerohour":     "零时-Tencent.txt",
    "illegal_url":  "非法网址.txt",
}

WORD_BANK_MAP: dict[str, str] = {
    "1":  "subversive",
    "2":  "political",
    "3":  "violence",
    "4":  "corruption",
    "5":  "weapons",
    "6":  "covid19",
    "7":  "gfw_supp",
    "8":  "misc",
    "9":  "advertising",
    "10": "new_thought",
    "11": "livelihood",
    "12": "netease",
    "13": "porn_type",
    "14": "porn_words",
    "15": "supplement",
    "16": "zerohour",
    "17": "illegal_url",
}

CST = timezone(timedelta(hours=8))

# ── 安全上下文词表 (THUOCL 清华大学开放中文词库) ────
# 用于判断敏感词是否出现在日常生活语境中（如"腐败的食物"），避免误杀
THUOCL_RAW_BASE = "https://raw.githubusercontent.com/thunlp/THUOCL/master/data"
THUOCL_MIRROR_BASE = (
    "https://edgeone.gh-proxy.org/https:/raw.githubusercontent.com"
    "/thunlp/THUOCL/master/data"
)

SAFE_WORD_FILES = {
    "food":    "THUOCL_food.txt",     # 饮食词库 (8,974 词)
    "animal":  "THUOCL_animal.txt",   # 动物词库 (17,287 词)
    "medical": "THUOCL_medical.txt",  # 医学词库 (18,749 词)
}

# 上下文窗口大小（字符数），敏感词前后各探测此范围
SAFE_CONTEXT_WINDOW = 2


class SensitiveGuard(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        if config is None:
            config = {}

        # ── 状态字段 ──
        self.words: dict[str, set[str]] = {}          # cat_id → {词, ...}
        self._safe_words: set[str] = set()            # 安全上下文词集合
        self._ready = False
        self._records: dict[str, dict] = {}
        self._t2s = None                               # OpenCC 实例，异步初始化

        # ── 数据目录 ──
        self._data_dir = os.path.join("data", "plugin_data", "astrbot_plugin_sensitive_guard")
        os.makedirs(self._data_dir, exist_ok=True)
        self._records_path = os.path.join(self._data_dir, "records.json")
        self._detection_log_path = os.path.join(self._data_dir, "detection.log")

        # ── 配置读取 ──
        self._enabled_groups: set[str]  = set(config.get("enabled_groups", []))
        self._whitelist_users: set[str] = set(config.get("whitelist_users", []))
        self._max_warn  = int(config.get("max_warn_count", 3))
        self._filter_llm = config.get("filter_llm_output", True)
        self._auto_revoke = config.get("auto_revoke", False)
        self._ban1 = int(config.get("first_ban_seconds", 60))
        self._ban2 = int(config.get("second_ban_seconds", 600))

        # ── 词库选择：解析 "1,2,3,4,5" 格式 → 过滤 CATEGORY_FILES ──
        raw_banks = config.get("word_banks", "1,2,3,4,5")
        selected_keys: set[str] = set()
        for part in raw_banks.split(","):
            part = part.strip()
            if part in WORD_BANK_MAP:
                selected_keys.add(WORD_BANK_MAP[part])
        # 若未选中任何有效词库，回退至全部启用
        if not selected_keys:
            selected_keys = set(CATEGORY_FILES.keys())
        self._selected_categories: dict[str, str] = {
            k: v for k, v in CATEGORY_FILES.items() if k in selected_keys
        }
        logger.info(
            f"[sensitive_guard] 已选择词库: {list(self._selected_categories.keys())}"
        )

        # ── 处罚阶梯 — 根据 max_warn_count 动态构建 ──
        #  第 1 次: ban1  第 2..(N-1) 次: ban2  第 N 次: 踢出
        self.penalty_steps: list[tuple[int | None, str]] = []
        if self._max_warn >= 2:
            self.penalty_steps.append(
                (self._ban1, f"禁言 {self._ban1} 秒")
            )
        for _ in range(max(0, self._max_warn - 2)):
            self.penalty_steps.append(
                (self._ban2, f"禁言 {self._ban2} 秒")
            )
        self.penalty_steps.append((None, "踢出群聊"))

        self._load_records()
        self._init_task = asyncio.ensure_future(self._init_async())

    # ══════════════════════════════════════════════
    #  初始化
    # ══════════════════════════════════════════════

    async def _init_async(self) -> None:
        """异步加载词库、初始化 OpenCC、构建 AC 自动机。"""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._load_words_sync)

        from opencc import OpenCC
        self._t2s = await loop.run_in_executor(None, lambda: OpenCC("t2s"))

        self._build_ac()
        await loop.run_in_executor(None, self._load_safe_words)
        self._ready = True
        logger.info(
            f"[sensitive_guard] 安全上下文词表就绪: {len(self._safe_words)} 词"
        )

    # ── 词库下载（同步 I/O，跑在 executor 里）──

    def _download_file(self, filename: str) -> str | None:
        """从 CDN 下载单个词库文件，失败返回 None。"""
        url = f"{CDN_BASE}/{quote(filename)}"
        req = Request(url, headers={"User-Agent": "AstrBot/sensitive-guard"})
        try:
            with urlopen(req, timeout=15) as resp:
                return resp.read().decode("utf-8")
        except Exception as e:
            logger.warning(f"[sensitive_guard] 下载失败 {filename}: {e}")
            return None

    def _load_words_sync(self) -> None:
        """下载选中的词库并存入 self.words。"""
        logger.info("[sensitive_guard] 开始下载词库...")
        for cat_id, filename in self._selected_categories.items():
            text = self._download_file(filename)
            if text is None:
                continue
            lines = [line.strip() for line in text.split("\n") if line.strip()]
            self.words[cat_id] = set(lines)
            logger.info(f"[sensitive_guard] {filename}: {len(lines)} 词")
        total = sum(len(v) for v in self.words.values())
        logger.info(f"[sensitive_guard] 词库下载完毕，共 {total} 词 / {len(self.words)} 类")

    # ── 安全上下文词表下载 ──

    def _download_raw_file(self, url: str) -> str | None:
        """下载原始文本文件（无 CDN 代理）。"""
        req = Request(url, headers={"User-Agent": "AstrBot/sensitive-guard"})
        try:
            with urlopen(req, timeout=15) as resp:
                return resp.read().decode("utf-8")
        except Exception as e:
            logger.warning(f"[sensitive_guard] 安全词表下载失败 {url}: {e}")
            return None

    def _load_safe_words(self) -> None:
        """下载 THUOCL 饮食 / 动物 / 医学词表，合并为安全上下文词集合。

        优先走 gh-proxy 镜像，失败时回退直连。
        """
        logger.info("[sensitive_guard] 下载安全上下文词表 (THUOCL)...")
        for filename in SAFE_WORD_FILES.values():
            text = self._download_raw_file(f"{THUOCL_MIRROR_BASE}/{filename}")
            if text is None:
                logger.info(
                    f"[sensitive_guard] 镜像下载失败，回退直连 {filename}"
                )
                text = self._download_raw_file(f"{THUOCL_RAW_BASE}/{filename}")
            if text is None:
                continue
            count = 0
            for line in text.split("\n"):
                line = line.strip()
                if not line:
                    continue
                # THUOCL 格式: 词 \t 词频
                word = line.split("\t")[0]
                if word and len(word) >= 2:
                    self._safe_words.add(word)
                    count += 1
            logger.info(f"[sensitive_guard] {filename}: 加载 {count} 个安全词")

    # ══════════════════════════════════════════════
    #  持久化
    # ══════════════════════════════════════════════

    def _load_records(self) -> None:
        try:
            with open(self._records_path, "r", encoding="utf-8") as f:
                self._records = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            self._records = {}

    def _save_records(self) -> None:
        with open(self._records_path, "w", encoding="utf-8") as f:
            json.dump(self._records, f, ensure_ascii=False)

    def _today_key(self) -> str:
        return datetime.now(CST).strftime("%Y%m%d")

    def _increment_count(self, group_id: str, user_id: str) -> int:
        """递增 (群, 用户, 天) 计数并返回新值。"""
        today = self._today_key()
        day_data = self._records.setdefault(today, {})
        grp_data = day_data.setdefault(group_id, {})
        grp_data[user_id] = grp_data.get(user_id, 0) + 1
        self._save_records()
        return grp_data[user_id]

    def _write_detection_log(
        self,
        group_id: str,
        user_id: str,
        user_name: str,
        count: int,
        penalty_desc: str,
        hits: list[tuple[str, str]],
        raw_text: str,
    ) -> None:
        """写入独立检测日志到文件。"""
        timestamp = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
        hit_summary = "; ".join(f"{w}[{c}]" for w, c in hits)
        raw_snippet = raw_text[:200] + "..." if len(raw_text) > 200 else raw_text
        line = (
            f"[{timestamp}] 群={group_id} 用户={user_id}({user_name}) "
            f"第{count}次 → {penalty_desc} | "
            f"命中({len(hits)}): {hit_summary} | "
            f"原文: {raw_snippet}"
        )
        try:
            with open(self._detection_log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as e:
            logger.error(f"[sensitive_guard] 写入检测日志失败: {e}")

    # ══════════════════════════════════════════════
    #  AC 自动机（构建 + 匹配）
    # ══════════════════════════════════════════════

    # 干扰符号正则（防「加 微 信」「最.好.的」绕过）
    _INTERFERENCE_RE = re.compile(
        r"[\s\.\-_\*\#\~\@\$\%\^\&\(\)\[\]\{\}\|\\\/\:\;\"\'\“\”\!\?\<\>\《\》\（\）\【\】\…\—\、\，\。\；\：\！\？]"
    )

    def _strip_interference(self, text: str) -> str:
        return self._INTERFERENCE_RE.sub("", text)

    def _build_ac(self) -> None:
        """用 pyahocorasick 为每个类别构建 AC 自动机 + 拼音索引。"""
        self._ac: dict[str, ahocorasick.Automaton] = {}
        self._py_index: dict[tuple[int, str], list[tuple[str, str]]] = {}

        for cat_id, word_set in self.words.items():
            automaton = ahocorasick.Automaton()
            for word in word_set:
                # 保存 (word, cat_id) 以便匹配时回溯
                automaton.add_word(word, (word, cat_id))

                # 拼音索引：仅对长度 ≥2 的词建立
                if len(word) >= 2:
                    key = (len(word), "".join(lazy_pinyin(word)))
                    self._py_index.setdefault(key, []).append((word, cat_id))

            automaton.make_automaton()
            self._ac[cat_id] = automaton
            logger.info(
                f"[sensitive_guard] AC 构建完成: {cat_id} → {len(word_set)} 词"
            )

    def _match_positions(self, text: str) -> list[tuple[int, int, str, str]]:
        """AC 遍历：返回所有命中位置 (start, end, word, cat_id)，不去重。"""
        matches: list[tuple[int, int, str, str]] = []
        for automaton in self._ac.values():
            for end, (word, cat_id) in automaton.iter(text):
                matches.append((end - len(word), end, word, cat_id))
        return matches

    def _match_ac(self, text: str) -> list[tuple[str, str]]:
        """同 _match_positions 但每个结束位置只保留最长匹配。"""
        end_best: dict[int, tuple[str, str]] = {}
        for _start, end, word, cat_id in self._match_positions(text):
            if end not in end_best or len(word) > len(end_best[end][0]):
                end_best[end] = (word, cat_id)
        return list(end_best.values())

    # ══════════════════════════════════════════════
    #  检测逻辑
    # ══════════════════════════════════════════════

    def _filter_by_jieba_boundary(
        self, text: str, hits: list[tuple[str, str]]
    ) -> list[tuple[str, str]]:
        """过滤掉跨越 jieba 分词边界的 AC 命中。"""
        seg = list(jieba.cut(text))
        # 构建分词偏移表
        seg_spans: list[tuple[int, int]] = []
        pos = 0
        for sw in seg:
            seg_spans.append((pos, pos + len(sw)))
            pos += len(sw)

        valid: list[tuple[str, str]] = []
        for word, cat_id in hits:
            idx = text.find(word)
            if idx == -1:
                # 仅存在于去干扰后的文本中，保留
                valid.append((word, cat_id))
                continue
            end = idx + len(word)
            # 检查是否完全落在某个 jieba 词的边界内
            if any(ss <= idx and end <= se for ss, se in seg_spans):
                valid.append((word, cat_id))
            else:
                logger.info(
                    f"[sensitive_guard] AC 命中「{word}」但跨越 jieba 分词边界，已过滤"
                )
        return valid

    def _filter_by_context(
        self, text: str, hits: list[tuple[str, str]]
    ) -> list[tuple[str, str]]:
        """过滤处于日常生活语境中的命中（如「腐败的食物」）。
        对每个命中词，取前后 SAFE_CONTEXT_WINDOW 个 jieba 词，
        若任一词在安全词表中则放行。
        """
        if not self._safe_words or not hits:
            return hits

        seg = list(jieba.cut(text))
        filtered: list[tuple[str, str]] = []
        for word, cat_id in hits:
            idx = text.find(word)
            if idx == -1:
                filtered.append((word, cat_id))
                continue

            # 找出敏感词落在哪些 jieba 词上
            surrounding: list[str] = []
            pos = 0
            for sw in seg:
                sw_end = pos + len(sw)
                if (
                    idx <= pos < idx + len(word)
                    or idx < sw_end <= idx + len(word)
                    or (pos >= idx and sw_end <= idx + len(word))
                ):
                    # 该 jieba 词与敏感词有交集 → 收集前后窗口
                    # 取该词在 seg 中的索引，收集前 + 后 SAFE_CONTEXT_WINDOW 个非空词
                    seg_idx = seg.index(sw) if sw in seg else -1
                    for offset in range(-SAFE_CONTEXT_WINDOW, SAFE_CONTEXT_WINDOW + 1):
                        ni = seg_idx + offset
                        if 0 <= ni < len(seg) and offset != 0:
                            nw = seg[ni].strip()
                            if nw and len(nw) >= 2:
                                surrounding.append(nw)
                    break  # 找到第一个交集词就够了
                pos = sw_end

            # 检查周围词是否在安全词表中
            safe_hits = [s for s in surrounding if s in self._safe_words]
            if safe_hits:
                logger.info(
                    f"[sensitive_guard] 因上下文安全放行「{word}」({cat_id})"
                    f" — 周围安全词: {safe_hits[:3]}"
                )
                continue  # 安全语境，不放行

            filtered.append((word, cat_id))
        return filtered

    def _match_pinyin(
        self, text: str, seg: list[str], seen: set[str]
    ) -> list[tuple[str, str]]:
        """拼音同音检测：滑动窗口 + 预建索引 O(1) 查找 + jieba 语境过滤。"""
        hits: list[tuple[str, str]] = []
        n_text = len(text)
        if n_text < 2:
            return hits

        # 构建 jieba 分词边界（与 _filter_by_jieba_boundary 一致）
        seg_spans: list[tuple[int, int]] = []
        pos = 0
        for sw in seg:
            seg_spans.append((pos, pos + len(sw)))
            pos += len(sw)

        for n in range(2, min(n_text, 10) + 1):
            for i in range(n_text - n + 1):
                window = text[i : i + n]
                key = (n, "".join(lazy_pinyin(window)))
                for word, cat_id in self._py_index.get(key, []):
                    if word in seen:
                        continue
                    if not set(word) & set(window):
                        continue
                    # 窗口是 jieba 正常词则放行（不视为敏感词）
                    if window in seg:
                        continue
                    # 窗口跨越 jieba 分词边界则放行（人为谐音不会跨词）
                    if not any(ss <= i and i + n <= se for ss, se in seg_spans):
                        continue
                    hits.append((word, cat_id))
                    seen.add(word)
        return hits

    def _check(self, text: str) -> list[tuple[str, str]]:
        """核心检测管道：繁→简 → 去干扰 → AC 匹配 → jieba 校验 → 拼音匹配。"""
        simplified = self._t2s.convert(text)
        cleaned = self._strip_interference(simplified)
        hits = self._match_ac(cleaned)

        # AC 命中做 jieba 分词边界校验
        hits = self._filter_by_jieba_boundary(simplified, hits)

        # 拼音同音检测
        seg = list(jieba.cut(simplified))
        seen: set[str] = {w for w, _ in hits}
        hits.extend(self._match_pinyin(simplified, seg, seen))

        # 安全上下文过滤：放行日常生活语境中的命中（如"腐败的食物"）
        hits = self._filter_by_context(simplified, hits)

        return hits

    # ══════════════════════════════════════════════
    #  处罚执行
    # ══════════════════════════════════════════════

    async def _apply_penalty(
        self, event: AstrMessageEvent, group_id: str, user_id: str, count: int
    ) -> None:
        step_idx = min(count - 1, len(self.penalty_steps) - 1)
        duration, desc = self.penalty_steps[step_idx]

        client = getattr(event, "bot", None)
        if client is None:
            logger.warning("[sensitive_guard] 无法获取 bot 客户端")
            return

        if duration is not None:
            try:
                await client.api.call_action(
                    "set_group_ban",
                    group_id=int(group_id),
                    user_id=int(user_id),
                    duration=duration,
                )
            except Exception as e:
                logger.error(f"[sensitive_guard] 禁言失败: {e}")
        else:
            try:
                await client.api.call_action(
                    "set_group_kick",
                    group_id=int(group_id),
                    user_id=int(user_id),
                    reject_add_request=False,
                )
            except Exception as e:
                logger.error(f"[sensitive_guard] 踢出失败: {e}")
        logger.info(f"[sensitive_guard] {desc}: user={user_id} group={group_id}")

    # ══════════════════════════════════════════════
    #  事件监听
    # ══════════════════════════════════════════════

    @filter.event_message_type(EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        if not self._ready:
            return

        group_id = event.message_obj.group_id
        if not group_id:
            return
        if self._enabled_groups and str(group_id) not in self._enabled_groups:
            return

        user_id = str(event.message_obj.sender.user_id)
        text = event.message_str
        if not user_id or not text:
            return
        if user_id in self._whitelist_users:
            return

        hits = self._check(text)
        if not hits:
            return

        count = self._increment_count(group_id, user_id)
        step_idx = min(count - 1, len(self.penalty_steps) - 1)
        _, penalty_desc = self.penalty_steps[step_idx]

        details = "; ".join(f"「{w}」({c})" for w, c in hits[:3])
        if len(hits) > 3:
            details += f" 等共 {len(hits)} 条"

        user_name = event.message_obj.sender.nickname or user_id
        self._write_detection_log(
            group_id, user_id, user_name, count, penalty_desc, hits, text
        )

        logger.info(
            f"[sensitive_guard] 群 {group_id} 用户 {user_id}"
            f" 第 {count} 次触发 → {penalty_desc} | {details}"
        )

        # 替换原文，使 LLM 收不到敏感内容
        event.message_str = (
            f"[系统提示] 该用户发送了一条包含敏感词的信息（{penalty_desc}）。"
        )
        await self._apply_penalty(event, group_id, user_id, count)

        # ── 自动撤回违规消息 ──
        if self._auto_revoke:
            try:
                client = getattr(event, "bot", None)
                if client is not None:
                    await client.api.call_action(
                        "delete_msg",
                        message_id=event.message_obj.message_id,
                    )
                    logger.info(
                        f"[sensitive_guard] 已撤回消息 "
                        f"msg_id={event.message_obj.message_id}"
                    )
            except Exception as e:
                logger.warning(f"[sensitive_guard] 撤回消息失败: {e}")

    # ── LLM 输出过滤 ──

    @filter.on_llm_response(priority=0)
    async def on_llm_response(
        self, event: AstrMessageEvent, response: LLMResponse
    ) -> None:
        """LLM 回复中命中敏感词时替换为等长 *。"""
        if not self._filter_llm or not self._ready or not response.completion_text:
            return

        text = response.completion_text
        matches = [(s, e, w) for s, e, w, _ in self._match_positions(text)]
        if not matches:
            return

        # 按起始位置倒序替换，避免索引错位
        matches.sort(key=lambda x: -x[0])
        chars = list(text)
        for start, end, _word in matches:
            for i in range(start, end):
                chars[i] = "*"

        response.completion_text = "".join(chars)
        logger.info(f"[sensitive_guard] LLM 输出过滤完成，命中 {len(matches)} 词")

    # ── 生命周期 ──

    async def terminate(self):
        self._save_records()

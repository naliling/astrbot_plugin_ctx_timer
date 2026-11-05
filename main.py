# -*- coding: utf-8 -*-
"""CTEX伴侣 (astrbot_plugin_ctx_timer)

为每个 Bot 维护一套独立的「定时上下文」规则库：
每条规则 = 一段注入内容 + 开始时间 + 关闭时间。
窗口内自动把内容注入 LLM 上下文，窗口外自动撤下。

作者: 娜莉灵
版本: 1.0.1.7
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register

# 上下文注入块（AstrBot >= 4.5.7）
try:
    from astrbot.core.agent.message import TextPart
except Exception:  # pragma: no cover
    TextPart = None  # type: ignore

# 插件数据目录
try:
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path
except Exception:  # pragma: no cover
    get_astrbot_data_path = None  # type: ignore

PLUGIN_NAME = "astrbot_plugin_ctx_timer"
VERSION = "1.0.1.7"
AUTHOR = "娜莉灵"

DEFAULT_TZ = "UTC+3"
DEFAULT_INJECT = "context"  # context | system
DEFAULT_TICK = 30
DEFAULT_MAX_RULES = 50
DEFAULT_NOTIFY = False
ALLOW_CROSS_BOT = False

DATE_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
MD_RE = re.compile(r"^(\d{1,2})-(\d{1,2})$")
TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")
DT_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})[T ](\d{1,2}):(\d{2})(?::(\d{2}))?$")
NOW_RE = re.compile(r"^now(?:([+-])(\d+)([mhd]))?$", re.IGNORECASE)
TZ_RE = re.compile(r"^(?:UTC|GMT)?\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?$", re.IGNORECASE)

COMMAND_WORDS = {"ctx", "上下文", "定时上下文", "ctx_timer", PLUGIN_NAME}
SUB_WORDS = {
    "help", "帮助", "?", "add", "添加", "新建", "list", "列表", "ls",
    "show", "查看", "info", "del", "delete", "删除", "rm", "kk", "hal",
    "on", "开启", "off", "关闭", "edit", "修改", "mode", "模式",
    "tz", "时区", "timezone", "scope", "范围", "notify", "提醒",
    "test", "测试", "status", "状态", "bots", "机器人",
    "export", "导出", "import", "导入", "cancel", "取消",
}


# --------------------------------------------------------------------------- #
# 时间工具
# --------------------------------------------------------------------------- #
def parse_tz(text: Any):
    """'UTC+8' / '+8' / 'GMT-3' / 'UTC+5:30' -> tzinfo；识别不了返回 None。"""
    if text is None:
        return None
    t = str(text).strip()
    if not t:
        return None
    up = t.upper()
    if up in ("UTC", "GMT", "Z", "UTC+0", "UTC-0", "GMT+0", "GMT-0"):
        return timezone.utc
    m = TZ_RE.match(t)
    if not m:
        return None
    sign = 1 if m.group(1) == "+" else -1
    hh, mm = int(m.group(2)), int(m.group(3) or 0)
    if hh > 14 or mm > 59:
        return None
    return timezone(sign * timedelta(hours=hh, minutes=mm))


def tz_name(tz) -> str:
    """tzinfo -> 'UTC+8' / 'UTC-3' / 'UTC+5:30'"""
    try:
        off = datetime.now(tz).utcoffset() or timedelta(0)
    except Exception:
        return "UTC"
    total = int(off.total_seconds() // 60)
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    if total % 60:
        return f"UTC{sign}{total // 60}:{total % 60:02d}"
    return f"UTC{sign}{total // 60}"


def resolve_tz(text: Any, fallback=None):
    """把用户写的时区解析成 tzinfo：支持 UTC+8 这类偏移，也支持 Asia/Tokyo 这类名字。"""
    tz = parse_tz(text)
    if tz is not None:
        return tz
    raw = str(text or "").strip()
    if raw:
        try:
            return ZoneInfo(raw)
        except Exception:
            pass
    return fallback


def rule_tz(rule: dict, now: datetime):
    """规则自己的时区；没设就跟随全局（now 所在时区）。"""
    raw = str(rule.get("tz") or "").strip()
    if raw:
        tz = resolve_tz(raw, None)
        if tz is not None:
            return tz
    return now.tzinfo


def hhmm_to_min(text: str) -> int:
    """'09:30' -> 570"""
    h, m = str(text).split(":")[:2]
    return int(h) * 60 + int(m)


def min_to_hhmm(minutes: int) -> str:
    minutes %= 1440
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def parse_endpoint(tokens: list[str], i: int, now: datetime):
    """解析一个时间端点。

    支持: 09:00 / 2026-11-05 / 2026-11-05 09:00 / 2026-11-05T09:00
         11-05 / 11-05 09:00 / now / now+30m / now+2h / now+1d
    返回 (datetime, has_date, date_only, next_index)，失败返回 None。
    """
    if i >= len(tokens):
        return None
    tok = tokens[i]

    m = NOW_RE.match(tok)
    if m:
        dt = now.replace(second=0, microsecond=0)
        if m.group(1):
            n = int(m.group(2))
            unit = m.group(3).lower()
            dt = dt + {"m": timedelta(minutes=n), "h": timedelta(hours=n), "d": timedelta(days=n)}[unit]
        return dt, True, False, i + 1

    m = DT_RE.match(tok)
    if m:
        y, mo, d, hh, mi = (int(m.group(k)) for k in range(1, 6))
        try:
            return datetime(y, mo, d, hh, mi, tzinfo=now.tzinfo), True, False, i + 1
        except ValueError:
            return None

    m = DATE_RE.match(tok)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hh, mi = 0, 0
        nxt = i + 1
        date_only = True
        if nxt < len(tokens):
            tm = TIME_RE.match(tokens[nxt])
            if tm:
                hh, mi = int(tm.group(1)), int(tm.group(2))
                nxt += 1
                date_only = False
        try:
            return datetime(y, mo, d, hh, mi, tzinfo=now.tzinfo), True, date_only, nxt
        except ValueError:
            return None

    m = MD_RE.match(tok)
    if m:
        mo, d = int(m.group(1)), int(m.group(2))
        hh, mi = 0, 0
        nxt = i + 1
        date_only = True
        if nxt < len(tokens):
            tm = TIME_RE.match(tokens[nxt])
            if tm:
                hh, mi = int(tm.group(1)), int(tm.group(2))
                nxt += 1
                date_only = False
        try:
            return datetime(now.year, mo, d, hh, mi, tzinfo=now.tzinfo), True, date_only, nxt
        except ValueError:
            return None

    m = TIME_RE.match(tok)
    if m:
        hh, mi = int(m.group(1)), int(m.group(2))
        if hh > 23 or mi > 59:
            return None
        return now.replace(hour=hh, minute=mi, second=0, microsecond=0), False, False, i + 1

    return None


def parse_window(tokens: list[str], now: datetime):
    """解析「开始 + 关闭」两个端点。

    返回 (spec, error, consumed)：
      spec = {"mode": "daily"|"once", "start": str, "end": str}
    """
    p1 = parse_endpoint(tokens, 0, now)
    if not p1:
        return None, "开始时间格式不对", 0
    start, s_date, _s_only, i = p1

    p2 = parse_endpoint(tokens, i, now)
    if not p2:
        return None, "关闭时间格式不对", 0
    end, e_date, e_only, j = p2

    if s_date or e_date:
        # 一次性
        if s_date and not e_date:
            end = end.replace(year=start.year, month=start.month, day=start.day)
            if end <= start:
                end = end + timedelta(days=1)
        elif e_date and not s_date:
            start = start.replace(year=end.year, month=end.month, day=end.day)
            if end <= start:
                start = start - timedelta(days=1)
        if e_only:
            end = end.replace(hour=23, minute=59, second=59)
        if end <= start:
            return None, "关闭时间必须晚于开始时间", 0
        return {
            "mode": "once",
            "start": start.strftime("%Y-%m-%d %H:%M"),
            "end": end.strftime("%Y-%m-%d %H:%M"),
        }, None, j

    sm, em = start.hour * 60 + start.minute, end.hour * 60 + end.minute
    if sm == em:
        return None, "开始与关闭时间不能相同", 0
    return {"mode": "daily", "start": min_to_hhmm(sm), "end": min_to_hhmm(em)}, None, j


def rule_local_now(rule: dict, now: datetime) -> datetime:
    """把「当前时刻」换算到规则自己的时区。"""
    try:
        return now.astimezone(rule_tz(rule, now))
    except Exception:
        return now


def rule_active(rule: dict, now: datetime) -> bool:
    """规则此刻是否应当注入（按规则自己的时区判断）。"""
    if not rule.get("enabled", True):
        return False
    tz = rule_tz(rule, now)
    local = rule_local_now(rule, now)
    if rule.get("mode") == "daily":
        try:
            s = hhmm_to_min(rule["start"])
            e = hhmm_to_min(rule["end"])
        except Exception:
            return False
        cur = local.hour * 60 + local.minute
        return (s <= cur < e) if s <= e else (cur >= s or cur < e)
    try:
        s = datetime.strptime(rule["start"], "%Y-%m-%d %H:%M").replace(tzinfo=tz)
        e = datetime.strptime(rule["end"], "%Y-%m-%d %H:%M").replace(tzinfo=tz)
    except Exception:
        return False
    return s <= local < e


def rule_state(rule: dict, now: datetime) -> str:
    """中文状态：生效中 / 未开始 / 已结束 / 未到时段 / 已停用"""
    if not rule.get("enabled", True):
        return "已停用"
    if rule.get("mode") == "daily":
        return "生效中" if rule_active(rule, now) else "未到时段"
    tz = rule_tz(rule, now)
    local = rule_local_now(rule, now)
    try:
        s = datetime.strptime(rule["start"], "%Y-%m-%d %H:%M").replace(tzinfo=tz)
        e = datetime.strptime(rule["end"], "%Y-%m-%d %H:%M").replace(tzinfo=tz)
    except Exception:
        return "数据异常"
    if local < s:
        return "未开始"
    if local >= e:
        return "已结束"
    return "生效中"


def window_text(rule: dict) -> str:
    if rule.get("mode") == "daily":
        base = f"每日 {rule.get('start')}-{rule.get('end')}"
    else:
        base = f"一次性 {rule.get('start')} ~ {rule.get('end')}"
    tz = str(rule.get("tz") or "").strip()
    if tz:
        base += f"（{tz}）"
    return base


# --------------------------------------------------------------------------- #
# 存储
# --------------------------------------------------------------------------- #
class RuleStore:
    """按 Bot 隔离的规则库，JSON 原子写入。"""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, Any] = {"schema": 1, "bots": {}}
        self.lock = asyncio.Lock()
        self.load()

    def load(self) -> None:
        try:
            if self.path.exists():
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and isinstance(raw.get("bots"), dict):
                    self.data = raw
        except Exception as e:
            logger.error(f"[ctx_timer] 规则库读取失败: {e}")

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(tmp, self.path)
        except Exception as e:
            logger.error(f"[ctx_timer] 规则库写入失败: {e}")

    # -- 查询 ------------------------------------------------------------- #
    def bot_ids(self) -> list[str]:
        bots = self.data.get("bots", {})
        if not isinstance(bots, dict):
            return []
        return [pid for pid, v in bots.items() if isinstance(v, dict) and v.get("rules")]

    def rules(self, pid: str) -> list[dict]:
        """只读：不存在时返回空列表，不会凭空创建 Bot 条目。"""
        bots = self.data.get("bots", {})
        if not isinstance(bots, dict):
            return []
        bot = bots.get(pid)
        if not isinstance(bot, dict):
            return []
        rl = bot.get("rules")
        return rl if isinstance(rl, list) else []

    def get(self, pid: str, rid: int) -> dict | None:
        for r in self.rules(pid):
            if int(r.get("id", 0)) == int(rid):
                return r
        return None

    def next_id(self, pid: str) -> int:
        ids = [int(r.get("id", 0)) for r in self.rules(pid)]
        return (max(ids) + 1) if ids else 1

    # -- 写入 ------------------------------------------------------------- #
    def add(self, pid: str, rule: dict) -> dict:
        rule["id"] = self.next_id(pid)
        bots = self.data.setdefault("bots", {})
        bot = bots.setdefault(pid, {})
        rl = bot.setdefault("rules", [])
        if not isinstance(rl, list):
            rl = bot["rules"] = []
        rl.append(rule)
        self.save()
        return rule

    def delete(self, pid: str, rid: int) -> bool:
        rules = self.rules(pid)
        for idx, r in enumerate(rules):
            if int(r.get("id", 0)) == int(rid):
                rules.pop(idx)
                self.save()
                return True
        return False


# --------------------------------------------------------------------------- #
# 插件
# --------------------------------------------------------------------------- #
def _admin_only():
    """兼容不同 AstrBot 版本的「仅管理员」装饰器。"""
    ptype = getattr(filter, "PermissionType", None)
    deco = getattr(filter, "permission_type", None)
    if ptype is None or deco is None or not hasattr(ptype, "ADMIN"):
        return lambda fn: fn
    return deco(ptype.ADMIN)


@register(PLUGIN_NAME, AUTHOR, "CTEX伴侣：到点自动注入上下文，过点自动撤下，纯指令管理，多 Bot 独立规则库", VERSION)
class CtxTimerPlugin(Star):
    def __init__(self, context: Context, config: Any = None):
        try:
            super().__init__(context, config)
        except TypeError:  # 旧版本 Star.__init__ 只接受 context
            super().__init__(context)
        self.cfg = config if config is not None else getattr(self, "config", None) or {}

        self.store = RuleStore(self._data_dir() / "rules.json")
        self._wizard: dict[str, dict] = {}
        self._last_state: dict[tuple, bool] = {}
        self._tick_task: asyncio.Task | None = None

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # 不在事件循环中（例如同步测试里实例化）
            loop = None
        if loop is not None:
            try:
                self._tick_task = loop.create_task(self._tick_loop())
            except Exception:
                self._tick_task = None

        total = sum(len(v.get("rules", [])) for v in self.store.data.get("bots", {}).values())
        logger.info(f"[ctx_timer] v{VERSION} 已加载，共 {len(self.store.bot_ids())} 个 Bot / {total} 条规则")

    # -- 基础 ------------------------------------------------------------- #
    def _cfg(self, key: str, default: Any) -> Any:
        try:
            val = self.cfg.get(key, default) if hasattr(self.cfg, "get") else default
            return default if val is None else val
        except Exception:
            return default

    def _data_dir(self) -> Path:
        if get_astrbot_data_path is not None:
            try:
                return Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME
            except Exception:
                pass
        return Path("data") / "plugin_data" / PLUGIN_NAME

    def _now(self) -> datetime:
        name = str(self._cfg("timezone", DEFAULT_TZ) or DEFAULT_TZ).strip()
        if name.lower() in ("", "auto", "系统", "跟随系统", "local"):
            return datetime.now().astimezone()
        tz = resolve_tz(name, None)
        if tz is None:
            try:
                tz = ZoneInfo(DEFAULT_TZ)
            except Exception:
                tz = timezone.utc
        return datetime.now(tz)

    def _pid(self, event: AstrMessageEvent) -> str:
        try:
            pid = event.get_platform_id()
            if pid:
                return str(pid)
        except Exception:
            pass
        try:
            return str(event.get_platform_name() or "unknown")
        except Exception:
            return "unknown"

    def _umo(self, event: AstrMessageEvent) -> str:
        try:
            return str(getattr(event, "unified_msg_origin", "") or "")
        except Exception:
            return ""

    def _uid(self, event: AstrMessageEvent) -> str:
        try:
            return str(event.get_sender_id() or "")
        except Exception:
            return ""

    def _gid(self, event: AstrMessageEvent) -> str:
        try:
            return str(event.get_group_id() or "")
        except Exception:
            return ""

    def _strip_mentions(self, text: str) -> str:
        """去掉开头的 @昵称(QQ) 片段，兼容先 @ 人再发指令的写法。"""
        t = (text or "").strip()
        while t.startswith("@"):
            parts = t.split(None, 1)
            if len(parts) < 2:
                return ""
            t = parts[1].strip()
        return t

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        try:
            if event.is_admin():
                return True
        except Exception:
            pass
        try:
            cfg = getattr(self.context, "astrbot_config", None) or {}
            admins = cfg.get("admins_id", []) if hasattr(cfg, "get") else []
            return self._uid(event) in [str(a) for a in admins]
        except Exception:
            return False

    def _looks_like_command(self, text: str) -> bool:
        t = self._strip_mentions(text)
        if not t:
            return False
        low = t.lower()
        for w in COMMAND_WORDS:
            wl = w.lower()
            if low == wl or low.startswith(wl + " "):
                return True
            if t.startswith(w):
                rest = t[len(w):].strip()
                if not rest:
                    return True
                return rest.split()[0].lower() in SUB_WORDS
        return False

    def _scope_match(self, rule: dict, event: AstrMessageEvent) -> bool:
        scope = rule.get("scope") or {"type": "all"}
        if scope.get("type", "all") == "all":
            return True
        gid, uid, umo = self._gid(event), self._uid(event), self._umo(event)
        if gid and gid in scope.get("groups", []):
            return True
        if uid and uid in scope.get("users", []):
            return True
        if umo and umo in scope.get("umos", []):
            return True
        return False

    def _active_rules(self, event: AstrMessageEvent) -> list[dict]:
        pid = self._pid(event)
        now = self._now()
        out = []
        for rule in self.store.rules(pid):
            if rule_active(rule, now) and self._scope_match(rule, event):
                out.append(rule)
        out.sort(key=lambda r: int(r.get("id", 0)))
        return out

    def _render(self, rules: list[dict], now: datetime) -> str:
        lines = ["<scheduled_context>"]
        for r in rules:
            lines.append(f"【规则：{r.get('name', '未命名')} | {window_text(r)}】")
            lines.append(str(r.get("content", "")).strip())
            lines.append("")
        lines.append("</scheduled_context>")
        return "\n".join(lines)

    # -- LLM 请求钩子 ------------------------------------------------------ #
    @filter.on_llm_request()
    async def inject_context(self, event: AstrMessageEvent, req: Any):
        """窗口内把规则内容注入本轮 LLM 请求。"""
        try:
            rules = self._active_rules(event)
            if not rules:
                return
            now = self._now()
            logger.info(f"[ctx_timer] 注入 {len(rules)} 条规则 -> {self._pid(event)}")
            sys_rules = [r for r in rules if r.get("inject") == "system"]
            ctx_rules = [r for r in rules if r.get("inject") != "system"]

            if sys_rules:
                block = self._render(sys_rules, now)
                req.system_prompt = (getattr(req, "system_prompt", "") or "") + "\n\n" + block

            if ctx_rules:
                block = self._render(ctx_rules, now)
                parts = getattr(req, "extra_user_content_parts", None)
                if TextPart is not None and parts is not None:
                    parts.append(TextPart(text=block))
                else:  # 旧版本兜底
                    req.system_prompt = (getattr(req, "system_prompt", "") or "") + "\n\n" + block
        except Exception as e:
            logger.error(f"[ctx_timer] 上下文注入失败: {e}")

    # -- 指令入口 ---------------------------------------------------------- #
    @filter.command("ctx")
    @_admin_only()
    async def cmd_ctx(self, event: AstrMessageEvent):
        """上下文定时注入：帮助"""
        logger.info(f"[ctx_timer] 收到指令：{event.message_str!r}")
        yield event.plain_result(await self._dispatch(event))

    @filter.command("上下文")
    @_admin_only()
    async def cmd_ctx_cn(self, event: AstrMessageEvent):
        """上下文定时注入：帮助（中文指令）"""
        logger.info(f"[ctx_timer] 收到指令：{event.message_str!r}")
        yield event.plain_result(await self._dispatch(event))

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def cmd_bare(self, event: AstrMessageEvent):
        """裸指令：不 @ 机器人、不加前缀也能用（仅管理员）。"""
        _raw = event.message_str or ""
        if ("ctx" in _raw.lower()) or ("上下文" in _raw):
            try:
                _adm = self._is_admin(event)
            except Exception:
                _adm = "err"
            logger.info(
                f"[ctx_timer] ALL 处理器被调用：{_raw!r} "
                f"wake={getattr(event, 'is_at_or_wake_command', None)} admin={_adm} pid={self._pid(event)}"
            )
        if getattr(event, "is_at_or_wake_command", False):
            return
        text = self._strip_mentions(event.message_str or "")
        if not text:
            return
        low = text.lower()
        rest = None
        for w in sorted(COMMAND_WORDS, key=len, reverse=True):
            wl = w.lower()
            if low == wl:
                rest = ""
                break
            if low.startswith(wl + " "):
                rest = text[len(w):].strip()
                break
            if text.startswith(w):
                rest = text[len(w):].strip()
                break
        if rest is None:
            return
        sub = rest.split()[0].lower() if rest else ""
        if sub and sub not in SUB_WORDS:
            return
        if not self._is_admin(event):
            return
        logger.info(f"[ctx_timer] 裸指令命中：{text!r}")
        reply = await self._dispatch(event, text=text)
        # 先 yield 让回复进入后面的发送阶段，再停止事件；
        # 反过来会连这一轮的回复一起被丢掉。
        yield event.plain_result(reply)
        event.stop_event()

    # -- 分步向导监听 ------------------------------------------------------ #
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def wizard_listener(self, event: AstrMessageEvent):
        uid = self._uid(event)
        st = self._wizard.get(uid)
        if not st:
            return
        text = (event.message_str or "").strip()
        if text.startswith(("/ctx", "/上下文", "／ctx")):
            return
        if self._looks_like_command(text):
            return
        if (datetime.now().timestamp() - st.get("ts", 0)) > 180:
            self._wizard.pop(uid, None)
            yield event.plain_result("向导已超时（3 分钟），请重新发送 /ctx add")
            return
        st["ts"] = datetime.now().timestamp()
        reply = self._wizard_step(uid, st, text)
        yield event.plain_result(reply)
        event.stop_event()

    def _wizard_step(self, uid: str, st: dict, text: str) -> str:
        step = st.get("step")
        if text in ("取消", "cancel", "/cancel"):
            self._wizard.pop(uid, None)
            return "已取消向导。"

        if step == "name":
            if not text:
                return "名字不能为空，请重新输入："
            st["name"] = text[:30]
            st["step"] = "time"
            return (
                "第 2/3 步：生效时间。两种写法都行：\n"
                "· 每日：09:00 18:00\n"
                "· 一次性：2026-11-05 09:00 2026-11-11 23:59\n"
                "（也支持 now+30m 这类写法）\n"
                "要指定时区就在后面加 @UTC+8，例如：09:00 18:00 @UTC+8"
            )

        if step == "time":
            now = self._now()
            parts = text.split()
            tz_val = st.get("tz", "")
            if parts and parts[-1].startswith("@"):
                tz = resolve_tz(parts[-1][1:], None)
                if tz is None:
                    return "时区格式不对，示例：09:00 18:00 @UTC+8"
                tz_val = tz_name(tz)
                parts = parts[:-1]
            spec, err, _ = parse_window(parts, now)
            if err:
                return f"{err}\n请重新输入时间，例如：09:00 18:00"
            st.update(spec)
            st["tz"] = tz_val
            st["step"] = "content"
            return "第 3/3 步：窗口内要注入什么内容？（可多行）"

        if step == "content":
            content = text.replace("\\n", "\n").strip()
            if not content:
                return "内容不能为空，请重新输入："
            pid = st.get("pid", "unknown")
            rule = {
                "name": st.get("name", "未命名"),
                "mode": st.get("mode", "daily"),
                "start": st.get("start", "00:00"),
                "end": st.get("end", "23:59"),
                "tz": st.get("tz", ""),
                "content": content,
                "inject": str(self._cfg("default_inject", DEFAULT_INJECT)),
                "enabled": True,
                "scope": {"type": "all"},
                "notify": bool(self._cfg("default_notify", DEFAULT_NOTIFY)),
                "notify_umo": st.get("umo", ""),
                "created_at": self._now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            saved = self.store.add(pid, rule)
            self._wizard.pop(uid, None)
            return (
                f"已创建规则 [{saved['id']}] {saved['name']}\n"
                f"时间：{window_text(saved)}\n"
                f"内容：{saved['content'][:60]}{'…' if len(saved['content']) > 60 else ''}\n"
                "发送 /ctx list 查看全部规则。"
            )
        self._wizard.pop(uid, None)
        return "向导状态异常，已重置。"

    # -- 分发 -------------------------------------------------------------- #
    async def _dispatch(self, event: AstrMessageEvent, text: str | None = None) -> str:
        raw = (text if text is not None else (event.message_str or ""))
        raw = self._strip_mentions(raw).strip()
        if raw[:1] in ("/", "／", "!"):
            raw = raw[1:]
        tokens = raw.split()
        if tokens and tokens[0].lower() in COMMAND_WORDS:
            tokens = tokens[1:]
        if not tokens:
            return HELP_TEXT

        sub = tokens[0].lower()
        args = tokens[1:]

        table = {
            "help": self._cmd_help, "帮助": self._cmd_help, "?": self._cmd_help,
            "add": self._cmd_add, "添加": self._cmd_add, "新建": self._cmd_add,
            "list": self._cmd_list, "列表": self._cmd_list, "ls": self._cmd_list,
            "kk": self._cmd_list,
            "show": self._cmd_show, "查看": self._cmd_show, "info": self._cmd_show,
            "del": self._cmd_del, "delete": self._cmd_del, "删除": self._cmd_del, "rm": self._cmd_del,
            "on": self._cmd_on, "开启": self._cmd_on,
            "off": self._cmd_off, "关闭": self._cmd_off,
            "edit": self._cmd_edit, "修改": self._cmd_edit,
            "mode": self._cmd_mode, "模式": self._cmd_mode,
            "tz": self._cmd_tz, "时区": self._cmd_tz, "timezone": self._cmd_tz,
            "scope": self._cmd_scope, "范围": self._cmd_scope,
            "notify": self._cmd_notify, "提醒": self._cmd_notify,
            "test": self._cmd_test, "测试": self._cmd_test,
            "status": self._cmd_status, "状态": self._cmd_status,
            "hal": self._cmd_status,
            "bots": self._cmd_bots, "机器人": self._cmd_bots,
            "export": self._cmd_export, "导出": self._cmd_export,
            "import": self._cmd_import, "导入": self._cmd_import,
            "cancel": self._cmd_cancel, "取消": self._cmd_cancel,
        }
        fn = table.get(sub)
        if fn is None:
            return f"未知指令：{sub}\n\n{HELP_TEXT}"
        try:
            return await fn(event, args)
        except Exception as e:
            logger.exception(f"[ctx_timer] 指令执行失败: {e}")
            return f"执行出错：{e}"

    # -- 各指令实现 -------------------------------------------------------- #
    async def _cmd_help(self, event, args) -> str:
        return HELP_TEXT

    async def _cmd_cancel(self, event, args) -> str:
        self._wizard.pop(self._uid(event), None)
        return "已取消向导。"

    def _resolve_bot(self, pid: str, want: str | None) -> tuple[str | None, str]:
        if not want:
            return pid, ""
        if not bool(self._cfg("allow_cross_bot", ALLOW_CROSS_BOT)):
            return None, "配置里已关闭「跨 Bot 管理」，只能在当前 Bot 下操作（去掉 -b 参数）。"
        bots = self.store.bot_ids()
        if want in bots:
            return want, ""
        hit = [b for b in bots if want.lower() in b.lower()]
        if len(hit) == 1:
            return hit[0], ""
        if not hit:
            return None, f"没找到 Bot：{want}\n现有：{', '.join(bots) or '（无）'}"
        return None, f"「{want}」匹配到多个 Bot：{', '.join(hit)}，请写完整名称。"

    async def _cmd_add(self, event, args) -> str:
        args, tz_raw = self._pop_opt(args, ("-tz", "--tz"))
        tz_val = ""
        if tz_raw is not None:
            tz = resolve_tz(tz_raw, None)
            if tz is None:
                return "时区格式不对，示例：UTC+8、UTC+2、UTC+3、UTC+6、UTC-3、UTC+5:30"
            tz_val = tz_name(tz)
        if not args:
            uid = self._uid(event)
            self._wizard[uid] = {
                "step": "name",
                "pid": self._pid(event),
                "umo": self._umo(event),
                "tz": tz_val,
                "ts": datetime.now().timestamp(),
            }
            return (
                "分步添加（发「取消」退出）\n"
                "第 1/3 步：这条规则叫什么名字？\n\n"
                "也可以一行搞定：\n"
                "/ctx add 名称 09:00 18:00 注入内容\n"
                "/ctx add 名称 09:00 18:00 内容 -tz UTC+8"
            )

        name = args[0]
        if len(args) < 3:
            return "格式：/ctx add <名称> <开始> <关闭> <内容>\n例如：/ctx add 早班 09:00 18:00 现在是工作时段"
        now = self._now()
        spec, err, used = parse_window(args[1:], now)
        if err:
            return f"{err}\n示例：/ctx add 早班 09:00 18:00 现在是工作时段"

        content = " ".join(args[1 + used:]).strip().replace("\\n", "\n")
        if not content:
            return "内容不能为空。格式：/ctx add <名称> <开始> <关闭> <内容>"

        pid = self._pid(event)
        if len(self.store.rules(pid)) >= int(self._cfg("max_rules_per_bot", DEFAULT_MAX_RULES)):
            return f"当前 Bot 规则数已达上限（{self._cfg('max_rules_per_bot', DEFAULT_MAX_RULES)} 条），请先删除旧规则。"

        rule = {
            "name": name[:30],
            "mode": spec["mode"],
            "start": spec["start"],
            "end": spec["end"],
            "tz": tz_val,
            "content": content,
            "inject": str(self._cfg("default_inject", DEFAULT_INJECT)),
            "enabled": True,
            "scope": {"type": "all"},
            "notify": bool(self._cfg("default_notify", DEFAULT_NOTIFY)),
            "notify_umo": self._umo(event),
            "created_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        }
        saved = self.store.add(pid, rule)
        return (
            f"已创建规则 [{saved['id']}] {saved['name']}\n"
            f"时间：{window_text(saved)}\n"
            f"注入：{'system_prompt' if saved['inject'] == 'system' else '上下文注入'}\n"
            f"范围：全部会话\n"
            "发送 /ctx list 查看，/ctx test {0} 预览效果。".format(saved["id"])
        )

    def _fmt_line(self, rule: dict, now: datetime) -> str:
        state = rule_state(rule, now)
        return f"[{rule.get('id')}] {rule.get('name')}  {state}  {window_text(rule)}"

    async def _cmd_list(self, event, args) -> str:
        args, want = self._pop_bot(args)
        pid, err = self._resolve_bot(self._pid(event), want)
        if err:
            return err
        now = self._now()
        rules = self.store.rules(pid)
        show_all = "-a" in args or "--all" in args
        if not rules:
            return f"Bot「{pid}」还没有任何定时上下文规则。\n发送 /ctx add 开始创建。"
        lines = [f"Bot「{pid}」的定时上下文（共 {len(rules)} 条）"]
        shown = 0
        for r in rules:
            st = rule_state(r, now)
            if not show_all and st == "已结束":
                continue
            lines.append(self._fmt_line(r, now))
            shown += 1
        if shown == 0:
            lines.append("（没有进行中的规则，加 -a 查看已结束的）")
        lines.append("")
        lines.append("查看详情：/ctx show <编号>")
        return "\n".join(lines)

    def _pop_bot(self, args: list[str]) -> tuple[list[str], str | None]:
        return self._pop_opt(args, ("-b", "--bot"))

    def _pop_opt(self, args: list[str], names) -> tuple[list[str], str | None]:
        """从参数里摘出 `-x 值` 形式的选项。"""
        out, val = [], None
        i = 0
        while i < len(args):
            if args[i].lower() in names and i + 1 < len(args):
                val = args[i + 1]
                i += 2
            else:
                out.append(args[i])
                i += 1
        return out, val

    async def _cmd_show(self, event, args) -> str:
        args, want = self._pop_bot(args)
        pid, err = self._resolve_bot(self._pid(event), want)
        if err:
            return err
        if not args:
            return "用法：/ctx show <编号>"
        try:
            rid = int(args[0])
        except ValueError:
            return "编号必须是数字。"
        rule = self.store.get(pid, rid)
        if not rule:
            return f"没有找到编号为 {rid} 的规则。"
        now = self._now()
        scope = rule.get("scope") or {"type": "all"}
        if scope.get("type", "all") == "all":
            scope_txt = "全部会话"
        else:
            bits = []
            if scope.get("groups"):
                bits.append("群 " + ",".join(scope["groups"]))
            if scope.get("users"):
                bits.append("用户 " + ",".join(scope["users"]))
            if scope.get("umos"):
                bits.append("会话 " + ",".join(scope["umos"]))
            scope_txt = " / ".join(bits) or "全部会话"
        return (
            f"📌 [{rule.get('id')}] {rule.get('name')}\n"
            f"状态：{rule_state(rule, now)}\n"
            f"时间：{window_text(rule)}\n"
            f"时区：{rule.get('tz') or '跟随全局（' + tz_name(now.tzinfo) + '）'}\n"
            f"注入：{'system_prompt' if rule.get('inject') == 'system' else '上下文注入'}\n"
            f"范围：{scope_txt}\n"
            f"到点提醒：{'开' if rule.get('notify') else '关'}\n"
            f"创建：{rule.get('created_at', '未知')}\n"
            f"—— 内容 ——\n{rule.get('content', '')}"
        )

    async def _cmd_del(self, event, args) -> str:
        args, want = self._pop_bot(args)
        pid, err = self._resolve_bot(self._pid(event), want)
        if err:
            return err
        if not args:
            return "用法：/ctx del <编号>"
        try:
            rid = int(args[0])
        except ValueError:
            return "编号必须是数字。"
        rule = self.store.get(pid, rid)
        if not rule:
            return f"没有找到编号为 {rid} 的规则。"
        self.store.delete(pid, rid)
        return f"已删除规则 [{rid}] {rule.get('name')}"

    async def _toggle(self, event, args, value: bool) -> str:
        args, want = self._pop_bot(args)
        pid, err = self._resolve_bot(self._pid(event), want)
        if err:
            return err
        if not args:
            return "用法：/ctx on|off <编号>"
        try:
            rid = int(args[0])
        except ValueError:
            return "编号必须是数字。"
        rule = self.store.get(pid, rid)
        if not rule:
            return f"没有找到编号为 {rid} 的规则。"
        rule["enabled"] = value
        self.store.save()
        return f"{'已启用' if value else '已停用'}规则 [{rid}] {rule.get('name')}"

    async def _cmd_on(self, event, args) -> str:
        return await self._toggle(event, args, True)

    async def _cmd_off(self, event, args) -> str:
        return await self._toggle(event, args, False)

    async def _cmd_edit(self, event, args) -> str:
        args, want = self._pop_bot(args)
        pid, err = self._resolve_bot(self._pid(event), want)
        if err:
            return err
        if len(args) < 3:
            return (
                "用法：/ctx edit <编号> <字段> <值>\n"
                "字段：名称 / 内容 / 开始 / 关闭 / 注入 / 时区\n"
                "例：/ctx edit 1 内容 现在是午休时间，回复可以随意些"
            )
        try:
            rid = int(args[0])
        except ValueError:
            return "编号必须是数字。"
        rule = self.store.get(pid, rid)
        if not rule:
            return f"没有找到编号为 {rid} 的规则。"

        field = args[1].lower()
        value = " ".join(args[2:]).strip()

        if field in ("名称", "name", "名字"):
            if not value:
                return "名称不能为空。"
            rule["name"] = value[:30]
        elif field in ("内容", "content"):
            if not value:
                return "内容不能为空。"
            rule["content"] = value.replace("\\n", "\n")
        elif field in ("注入", "inject"):
            if value not in ("context", "system", "动态", "系统"):
                return "注入方式只能是 context（上下文注入）或 system（系统提示词）。"
            rule["inject"] = "system" if value in ("system", "系统") else "context"
        elif field in ("开始", "start", "关闭", "end", "时间"):
            now = self._now()
            if field in ("时间", "time"):
                spec, err2, _ = parse_window(value.split(), now)
            elif field in ("开始", "start"):
                p = parse_endpoint(value.split(), 0, now)
                if not p:
                    return "开始时间格式不对。"
                dt, has_date, date_only, _ = p
                spec, err2 = self._rebuild_window(rule, dt, has_date, date_only, True)
            else:
                p = parse_endpoint(value.split(), 0, now)
                if not p:
                    return "关闭时间格式不对。"
                dt, has_date, date_only, _ = p
                spec, err2 = self._rebuild_window(rule, dt, has_date, date_only, False)
            if err2:
                return f"{err2}"
            rule.update(spec)
        elif field in ("时区", "tz", "timezone"):
            if value.lower() in ("默认", "全局", "global", "default", "off", "跟随全局"):
                rule["tz"] = ""
            else:
                tz = resolve_tz(value, None)
                if tz is None:
                    return "时区格式不对，示例：UTC+8、UTC-3、UTC+5:30"
                rule["tz"] = tz_name(tz)
        else:
            return "未知字段，可用：名称 / 内容 / 开始 / 关闭 / 注入 / 时区"
        self.store.save()
        return f"已更新规则 [{rid}] {rule.get('name')}\n时间：{window_text(rule)}"

    def _rebuild_window(self, rule, dt, has_date, date_only, is_start):
        """修改单侧时间后重建窗口。"""
        if is_start:
            start, end = dt, None
            if rule.get("mode") == "daily" and not has_date:
                try:
                    end = datetime.strptime(rule["end"], "%H:%M").replace(
                        year=dt.year, month=dt.month, day=dt.day, tzinfo=dt.tzinfo)
                except Exception:
                    end = dt
            elif rule.get("mode") == "once" and not has_date:
                try:
                    base = datetime.strptime(rule["end"], "%Y-%m-%d %H:%M").replace(tzinfo=dt.tzinfo)
                    end = base.replace(hour=dt.hour, minute=dt.minute)
                except Exception:
                    end = dt + timedelta(hours=1)
            else:
                end = dt + timedelta(hours=1)
        else:
            end, start = dt, None
            if rule.get("mode") == "daily" and not has_date:
                try:
                    start = datetime.strptime(rule["start"], "%H:%M").replace(
                        year=dt.year, month=dt.month, day=dt.day, tzinfo=dt.tzinfo)
                except Exception:
                    start = dt
            elif rule.get("mode") == "once" and not has_date:
                try:
                    base = datetime.strptime(rule["start"], "%Y-%m-%d %H:%M").replace(tzinfo=dt.tzinfo)
                    start = base.replace(hour=dt.hour, minute=dt.minute)
                except Exception:
                    start = dt - timedelta(hours=1)
            else:
                start = dt - timedelta(hours=1)

        if (start and start.tzinfo is None) or (end and end.tzinfo is None):
            tz = self._now().tzinfo
            start = start.replace(tzinfo=tz) if start and start.tzinfo is None else start
            end = end.replace(tzinfo=tz) if end and end.tzinfo is None else end

        if has_date or rule.get("mode") == "once":
            if start.date() != end.date():
                # 跨天的一次性窗口
                if end <= start:
                    return None, "关闭时间必须晚于开始时间"
            elif end <= start:
                return None, "关闭时间必须晚于开始时间"
            return {
                "mode": "once",
                "start": start.strftime("%Y-%m-%d %H:%M"),
                "end": end.strftime("%Y-%m-%d %H:%M"),
            }, None

        sm, em = start.hour * 60 + start.minute, end.hour * 60 + end.minute
        if sm == em:
            return None, "开始与关闭时间不能相同"
        return {"mode": "daily", "start": min_to_hhmm(sm), "end": min_to_hhmm(em)}, None

    async def _cmd_mode(self, event, args) -> str:
        args, want = self._pop_bot(args)
        pid, err = self._resolve_bot(self._pid(event), want)
        if err:
            return err
        if len(args) < 2:
            return "用法：/ctx mode <编号> system|context"
        try:
            rid = int(args[0])
        except ValueError:
            return "编号必须是数字。"
        rule = self.store.get(pid, rid)
        if not rule:
            return f"没有找到编号为 {rid} 的规则。"
        val = args[1].lower()
        if val in ("system", "系统"):
            rule["inject"] = "system"
            tip = "系统提示词每轮变化会破坏提示词缓存，可能增加费用。"
        elif val in ("context", "动态"):
            rule["inject"] = "context"
            tip = "上下文注入不破坏缓存。"
        else:
            return "只能是 system 或 context。"
        self.store.save()
        label = "系统提示词（system_prompt）" if rule["inject"] == "system" else "上下文注入"
        return f"已设置规则 [{rid}] 注入方式为{label}\n{tip}"

    async def _cmd_tz(self, event, args) -> str:
        args, want = self._pop_bot(args)
        pid, err = self._resolve_bot(self._pid(event), want)
        if err:
            return err
        now = self._now()
        if not args:
            lines = [
                f"全局时区：{tz_name(now.tzinfo)}（当前 {now.strftime('%Y-%m-%d %H:%M')}）",
                "",
                "设置某条规则的时区：",
                "/ctx tz 1 UTC+8      常用：UTC+8 / UTC+2 / UTC+3 / UTC+6 / UTC-3",
                "/ctx tz 1 UTC+5:30   也支持半小时时区",
                "/ctx tz 1 默认        跟随全局",
                "",
                "规则时区：",
            ]
            rules = self.store.rules(pid)
            if not rules:
                lines.append("（还没有规则）")
            for r in rules:
                tz_txt = r.get("tz") or "跟随全局"
                lines.append(f"· [{r.get('id')}] {r.get('name')}：{tz_txt}")
            return "\n".join(lines)

        if len(args) < 2:
            return "用法：/ctx tz <编号> UTC+8\n可用：UTC+8 / UTC+2 / UTC+3 / UTC+6 / UTC-3 / UTC+5:30 / 默认"
        try:
            rid = int(args[0])
        except ValueError:
            return "编号必须是数字。"
        rule = self.store.get(pid, rid)
        if not rule:
            return f"没有找到编号为 {rid} 的规则。"

        val = args[1]
        if val.lower() in ("默认", "全局", "global", "default", "off", "跟随全局"):
            rule["tz"] = ""
            self.store.save()
            return f"已把规则 [{rid}] 的时区设为跟随全局（{tz_name(now.tzinfo)}）。"
        tz = resolve_tz(val, None)
        if tz is None:
            return "时区格式不对。可用：UTC+8 / UTC+2 / UTC+3 / UTC+6 / UTC-3 / UTC+5:30 / 默认"
        rule["tz"] = tz_name(tz)
        self.store.save()
        local = now.astimezone(tz)
        return (
            f"规则 [{rid}] 时区已设为 {rule['tz']}\n"
            f"该时区现在：{local.strftime('%Y-%m-%d %H:%M')}\n"
            f"窗口：{window_text(rule)}"
        )

    async def _cmd_scope(self, event, args) -> str:
        args, want = self._pop_bot(args)
        pid, err = self._resolve_bot(self._pid(event), want)
        if err:
            return err
        if len(args) < 2:
            return (
                "用法：/ctx scope <编号> all\n"
                "      /ctx scope <编号> g:群号\n"
                "      /ctx scope <编号> u:QQ号\n"
                "      /ctx scope <编号> +g:群号 / -g:群号"
            )
        try:
            rid = int(args[0])
        except ValueError:
            return "编号必须是数字。"
        rule = self.store.get(pid, rid)
        if not rule:
            return f"没有找到编号为 {rid} 的规则。"

        scope = rule.get("scope") or {"type": "custom", "groups": [], "users": [], "umos": []}
        if scope.get("type") == "all":
            scope = {"type": "custom", "groups": [], "users": [], "umos": []}

        target = args[1]
        if target.lower() == "all":
            rule["scope"] = {"type": "all"}
            self.store.save()
            return f"已把规则 [{rid}] 的范围设为全部会话。"

        sign = "+"
        if target[:1] in "+-":
            sign, target = target[0], target[1:]
        kind, _, val = target.partition(":")
        if not val:
            return "格式不对，示例：g:123456 或 u:10001"

        key = {"g": "groups", "群": "groups", "u": "users", "用户": "users", "umo": "umos"}.get(kind)
        if key is None:
            return "前缀只能是 g:（群）、u:（用户）或 umo:（会话）"

        bucket = scope.setdefault(key, [])
        if sign == "+":
            if val not in bucket:
                bucket.append(val)
        else:
            if val in bucket:
                bucket.remove(val)

        scope["type"] = "custom" if any(scope.get(k) for k in ("groups", "users", "umos")) else "all"
        rule["scope"] = scope
        self.store.save()

        bits = []
        if scope.get("groups"):
            bits.append("群 " + ",".join(scope["groups"]))
        if scope.get("users"):
            bits.append("用户 " + ",".join(scope["users"]))
        if scope.get("umos"):
            bits.append("会话 " + ",".join(scope["umos"]))
        return f"已更新规则 [{rid}] 范围：{' / '.join(bits) or '全部会话'}"

    async def _cmd_notify(self, event, args) -> str:
        args, want = self._pop_bot(args)
        pid, err = self._resolve_bot(self._pid(event), want)
        if err:
            return err
        if len(args) < 2:
            return "用法：/ctx notify <编号> on|off"
        try:
            rid = int(args[0])
        except ValueError:
            return "编号必须是数字。"
        rule = self.store.get(pid, rid)
        if not rule:
            return f"没有找到编号为 {rid} 的规则。"
        val = args[1].lower()
        if val in ("on", "开", "true", "1"):
            rule["notify"] = True
            rule["notify_umo"] = rule.get("notify_umo") or self._umo(event)
            if not rule["notify_umo"]:
                return "当前会话没有可用的 unified_msg_origin，无法发送提醒。"
            self.store.save()
            return f"已开启规则 [{rid}] 的到点提醒，将发送到当前会话。"
        if val in ("off", "关", "false", "0"):
            rule["notify"] = False
            self.store.save()
            return f"已关闭规则 [{rid}] 的到点提醒。"
        return "只能是 on 或 off。"

    async def _cmd_test(self, event, args) -> str:
        args, want = self._pop_bot(args)
        pid, err = self._resolve_bot(self._pid(event), want)
        if err:
            return err
        if not args:
            return "用法：/ctx test <编号>"
        try:
            rid = int(args[0])
        except ValueError:
            return "编号必须是数字。"
        rule = self.store.get(pid, rid)
        if not rule:
            return f"没有找到编号为 {rid} 的规则。"
        now = self._now()
        state = rule_state(rule, now)
        preview = self._render([rule], now)
        note = "" if state == "生效中" else f"\n（注意：当前状态是「{state}」，此预览不会真的注入）"
        return f"规则 [{rid}] 注入预览{note}\n\n{preview}"

    async def _cmd_status(self, event, args) -> str:
        pid = self._pid(event)
        now = self._now()
        rules = self.store.rules(pid)
        active = [r for r in rules if rule_active(r, now)]
        lines = [
            f"当前时间：{now.strftime('%Y-%m-%d %H:%M:%S')}",
            f"当前 Bot：{pid}",
            f"规则总数：{len(rules)} 条，生效中 {len(active)} 条",
        ]
        if active:
            lines.append("")
            lines.append("正在注入：")
            for r in active:
                lines.append(f"  [{r['id']}] {r['name']}（{window_text(r)}）")
        nxt = self._next_change(rules, now)
        if nxt:
            lines.append("")
            lines.append(nxt)
        return "\n".join(lines)

    def _next_change(self, rules: list[dict], now: datetime) -> str:
        best = None
        for r in rules:
            if not r.get("enabled", True):
                continue
            tz = rule_tz(r, now)
            local = rule_local_now(r, now)
            if r.get("mode") == "daily":
                s = hhmm_to_min(r["start"])
                e = hhmm_to_min(r["end"])
                cur = local.hour * 60 + local.minute
                candidates = []
                for m in (s, e):
                    delta = (m - cur) % 1440
                    if delta == 0:
                        delta = 1440
                    candidates.append((delta, "生效" if m == s else "关闭"))
                delta, what = min(candidates)
                when = local + timedelta(minutes=delta)
            else:
                try:
                    s = datetime.strptime(r["start"], "%Y-%m-%d %H:%M").replace(tzinfo=tz)
                    e = datetime.strptime(r["end"], "%Y-%m-%d %H:%M").replace(tzinfo=tz)
                except Exception:
                    continue
                if local < s:
                    delta, what, when = s - local, "生效", s
                elif local < e:
                    delta, what, when = e - local, "关闭", e
                else:
                    continue
            if best is None or delta < best[0]:
                best = (delta, what, when, r)
        if not best:
            return ""
        _, what, when, r = best
        label = f"（{r['tz']}）" if r.get("tz") else ""
        return f"下一条：[{r['id']}] {r['name']} 将于 {when.strftime('%Y-%m-%d %H:%M')}{label} {what}"

    async def _cmd_bots(self, event, args) -> str:
        bots = self.store.bot_ids()
        if not bots:
            return "还没有任何 Bot 建立过规则。"
        now = self._now()
        lines = ["已建立规则库的 Bot："]
        for pid in bots:
            rules = self.store.rules(pid)
            act = sum(1 for r in rules if rule_active(r, now))
            mark = " ← 当前" if pid == self._pid(event) else ""
            lines.append(f"· {pid}：{len(rules)} 条（生效中 {act}）{mark}")
        lines.append("")
        lines.append("用 -b <Bot> 可管理其它 Bot 的规则，例如：/ctx list -b " + bots[0])
        return "\n".join(lines)

    async def _cmd_export(self, event, args) -> str:
        pid = self._pid(event)
        data = {"bot": pid, "rules": self.store.rules(pid)}
        return "导出（复制下面整段保存）：\n" + json.dumps(data, ensure_ascii=False)

    async def _cmd_import(self, event, args) -> str:
        if not args:
            return "用法：/ctx import {\"rules\": [...]}"
        raw = " ".join(args).strip()
        try:
            data = json.loads(raw)
            rules = data["rules"] if isinstance(data, dict) else data
            if not isinstance(rules, list):
                raise ValueError("rules 必须是数组")
        except Exception as e:
            return f"解析失败：{e}"
        pid = self._pid(event)
        count = 0
        for r in rules:
            if not isinstance(r, dict):
                continue
            rule = {
                "name": str(r.get("name", "导入规则"))[:30],
                "mode": r.get("mode", "daily"),
                "start": str(r.get("start", "00:00")),
                "end": str(r.get("end", "23:59")),
                "content": str(r.get("content", "")),
                "inject": "system" if r.get("inject") == "system" else "context",
                "enabled": bool(r.get("enabled", True)),
                "scope": r.get("scope") or {"type": "all"},
                "notify": bool(r.get("notify", False)),
                "notify_umo": str(r.get("notify_umo", "")),
                "created_at": self._now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            if not rule["content"]:
                continue
            self.store.add(pid, rule)
            count += 1
        return f"已导入 {count} 条规则到 Bot「{pid}」。"

    # -- 到点提醒 ---------------------------------------------------------- #
    def _tick_seconds(self) -> int:
        try:
            return max(10, int(self._cfg("notify_tick", DEFAULT_TICK)))
        except Exception:
            return DEFAULT_TICK

    async def _tick_loop(self):
        await asyncio.sleep(5)
        while True:
            try:
                await self._check_transitions()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[ctx_timer] 状态检查失败: {e}")
            await asyncio.sleep(self._tick_seconds())

    async def _check_transitions(self):
        now = self._now()
        for pid, lib in list(self.store.data.get("bots", {}).items()):
            for rule in lib.get("rules", []):
                key = (pid, int(rule.get("id", 0)))
                active = rule_active(rule, now)
                prev = self._last_state.get(key)
                self._last_state[key] = active
                if prev is None or prev == active:
                    continue
                if not rule.get("notify"):
                    continue
                umo = rule.get("notify_umo") or ""
                if not umo:
                    continue
                if active:
                    text = f"定时上下文已生效：[{rule.get('id')}] {rule.get('name')}\n{window_text(rule)}"
                else:
                    text = f"定时上下文已关闭：[{rule.get('id')}] {rule.get('name')}"
                try:
                    await self.context.send_message(umo, MessageChain().message(text))
                except Exception as e:
                    logger.error(f"[ctx_timer] 提醒发送失败: {e}")

    async def terminate(self):
        if self._tick_task and not self._tick_task.done():
            self._tick_task.cancel()
            try:
                await self._tick_task
            except asyncio.CancelledError:
                pass


HELP_TEXT = """CTEX伴侣 v1.0.1.7 · 到点自动注入上下文，过点自动撤下

【咋用】
  ctx add 早班 09:00 18:00 现在是工作时间，回复要正式
  ctx add                      跟着向导填（名字 → 时间 → 内容）
  ctx kk                       看规则列表
  ctx hal                      看现在生效什么

【时间】
  每天 09:00 18:00             也支持跨天 22:00 06:00
  一次 2026-11-05 09:00 2026-11-05 18:00
  相对 now+30m

【改】
  ctx on 1 / ctx off 1         启用 / 停用
  ctx edit 1 内容 新内容        改内容
  ctx del 1                    删除
  ctx mode 1 system            改成系统提示词（默认上下文注入，更省钱）
  ctx tz 1 UTC+8               这条规则单独用时区（默认跟全局 UTC+3）

【其它】
  ctx show 1 详情 · ctx test 1 预览 · ctx tz 看时区 · ctx bots 看各 Bot 规则数
  前缀都认：/ctx、.ctx、或者不加前缀直接发 ctx

最有用：把硬要求做成关键提示（比如凌晨 1-5 点必须换人格），模型就不会跑偏。"""


__all__ = ["CtxTimerPlugin", "RuleStore", "parse_window", "rule_active", "rule_state"]

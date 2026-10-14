"""
apiroutes.deps
==============
FastAPI 后端的**共享依赖层**：从 api.py 抽出的惰性单例、鉴权、限速、会话记忆、
请求模型等。各 router（query/analyze/governance/hitl/subscriptions）复用这里的实现，
保证"鉴权只认凭据、口径一致、限速统一"等不变量只有一份。

与 api.py 的关系：api.py 只负责**组装** app（CORS + include_router + /health），
并把本模块的符号**再导出**，保持 `import api` 的既有调用面不变（测试/工具大量依赖它）。
"""
from __future__ import annotations

import hashlib
import os
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import Header, HTTPException
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:  # noqa: BLE001
    pass


def answer_impl(*args, **kwargs):
    """惰性导入业务服务层（避免 import 期就加载 LLM/数据库依赖）。"""
    from sqlpa.business.service import answer
    return answer(*args, **kwargs)


# ---------------- 依赖（惰性单例） ----------------

def _cfg():
    from sqlpa.business.metric_config import load_config
    return load_config()


_DB_KIND_WARNED = False


def _db_path() -> str:
    """业务库路径：环境变量 SQLPA_DB_PATH > 真实全量库 > 仓库自带样本库。

    这样 `git clone` 之后不下载 66MB 真实数据也能跑通接口与前端演示
    （样本库见 data/sample/olist_sample.db，由 tools/build_olist_sample.py 生成）。
    """
    override = (os.getenv("SQLPA_DB_PATH") or "").strip()
    if override:
        return override
    from sqlpa.config import resolve_db_path
    path, kind = resolve_db_path()
    if kind == "sample":
        global _DB_KIND_WARNED
        if not _DB_KIND_WARNED:
            _DB_KIND_WARNED = True
            print("[数据] 未找到 data/olist/olist.db，使用仓库自带样本库 "
                  "data/sample/olist_sample.db（按月抽样，数值仅供流程演示）。")
    return path


def _bind_asof() -> None:
    """把相对时间词（本月/上月/最近N天）锚定到**数据自身的最新日期**。

    为什么必须在入口做：Olist 数据窗口只到 2018-10，而系统当天是 2026 年。
    若相对时间按 date.today() 解析，任何"上月 GMV 为什么跌"都会落在数据窗口之外
    → 当期/上期永远查空 → 用户看到的就只是"流程跑完、没有结果"。
    这里按业务库探测 max(order_purchase_timestamp) 并绑定（按库路径缓存，只探测一次）；
    探测失败不阻塞请求，回落系统当天。
    """
    try:
        from sqlpa.config import bind_data_asof_once
        bind_data_asof_once(_db_path())
    except Exception:  # noqa: BLE001 —— 探测失败不应让取数/分析请求失败
        pass


def _sandbox():
    from sqlpa.sandbox.sql_executor import ExecConfig, SqlSandbox
    # 由 config/settings.yaml 驱动（超时/多语句/额外禁用关键字）
    return SqlSandbox(_db_path(), ExecConfig.from_settings(max_rows=2000))


def _llm():
    """有 Key 用真实 LLM，否则 None（离线：口径内走确定性组装，口径外不支持）。"""
    if os.environ.get("LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY") \
            or os.environ.get("OPENAI_API_KEY"):
        from sqlpa.llm.openai_compat import OpenAICompatLLM
        return OpenAICompatLLM()
    return None


# ---------------- 模型 ----------------

class QueryIn(BaseModel):
    question: str = Field(..., description="自然语言业务问题")
    role: str = Field("analyst",
                      description="期望角色；仅可在 token 授予的权限范围内使用（不能提权）")
    history: List[dict] = Field(default_factory=list,
                                description="对话历史 [{'question','metric','sql'}]，用于多轮追问")
    confirm_id: Optional[str] = Field(
        None, description="口径确认记录 id（LLM 推断口径时由上一次响应返回）。"
                          "传入后按该记录的口径**逐字**执行；无效/已用过 → 拒绝")


class ReportIn(QueryIn):
    """报告导出请求：沿用查询入参，外加导出格式。"""
    fmt: str = Field("markdown", description="markdown / csv / both")


class AnalyzeIn(QueryIn):
    """多 Agent 分析请求：沿用查询入参，可用 alert_threshold_pct 覆盖默认告警阈值。"""
    alert_threshold_pct: float = Field(0.05, description="波动告警阈值（0.05 = 5%")
    session_id: Optional[str] = Field(
        None, description="会话 ID：复用则会话级 Agent 记忆（跨轮续下钻）；缺省为一次性无记忆会话")
    final: Optional[Dict] = Field(
        None, description="前一次 /api/analyze 返回的 final 载荷。传入则**直接据此生成报告**"
                          "（不重跑分析、零额外 LLM 成本）；缺省则按 question 重跑一次分析")


class SubscriptionIn(BaseModel):
    """新增订阅规则（指标 + 阈值 + 负责人 + 投递通道）。"""
    metric: str = Field(..., description="指标 key，如 gmv")
    threshold_pct: float = Field(0.05, description="波动阈值（0.05 = 5%）")
    owner: str = Field("", description="负责人（推送给谁）")
    question: str = Field("", description="订阅的业务问题（留空则用指标名）")
    time_spec: str = Field("本月", description="检查的时间窗语义，如 上月/2018-06")
    dims: List[str] = Field(default_factory=list, description="归因维度")
    channel: str = Field("console", description="投递通道：console / file / webhook")
    webhook: str = Field("", description="channel=webhook 时的回调地址")


class HitlDecisionIn(BaseModel):
    """HITL 工单状态流转。"""
    status: str = Field(..., description="in_progress / fixed / verified / dismissed / reopened")
    note: str = Field("", description="人工复核意见")


# ---------------- 鉴权 ----------------

def _token_map() -> Dict[str, str]:
    """解析 SQLPA_API_TOKENS="tokenA:admin,tokenB:analyst" → {token: role}。"""
    raw = (os.getenv("SQLPA_API_TOKENS") or "").strip()
    out: Dict[str, str] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        tok, _, role = part.partition(":")
        tok, role = tok.strip(), role.strip()
        if tok and role:
            out[tok] = role
    return out


def _resolve_role(token: Optional[str], requested: str) -> str:
    """决定本次请求实际使用的角色。

    原则：**角色只能来自凭据，不能来自请求体**。
    - 配了 token：必须提供有效 token；角色取自 token。请求体 role 只能用于降级
      （当它与 token 角色不同时，一律以 token 角色为准，避免提权）。
    - 未配 token：忽略请求体 role，使用最小权限默认角色。
    """
    tokens = _token_map()
    if tokens:
        if not token or token not in tokens:
            raise HTTPException(status_code=401, detail="缺少或无效的 X-API-Token")
        return tokens[token]
    return (os.getenv("SQLPA_DEFAULT_ROLE") or "analyst").strip() or "analyst"


def _caller_key(token: Optional[str], role: str) -> str:
    """调用者标识：优先用凭据指纹（不落原文），无凭据时退化为角色。"""
    if token:
        return hashlib.sha256(token.encode()).hexdigest()[:12]
    return f"role:{role}"


# ---- 最简内存滑动窗口限速（保护烧钱/重活端点）----
_RATE_LOCK = threading.Lock()
_RATE_HITS: Dict[str, List[float]] = {}
_RATE_MAX = int(os.getenv("SQLPA_RATE_LIMIT_PER_MIN", "30") or 30)
_RATE_WINDOW = 60.0


def _rate_limit(caller: str, bucket: str = "analyze") -> None:
    """同一调用者在 60s 内对同一 bucket 的请求数上限（默认 30/min）。

    为什么要有：/api/analyze 会真实调用 LLM（花钱且慢），无限速时一个脚本循环
    就能把额度与后端线程打满。SQLPA_RATE_LIMIT_PER_MIN<=0 关闭限速（评测用）。
    """
    if _RATE_MAX <= 0:
        return
    now = time.time()
    key = f"{bucket}::{caller}"
    with _RATE_LOCK:
        hits = [t for t in _RATE_HITS.get(key, []) if now - t < _RATE_WINDOW]
        if len(hits) >= _RATE_MAX:
            _RATE_HITS[key] = hits
            raise HTTPException(status_code=429,
                                detail=f"请求过于频繁（{bucket} 每分钟上限 {_RATE_MAX}），请稍后再试")
        hits.append(now)
        _RATE_HITS[key] = hits


# ---------------- 会话记忆（并发安全） ----------------
# session_id -> {last_drill, metric, ...}
# 加固点（此前是无上限、无过期、且只以**客户端自报**的 session_id 为键的全局 dict）：
#   - 键包含调用者（token 哈希/角色），换一个凭据拿同一个 session_id 读不到别人的记忆；
#   - 有 TTL 与容量上限（LRU 淘汰），不会随会话数无限增长；
#   - `_session_memory` 的 map 增删由全局锁保护；
#   - `_session_guard` 额外持有**该会话的独占锁**，把同一 (caller, session_id) 的并发
#     分析串行化 —— 因为 `_session_memory` 返回的是**共享可变 dict**，只有 map 级锁
#     不足以防两个请求同时改这个 dict 造成写撕裂/丢更新。
_SESSION_MEMORY: Dict[str, Dict] = {}
_SESSION_SEEN: Dict[str, float] = {}
_SESSION_LOCKS: Dict[str, threading.RLock] = {}
_SESSION_LOCK = threading.Lock()
_SESSION_TTL_SECONDS = float(os.getenv("SQLPA_SESSION_TTL", "3600") or 3600)
_SESSION_MAX = int(os.getenv("SQLPA_SESSION_MAX", "500") or 500)


def _session_memory(session_id: Optional[str], caller: str = "role:analyst") -> Dict:
    """取/建会话记忆 dict（**可变共享引用**，供 run_analysis 跨轮读写）。"""
    if not session_id:
        return {}
    key = f"{caller}::{session_id}"
    now = time.time()
    with _SESSION_LOCK:
        # 过期清理
        for k in [k for k, ts in _SESSION_SEEN.items() if now - ts > _SESSION_TTL_SECONDS]:
            _SESSION_SEEN.pop(k, None)
            _SESSION_MEMORY.pop(k, None)
            _SESSION_LOCKS.pop(k, None)
        # 容量淘汰（dict 保序 → 丢最早插入的一批，近似 LRU）
        if key not in _SESSION_MEMORY and len(_SESSION_MEMORY) >= _SESSION_MAX:
            for k in list(_SESSION_SEEN)[:max(1, _SESSION_MAX // 10)]:
                _SESSION_SEEN.pop(k, None)
                _SESSION_MEMORY.pop(k, None)
                _SESSION_LOCKS.pop(k, None)
        _SESSION_SEEN[key] = now
        return _SESSION_MEMORY.setdefault(key, {})


def _get_session_lock(key: str) -> "threading.RLock":
    """取该会话键的独占锁（惰性创建）。"""
    with _SESSION_LOCK:
        return _SESSION_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def _session_guard(session_id: Optional[str], caller: str = "role:analyst"):
    """会话记忆 + 独占锁：同一 (caller, session_id) 的并发分析被串行化。

    session_id 为空 → 一次性无记忆会话（返回临时空 dict，无锁）。
    """
    mem = _session_memory(session_id, caller)
    if not session_id:
        yield mem
        return
    lock = _get_session_lock(f"{caller}::{session_id}")
    with lock:
        # 握锁期间该会话记忆对本请求独占，避免并发写撕裂
        _SESSION_SEEN[f"{caller}::{session_id}"] = time.time()
        yield mem


# ---------------- 分析内部复用 ----------------

def _analyze_run(q: "AnalyzeIn", role: str, caller: str = "role:analyst") -> Dict:
    """非流式多 Agent 分析（/api/analyze/report 内部复用，避免重复实现）。"""
    from sqlpa.analysis.orchestrator import run_analysis
    with _session_guard(q.session_id, caller) as mem:
        return run_analysis(q.question, _cfg(), _sandbox(), _db_path(), _llm(),
                            role=role, alert_threshold_pct=q.alert_threshold_pct,
                            emit=None, memory=mem)

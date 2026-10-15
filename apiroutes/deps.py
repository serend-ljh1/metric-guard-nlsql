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


class FavoriteIn(BaseModel):
    """新增收藏：把一次查询存进"我的收藏"，供以后一键复跑。"""
    title: str = Field(..., description="收藏标题（用户自填）")
    user_input: str = Field("", description="原始自然语言问题")
    generated_sql: str = Field("", description="可复现的 SQL（可选）")


class FeedbackIn(BaseModel):
    """提交反馈：对一次查询点赞/点踩并附评论（坏例可导出补充评测集）。"""
    query_id: str = Field("", description="被评价的查询 id（审计表里的 query_id）")
    user_input: str = Field("", description="原始问题")
    generated_sql: str = Field("", description="当时的 SQL")
    rating: int = Field(..., description="1=点赞 / -1=点踩")
    comment: str = Field("", description="评论文本")


class MetricIn(BaseModel):
    """指标中心：新增/更新一条指标（写操作仅 admin，公式落回 business_config.yaml）。"""
    key: str = Field(..., description="指标 key（小写字母开头）")
    name: str = Field(..., description="指标名称")
    metric_expr: str = Field(..., description="计算表达式（分子/分母公式，口径核心）")
    from_clause: str = Field(..., description="数据来源（必须以 FROM 开头）")
    desc: str = Field("", description="指标说明")
    owner: str = Field("未指定", description="负责人")
    version: str = Field("v1", description="口径版本")
    join_clause: str = Field("", description="可选 JOIN 子句")
    where_core: str = Field("1=1", description="核心过滤条件")
    support_dims: List[str] = Field(default_factory=list, description="支持维度")
    support_filters: List[str] = Field(default_factory=list, description="支持过滤")
    editing_key: str = Field("", description="编辑既有指标时填原 key（重命名场景）")


class DimensionIn(BaseModel):
    """指标中心：新增/更新一个维度（写操作仅 admin）。"""
    key: str = Field(..., description="维度 key")
    name: str = Field(..., description="维度名称")
    sql_fragment: str = Field(..., description="维度 SQL 片段")
    editing_key: str = Field("", description="编辑既有维度时填原 key")


class AliasIn(BaseModel):
    """指标中心：新增/更新一条业务别名（写操作仅 admin）。"""
    name: str = Field(..., description="业务别名（自然语言词）")
    table: str = Field(..., description="映射到的表名")
    col: str = Field("", description="可选列名")


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


def _token_owner(token: Optional[str]) -> str:
    """无登录态时的调用者展示名：取凭据指纹（不落 token 原文）。"""
    if token:
        return "token:" + hashlib.sha256(token.encode()).hexdigest()[:8]
    return "anonymous"


def _resolve_identity(token: Optional[str], requested: str = "") -> Dict[str, str]:
    """解析本次请求的 (username, role) —— **角色只能来自凭据，不能来自请求体**。

    三种凭据来源，优先级从高到低：
      1. **登录会话**（`storage.sessions`）：token 是登录签发的随机串 → 归属人与角色
         都从会话记录取，审计因此能记到具体的人；
      2. **静态 token**（`SQLPA_API_TOKENS`）：未登录时的兼容模式，角色取自 token，
         用户名为 token 指纹（无法知道是谁，但至少不匿名）；
      3. 都没配（本地/演示/测试）：忽略请求体 role，用最小权限默认角色。
    """
    if token:
        try:
            from sqlpa.business import storage
            sess = storage.get_session(token)
        except Exception:  # noqa: BLE001 —— 会话存储不可用不应让鉴权整体失败
            sess = None
        if sess:
            return {"username": sess.get("username") or _token_owner(token),
                    "role": sess.get("role") or "analyst", "source": "session"}
    tokens = _token_map()
    if tokens:
        if not token or token not in tokens:
            raise HTTPException(status_code=401, detail="缺少或无效的 X-API-Token")
        return {"username": _token_owner(token), "role": tokens[token], "source": "static"}
    return {"username": _token_owner(token),
            "role": (os.getenv("SQLPA_DEFAULT_ROLE") or "analyst").strip() or "analyst",
            "source": "default"}


def _resolve_role(token: Optional[str], requested: str) -> str:
    """决定本次请求实际使用的角色（`_resolve_identity` 的角色投影）。"""
    return _resolve_identity(token, requested)["role"]


def _caller_key(token: Optional[str], role: str) -> str:
    """调用者标识：优先用凭据指纹（不落原文），无凭据时退化为角色。"""
    if token:
        return hashlib.sha256(token.encode()).hexdigest()[:12]
    return f"role:{role}"


def _identify(token: Optional[str], requested: str = "") -> Dict[str, str]:
    """端点统一入口：**一次**解析出 (username, role, caller)。

    为什么要合并：`_resolve_role` + `_caller_key` 各自解析会导致对 sessions 表查两次；
    且登录用户应以**用户维度**共享会话记忆（重新登录不该丢掉下钻上下文），
    所以 caller 在会话模式下取 `user:<用户名>`。
    """
    ident = _resolve_identity(token, requested)
    username, source = ident["username"], ident["source"]
    if source == "session" and username:
        caller = f"user:{username}"
    else:
        caller = _caller_key(token, ident["role"])
    return {"username": username, "role": ident["role"], "caller": caller, "source": source}


# ---- 滑动窗口限速（内存缓存 + SQLite 持久化；保护烧钱/重活端点）----
# 落库的意义：单进程重启不重置、多 worker 共享同一 DB 文件时限速才真的是……
# 全局限速。（此前纯内存：重启即清零、多 worker 各算各的。）
_RATE_LOCK = threading.Lock()
_RATE_HITS: Dict[str, List[float]] = {}
_RATE_MAX = int(os.getenv("SQLPA_RATE_LIMIT_PER_MIN", "30") or 30)
_RATE_WINDOW = 60.0


def _rate_read(key: str) -> List[float]:
    """读该 bucket 的时间戳窗口：内存缓存优先，未命中回落到持久层。"""
    hits = _RATE_HITS.get(key)
    if hits is not None:
        return hits
    try:
        from sqlpa.business import storage
        hits = [float(t) for t in storage.rate_load(key)]
    except Exception:  # noqa: BLE001
        hits = []
    _RATE_HITS[key] = hits
    return hits


def _rate_write(key: str, hits: List[float]) -> None:
    _RATE_HITS[key] = hits
    try:
        from sqlpa.business import storage
        storage.rate_save(key, hits, window_seconds=_RATE_WINDOW)
    except Exception:  # noqa: BLE001 —— 持久化失败不影响本次限速判定
        pass


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
        hits = [t for t in _rate_read(key) if now - t < _RATE_WINDOW]
        if len(hits) >= _RATE_MAX:
            _rate_write(key, hits)
            raise HTTPException(status_code=429,
                                detail=f"请求过于频繁（{bucket} 每分钟上限 {_RATE_MAX}），请稍后再试")
        hits.append(now)
        _rate_write(key, hits)


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
    """取/建会话记忆 dict（**可变共享引用**，供 run_analysis 跨轮读写）。

    缓存未命中时从**持久层恢复**：这样后端重启、或换到同一 DB 文件的另一个 worker，
    仍能续上「那 SP 呢」式多轮下钻，而不是把上下文清空。
    """
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
        if key not in _SESSION_MEMORY:
            _SESSION_MEMORY[key] = _session_restore(key)
        return _SESSION_MEMORY[key]


def _session_restore(key: str) -> Dict:
    """从持久层恢复会话记忆（失败回落空 dict —— 记忆丢了不该让分析失败）。"""
    try:
        from sqlpa.business import storage
        restored = storage.session_load(key)
        return restored if isinstance(restored, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _session_persist(key: str, data: Dict) -> None:
    """把本轮会话记忆写回持久层（失败只记日志，不影响已完成的分析）。"""
    try:
        from sqlpa.business import storage
        storage.session_save(key, data, ttl_seconds=_SESSION_TTL_SECONDS)
    except Exception:  # noqa: BLE001
        pass


def _get_session_lock(key: str) -> "threading.RLock":
    """取该会话键的独占锁（惰性创建）。"""
    with _SESSION_LOCK:
        return _SESSION_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def _session_guard(session_id: Optional[str], caller: str = "role:analyst"):
    """会话记忆 + 独占锁：同一 (caller, session_id) 的并发分析被串行化。

    session_id 为空 → 一次性无记忆会话（返回临时空 dict，无锁）。
    非空时在**持有独占锁期间**跑完本轮，结束时把记忆写回持久层。
    """
    mem = _session_memory(session_id, caller)
    if not session_id:
        yield mem
        return
    key = f"{caller}::{session_id}"
    lock = _get_session_lock(key)
    with lock:
        # 握锁期间该会话记忆对本请求独占，避免并发写撕裂
        _SESSION_SEEN[key] = time.time()
        try:
            yield mem
        finally:
            _session_persist(key, mem)


# ---------------- 分析内部复用 ----------------

def _analyze_run(q: "AnalyzeIn", role: str, caller: str = "role:analyst",
                 username: str = "") -> Dict:
    """非流式多 Agent 分析（/api/analyze/report 内部复用，避免重复实现）。"""
    from sqlpa.analysis.orchestrator import run_analysis
    with _session_guard(q.session_id, caller) as mem:
        return run_analysis(q.question, _cfg(), _sandbox(), _db_path(), _llm(),
                            role=role, username=username,
                            alert_threshold_pct=q.alert_threshold_pct,
                            emit=None, memory=mem)

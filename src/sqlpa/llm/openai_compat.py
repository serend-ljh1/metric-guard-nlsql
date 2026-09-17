"""
sqlpa.llm.openai_compat
=======================
针对 OpenAI 兼容接口的真实 LLM 客户端（DeepSeek / OpenAI / 任何兼容端点）。

无需第三方依赖：用标准库 urllib 直接 POST 到 {base_url}/chat/completions。

配置（.env / 环境变量）：
  LLM_API_BASE  默认 https://api.deepseek.com/v1
  LLM_API_KEY   必填（DEEPSEEK_API_KEY 或 OPENAI_API_KEY 兜底）
  LLM_MODEL     默认 deepseek-chat
  LLM_TEMPERATURE 默认 0.0（Text-to-SQL 要稳定）
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from typing import Dict, Optional
from .base import LLMProvider

try:
    from dotenv import load_dotenv as _ld
    _ld()
except Exception:  # noqa: BLE001  未装 python-dotenv 时依赖环境变量
    pass

DEFAULT_BASE = os.environ.get("LLM_API_BASE", "https://api.deepseek.com/v1")
DEFAULT_MODEL = os.environ.get("LLM_MODEL", "deepseek-chat")
DEFAULT_TEMP = float(os.environ.get("LLM_TEMPERATURE", "0.0"))

# 模型池：逗号分隔的模型名列表，按顺序尝试，前一个失败/空内容自动切换
_pool_env = os.environ.get("MODEL_POOL", "").strip()
DEFAULT_MODEL_POOL = (
    [m.strip() for m in _pool_env.split(",") if m.strip()]
    if _pool_env
    else [DEFAULT_MODEL]
)


def _is_retryable(msg: str) -> bool:
    """瞬态错误（可在当前模型上重试）：429 限流 / 超时 / 5xx / 连接错误。"""
    m = msg.lower()
    return any(k in m for k in
               ("429", "rate limit", "too many requests",
                "timeout", "timed out",
                "500", "502", "503", "504",
                "connection", "connectionreset"))


def _should_switch(msg: str) -> bool:
    """应切换到下一个模型：404/403/401/400 / 额度耗尽 / context 超限 / 模型过载 / 空内容。"""
    m = msg.lower()
    return any(k in m for k in
               ("404", "model not found", "model_not_found",
                "403", "insufficient_quota", "quota", "free quota",
                "401", "unauthorized", "invalid api key",
                "400", "bad request",
                "context_length_exceeded", "maximum context length",
                "overloaded", "model overloaded"))


class OpenAICompatLLM(LLMProvider):
    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None,
                 model: Optional[str] = None, temperature: float = DEFAULT_TEMP,
                 max_retries: int = 2, timeout: float = 60.0,
                 model_pool: Optional[list] = None):
        self.api_key = api_key or os.environ.get("LLM_API_KEY") \
            or os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY")
        if not self.api_key:
            raise ValueError("未提供 LLM API Key：请设置 LLM_API_KEY / DEEPSEEK_API_KEY / OPENAI_API_KEY")
        self.base_url = (base_url or DEFAULT_BASE).rstrip("/")
        self.model = model or DEFAULT_MODEL
        # 模型池：显式传入优先，否则取环境变量 MODEL_POOL，最后回退到单模型
        self.model_pool = model_pool if model_pool else list(DEFAULT_MODEL_POOL)
        self.temperature = temperature
        self.max_retries = max_retries
        self.timeout = timeout
        # ---- 用量/成本统计 ----
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        self.cost = 0.0
        # 计价(每 1M token)可通过 env 配置：LLM_INPUT_PRICE_PER_1M / LLM_OUTPUT_PRICE_PER_1M
        self.price_in = float(os.environ.get("LLM_INPUT_PRICE_PER_1M", "0.14"))    # 默认按 deepseek 大致表价
        self.price_out = float(os.environ.get("LLM_OUTPUT_PRICE_PER_1M", "0.28"))
        # 记录实际使用的模型名（便于诊断）
        self.last_used_model = self.model_pool[0] if self.model_pool else self.model

    def _acc_usage(self, u: Optional[Dict]) -> None:
        if not u:
            return
        self.usage["prompt_tokens"] += int(u.get("prompt_tokens", 0))
        self.usage["completion_tokens"] += int(u.get("completion_tokens", 0))
        self.usage["total_tokens"] += int(u.get("total_tokens", 0))
        self.cost += (int(u.get("prompt_tokens", 0)) / 1e6 * self.price_in
                      + int(u.get("completion_tokens", 0)) / 1e6 * self.price_out)

    def stats(self) -> Dict:
        return {"usage": dict(self.usage), "cost": round(self.cost, 4)}

    def last_model(self) -> str:
        """最近一次实际调用的模型名（用于发现模型池中途切换）。"""
        return str(getattr(self, "last_used_model", "") or "")

    def reset_stats(self) -> None:
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        self.cost = 0.0

    def _chat(self, messages: list, temperature: float | None = None) -> str:
        """模型池调用：按顺序尝试 model_pool 中的模型。

        - 对瞬态错误（429/超时/5xx/连接错误）在当前模型上指数退避重试；
        - 对不可重试错误（404/401/400/context超限/过载）或空内容，切换到下一个模型；
        - 所有模型都失败则抛出最后一次异常。
        """
        url = f"{self.base_url}/chat/completions"
        last_err: Optional[Exception] = None
        for model_name in self.model_pool:
            payload = {"model": model_name,
                       "messages": messages,
                       "temperature": self.temperature if temperature is None else temperature,
                       "stream": False}
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            for attempt in range(self.max_retries + 1):
                try:
                    req = urllib.request.Request(
                        url, data=body,
                        headers={"Content-Type": "application/json",
                                 "Authorization": f"Bearer {self.api_key}"})
                    with urllib.request.urlopen(req, timeout=self.timeout) as r:
                        data = json.loads(r.read().decode("utf-8"))
                    content = data["choices"][0]["message"]["content"]
                    if not content or not content.strip():
                        # 空内容视为失败，切换下一个模型
                        last_err = RuntimeError(f"模型 {model_name} 返回空内容")
                        break
                    self._acc_usage(data.get("usage"))
                    self.last_used_model = model_name
                    return content
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    msg = str(e)
                    # 可重试的瞬态错误：在当前模型上指数退避后重试
                    if attempt < self.max_retries and _is_retryable(msg):
                        time.sleep(2 ** attempt)
                        continue
                    # 不可重试或重试耗尽：跳出当前模型，切到下一个
                    break
            # 当前模型结束循环，短暂等待避免连续失败，继续下一个模型
            time.sleep(0.3)
        raise RuntimeError(f"LLM 调用失败（已尝试模型池 {self.model_pool}）: {last_err}")

    # ---- 面向 Text-to-SQL 的封装 ----
    def generate_sql(self, question: str, schema_text: str, plan: str = "",
                     previous_try: Optional[Dict] = None,
                     metric_constraint: str = "", dialect_hint: str = "") -> str:
        dialect = dialect_hint or "目标数据库是 SQLite，请生成 SQLite 可执行的查询。"
        sys = ("你是 Text-to-SQL 专家。只输出一条 SQL（不要 markdown/解释）。"
               f"基于给定 schema，从自然语言问题生成 SQL。{dialect}")
        ctx = f"【schema】\n{schema_text}\n\n【问题】{question}\n"
        if metric_constraint:
            ctx += f"\n【业务指标硬约束】{metric_constraint}\n（必须在 SELECT 中原样使用该计算表达式，不得改动；请围绕它构建完整的 FROM/JOIN/GROUP BY。)\n"
        if plan:
            ctx += f"\n【计划】{plan}\n"
        if previous_try:
            if previous_try.get("feedback"):
                ctx += (f"\n【评审意见】{previous_try['feedback']}\n"
                        f"请按该意见修改 SQL 后重新输出（保持 schema/表名一致）。\n")
            if previous_try.get("sql") or previous_try.get("error"):
                ctx += (f"\n【上一条 SQL】{previous_try.get('sql')}\n"
                        f"【执行报错】{previous_try.get('error')}\n请修复后重新输出。\n")
        return self._chat([{"role": "system", "content": sys},
                           {"role": "user", "content": ctx}]).strip()

    def review_sql(self, question: str, sql: str, schema_text: str,
                   exec_result: Optional[Dict] = None) -> Dict:
        sys = ("你是 SQL 评审专家（与写SQL者是不同角色）。独立审核给定 SQL 是否准确回答了问题，"
               "并给出可执行的修改意见。\n"
               "检查维度：① 语法/schema 是否正确 ② 语义是否匹配问题 ③ 是否违背数据口径/业务规则。\n"
               "只输出 JSON：{\"pass\": bool, \"issues\": [\"...\"], \"feedback\": \"<具体的修改建议>\"}。")
        ctx = (f"【问题】{question}\n【schema】\n{schema_text}\n【待审SQL】\n{sql}\n")
        if exec_result:
            ctx += f"【执行结果】{json.dumps(exec_result, ensure_ascii=False)[:2000]}\n"
        text = self._chat([{"role": "system", "content": sys},
                           {"role": "user", "content": ctx}], temperature=0.0)
        try:
            text = text.strip().strip("`").removeprefix("json").removeprefix("JSON")
            obj = json.loads(text)
            obj.setdefault("pass", True)
            obj.setdefault("issues", [])
            obj.setdefault("feedback", "")
            return obj
        except Exception:
            return {"pass": True, "issues": [], "feedback": ""}

    def diagnose_error(self, sql: str, error: str, schema_text: str) -> str:
        sys = ("你是 SQL 错误诊断专家。基于报错信息，指出根因并给出修复建议（简短）。")
        ctx = f"schema:\n{schema_text}\n\nSQL:\n{sql}\n\n报错:\n{error}\n"
        return self._chat([{"role": "system", "content": sys},
                           {"role": "user", "content": ctx}]).strip()

    def validate_semantics(self, question: str, sql: str, exec_result: Dict) -> Dict:
        sys = ("判断给定 SQL 的执行结果是否回答了用户问题。返回 JSON {\"valid\": bool, \"reason\": str}。")
        ctx = (f"问题:{question}\nSQL:{sql}\n执行结果(JSON):{json.dumps(exec_result, ensure_ascii=False)[:2000]}\n")
        text = self._chat([{"role": "system", "content": sys},
                           {"role": "user", "content": ctx}], temperature=0.0)
        try:
            text = text.strip().strip("`").removeprefix("json")
            return json.loads(text)
        except Exception:
            return {"valid": True, "reason": "parse-fallback"}

    def difficulty_judge(self, question: str, schema_text: str) -> str:
        sys = ("判断该 Text-to-SQL 问题难度：只输出 simple 或 complex。"
               "complex=需要跨表连接/聚合/排序/子查询等逻辑；simple=单表直接检索。")
        return self._chat([{"role": "system", "content": sys},
                           {"role": "user", "content": question}]).strip().lower()

    def complete(self, prompt: str) -> str:
        return self._chat([{"role": "user", "content": prompt}], temperature=0.0)

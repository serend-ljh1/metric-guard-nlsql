"""
sqlpa.llm.openai_compat
=======================
针对 OpenAI 兼容接口的真实 LLM 客户端（DeepSeek / OpenAI / 任何兼容端点）。

无需第三方依赖：用标准库 urllib 直接 POST 到 {base_url}/chat/completions。

配置（.env / 环境变量）：
  LLM_API_BASE  默认 https://api.deepseek.com/v1
  LLM_API_KEY   必填（DEEPSEEK_API_KEY 或 OPENAI_API_KEY 兜底）
  LLM_MODEL     默认 deepseek-chat
  LLM_TEMPERATURE 默认 0.0（判断/表述类调用需要稳定输出）
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

# 模型池：逗号分隔的模型名列表，按顺序尝试，前一个失败/空内容自动切换。
# 未设置 MODEL_POOL 环境变量时使用以下默认池（qwen3.8-27b 优先，其次 qwen3.8-max）。
_pool_env = os.environ.get("MODEL_POOL", "").strip()
DEFAULT_MODEL_POOL = (
    [m.strip() for m in _pool_env.split(",") if m.strip()]
    if _pool_env
    else [
        "qwen3.8-27b",
        "qwen3.8-max",
        "qwen3.8-flash",
        "deepseek-v4.1-flash",
        "qwen3.7-flash-2026-07-15",
        "qwen3.8-max-0902",
        "qwen3.8-2.4t-a95b",
        "glm-5.3",
    ]
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
        # 每次**成功**拿到内容算一次调用（含 token 与成本），供逐请求可观测性使用。
        self.usage["calls"] = int(self.usage.get("calls", 0)) + 1
        if not u:
            return
        self.usage["prompt_tokens"] += int(u.get("prompt_tokens", 0))
        self.usage["completion_tokens"] += int(u.get("completion_tokens", 0))
        self.usage["total_tokens"] += int(u.get("total_tokens", 0))
        self.cost += (int(u.get("prompt_tokens", 0)) / 1e6 * self.price_in
                      + int(u.get("completion_tokens", 0)) / 1e6 * self.price_out)

    def stats(self) -> Dict:
        # 成本保留 6 位：单次请求常在 1e-4 量级，round(…, 4) 会把小额成本抹成 0
        return {"usage": dict(self.usage), "cost": round(self.cost, 6),
                "last_model": self.last_model()}

    def last_model(self) -> str:
        """最近一次实际调用的模型名（用于发现模型池中途切换）。"""
        return str(getattr(self, "last_used_model", "") or "")

    def reset_stats(self) -> None:
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0,
                      "total_tokens": 0, "calls": 0}
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

    def complete(self, prompt: str) -> str:
        return self._chat([{"role": "user", "content": prompt}], temperature=0.0)

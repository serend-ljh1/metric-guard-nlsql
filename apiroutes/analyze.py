"""apiroutes.analyze：多 Agent 分析类端点（非流式 / SSE 流式 / 图表模板 / 报告导出）。"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import APIRouter, Header, HTTPException

from .deps import (AnalyzeIn, ROOT, _bind_asof, _cfg, _db_path, _identify, _llm,
                   _rate_limit, _sandbox, _session_guard)

router = APIRouter()


@router.post("/api/analyze")
def api_analyze(q: AnalyzeIn, x_api_token: Optional[str] = Header(default=None)):
    """多 Agent 协作分析（**非流式**聚合返回，供脚本/测试/预览使用）。

    与 /api/analyze/stream 同一套编排，只是把流式事件收集成一个数组返回。
    """
    from sqlpa.analysis.orchestrator import run_analysis
    _bind_asof()
    ident = _identify(x_api_token, q.role)
    role, caller, username = ident["role"], ident["caller"], ident["username"]
    _rate_limit(caller, "analyze")
    events: List[Dict] = []

    def _emit(p):
        events.append(p)

    with _session_guard(q.session_id, caller) as mem:
        final = run_analysis(q.question, _cfg(), _sandbox(), _db_path(), _llm(),
                             role=role, username=username,
                             alert_threshold_pct=q.alert_threshold_pct,
                             emit=_emit,
                             confirm_id=q.confirm_id,
                             memory=mem)
    return {"events": events, "final": final, "effective_role": role}


@router.post("/api/analyze/stream")
def api_analyze_stream(q: AnalyzeIn, x_api_token: Optional[str] = Header(default=None)):
    """多 Agent 协作分析（**SSE 流式**）：逐 Agent 事件推给前端。

    事件格式（SSE data: JSON 一行一条）：
      session_start / agent_start / agent_step / agent_done / done / error
    前端消费后渲染 Agent 执行面板 + 炫的可视化 + 决策闭环。
    """
    from sqlpa.analysis.orchestrator import run_analysis
    from fastapi.responses import StreamingResponse
    import queue, threading

    ident = _identify(x_api_token, q.role)
    role, caller, username = ident["role"], ident["caller"], ident["username"]
    _rate_limit(caller, "analyze")
    qq: "queue.Queue[Dict]" = queue.Queue()

    def _emit(p):
        qq.put(p)

    def _run():
        """跑编排并把**任何异常**翻译成终帧事件。

        此前线程目标没有 try/except：任一节点抛异常就静默死线程，客户端既不收到
        done 也收不到 error，只能干等 300 秒后拿到一句假的"分析超时"。
        """
        try:
            _bind_asof()
            # 会话记忆的独占锁在**本线程内**持有到分析结束，保证并发同会话不撕裂
            with _session_guard(q.session_id, caller) as mem:
                run_analysis(
                    question=q.question, cfg=_cfg(), sb=_sandbox(), db_path=_db_path(),
                    llm=_llm(), role=role, username=username,
                    alert_threshold_pct=q.alert_threshold_pct, emit=_emit,
                    confirm_id=q.confirm_id,
                    memory=mem)
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            qq.put({"type": "error", "detail": f"分析执行失败: {type(e).__name__}: {e}"})
            qq.put({"type": "done", "payload": {"ok": False,
                                                "error": f"{type(e).__name__}: {e}"}})

    def _stream():
        yield "data: " + json.dumps({"type": "session_start",
                                     "question": q.question}) + "\n\n"
        t = threading.Thread(target=_run, daemon=True)
        t.start()
        while True:
            try:
                p = qq.get(timeout=300)
            except queue.Empty:
                yield "data: " + json.dumps({"type": "error",
                                             "detail": "分析超时"}) + "\n\n"
                break
            yield "data: " + json.dumps(p, ensure_ascii=False) + "\n\n"
            if p.get("type") == "done":
                break

    return StreamingResponse(_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "Connection": "keep-alive",
                                      "X-Accel-Buffering": "no"})


@router.get("/api/analyze/traces")
def api_analyze_traces(limit: int = 20, x_api_token: Optional[str] = Header(default=None)):
    """最近的分析 trace 摘要列表（审计入口）。analyst 只看自己的，admin 看全部。"""
    from sqlpa.business import storage
    ident = _identify(x_api_token, "analyst")
    only = None if ident["role"] == "admin" else ident["username"]
    return {"traces": storage.list_traces(limit=max(1, min(int(limit), 200)), username=only)}


@router.get("/api/analyze/trace/{analysis_id}")
def api_analyze_trace(analysis_id: str, x_api_token: Optional[str] = Header(default=None)):
    """按 analysis_id 取回**完整事件流水**（结构化），供前端/工具重放当时的多 Agent 过程。

    重放语义：返回的就是当初推给 SSE 的同一串事件 + 最终载荷，前端用同一套渲染器
    再放一遍即可；因此"过程可看"升级成了"过程可审计、可重放"。
    """
    from sqlpa.business import storage
    ident = _identify(x_api_token, "analyst")
    t = storage.get_trace(analysis_id)
    if not t:
        raise HTTPException(status_code=404, detail="未找到该 analysis_id 的 trace")
    if ident["role"] != "admin" and (t.get("username") or "") not in ("", ident["username"]):
        raise HTTPException(status_code=403, detail="只能重放自己的分析 trace")
    return t


@router.get("/api/analyze/chart-template")
def api_analyze_chart_template():
    """ECharts 可视化模板说明：前端据此渲染归因瀑布/主因占比/下钻树/因子占比。"""
    return {"summary": "归因瀑布(waterfall)/主因占比(share)/下钻树(tree)/因子占比(factors) 见 /api/analyze 返回的 final.chart"}


@router.post("/api/analyze/report")
def api_analyze_report(q: AnalyzeIn, download: Optional[str] = None,
                       x_api_token: Optional[str] = Header(default=None)):
    """导出「多 Agent 分析」报告：Markdown（内联）+ Word(.docx)。

    前端把刚拿到的 `final` 一并回传（q.final）即可**零成本**出报告——不重跑分析、
    不再调用 LLM；不带 final 时按 question 重跑一次分析（兼容脚本/测试用法）。

    `download=docx` 时直接返回 **Word 文件流**（FileResponse，正确 MIME），
    不再把二进制 base64 塞进 JSON（base64 会把响应体积放大 ~33%，且前端还需解码）。
    """
    from fastapi.responses import FileResponse
    from sqlpa.business.deliver import (export_analysis_report,
                                        export_analysis_report_docx)
    from .deps import _analyze_run
    ident = _identify(x_api_token, q.role)
    role, caller, username = ident["role"], ident["caller"], ident["username"]
    _rate_limit(caller, "report")
    if isinstance(q.final, dict) and q.final:
        final = q.final
    else:
        _bind_asof()
        final = _analyze_run(q, role, caller, username)
    if not final.get("ok"):
        return {"ok": False, "reject": final.get("reject") or "分析未完成，无法生成报告"}
    out_dir = Path(os.environ.get("SQLPA_REPORT_DIR") or (ROOT / "data" / "reports"))
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    md_path = out_dir / f"analysis-{stamp}.md"
    docx_path = out_dir / f"analysis-{stamp}.docx"
    export_analysis_report(md_path, q.question, final, username=username)
    export_analysis_report_docx(docx_path, q.question, final, username=username)
    md = md_path.read_text(encoding="utf-8")
    if (download or "").lower() in ("docx", "word"):
        return FileResponse(
            str(docx_path),
            media_type=("application/vnd.openxmlformats-officedocument."
                        "wordprocessingml.document"),
            filename=docx_path.name)
    return {"ok": True, "report": md, "markdown": md, "filename": docx_path.name}

# 多 Agent 协作数据分析系统 —— 容器化（FastAPI 后端）
# 构建:  docker build -t sqlpa .
# 运行:  docker run -p 8000:8000 --env-file .env sqlpa      （/docs 有 Swagger）
#       或 docker compose up                                （api:8000）
#
# 说明：前端是 Vue3 + Vite（web/），开发期用 `npm run dev`（5173）并代理到本服务；
# 容器里只跑 API。历史上这里曾用 Streamlit（app.py），该前端已下线。
FROM python:3.11-slim

WORKDIR /app

# 先装依赖（利用缓存层）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 拷贝代码
COPY . .

# 预构建 Olist 同结构样本库（离线可跑；挂载真实 olist.db 可覆盖）
RUN python tools/build_olist_sample.py

# FastAPI 后端
EXPOSE 8000

CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]

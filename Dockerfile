# 多智能体 Text-to-SQL 智能取数产品 —— 一键容器化
# 构建:  docker build -t sqlpa .
# 运行:  docker compose up  （web:8501 前端 + api:8000 后端）
FROM python:3.11-slim

WORKDIR /app

# 先装依赖（利用缓存层）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 拷贝代码
COPY . .

# 预构建 Olist 同结构样本库（离线可跑；挂载真实 olist.db 可覆盖）
RUN python tools/build_olist_sample.py

# Streamlit 前端 8501 / FastAPI 后端 8000
EXPOSE 8501 8000

# 默认起前端；compose 里分别起 web / api
CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0", "--server.headless=true"]

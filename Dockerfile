FROM python:3.12-slim

WORKDIR /app

# 构建镜像时安装依赖（容器启动命令中也会再校验一次）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000

# 启动服务；建表由应用启动钩子（lifespan）完成
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

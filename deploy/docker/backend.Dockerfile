# backend 镜像：多阶段（builder 装依赖 → 运行层只拷产物）
# build context = 仓库根（compose 里指定），路径带 backend/ 前缀
# PIP_INDEX_URL：国内构建经 compose build.args 注入镜像源（如清华 tuna），默认官方
FROM python:3.11-slim AS builder
ARG PIP_INDEX_URL=https://pypi.org/simple
WORKDIR /build
COPY backend/requirements.txt .
RUN pip install --no-cache-dir --index-url "$PIP_INDEX_URL" --prefix=/install -r requirements.txt

FROM python:3.11-slim
WORKDIR /app
COPY --from=builder /install /usr/local
COPY backend/ .
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]

#!/bin/sh
# Docker 回归测试：在容器（python:3.12-slim + dev 依赖）内运行完整 pytest 套件。
#
# 用法：
#   ./scripts/docker-test.sh                 # 运行全部测试
#   ./scripts/docker-test.sh -k clone        # 只跑名称匹配 clone 的测试
#   ./scripts/docker-test.sh tests/test_api.py::test_clone_unknown_source_returns_404
#
# 测试使用容器内独立的临时数据库，不触碰 api 服务的 crossover-data 数据卷。
set -eu

cd "$(dirname "$0")/.."

# 无参数时使用镜像默认命令（pytest -q）；有参数时作为 pytest 的参数透传。
# --build 保证代码变更后镜像总是重建；--rm 清理退出的测试容器。
if [ "$#" -eq 0 ]; then
  exec docker compose --profile test run --build --rm tests
fi

exec docker compose --profile test run --build --rm tests pytest -q "$@"

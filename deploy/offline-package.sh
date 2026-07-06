#!/bin/bash
# 将 Jaeger 镜像打包为离线 tar 文件，用于无网络环境部署
# 在有网络的机器上执行此脚本

set -e

IMAGE="jaegertracing/all-in-one:1.57"
OUTPUT="jaeger-offline.tar"

echo "拉取镜像 $IMAGE ..."
docker pull "$IMAGE"

echo "导出镜像到 $OUTPUT ..."
docker save "$IMAGE" -o "$OUTPUT"

echo "完成。文件大小："
du -sh "$OUTPUT"

echo ""
echo "将以下文件复制到目标机器："
echo "  - $OUTPUT"
echo "  - docker-compose.yml"
echo ""
echo "在目标机器上执行："
echo "  docker load -i $OUTPUT"
echo "  docker compose up -d"
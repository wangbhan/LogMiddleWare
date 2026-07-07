#!/bin/bash
# 将 Tempo + Grafana 镜像打包为离线 tar 文件，用于无网络环境部署
# 在有网络的机器上执行此脚本

set -e

TEMPO_IMAGE="grafana/tempo:2.5.0"
GRAFANA_IMAGE="grafana/grafana:11.0.0"
OUTPUT="tempo-grafana-offline.tar"

echo "拉取镜像..."
docker pull "$TEMPO_IMAGE"
docker pull "$GRAFANA_IMAGE"

echo "导出镜像到 $OUTPUT ..."
docker save "$TEMPO_IMAGE" "$GRAFANA_IMAGE" -o "$OUTPUT"

echo "完成。文件大小："
du -sh "$OUTPUT"

echo ""
echo "将以下文件复制到目标机器："
echo "  - $OUTPUT"
echo "  - docker-compose.tempo.yml"
echo "  - tempo.yaml"
echo "  - grafana/datasources.yaml"
echo ""
echo "在目标机器上执行："
echo "  docker load -i $OUTPUT"
echo "  docker compose -f docker-compose.tempo.yml up -d"
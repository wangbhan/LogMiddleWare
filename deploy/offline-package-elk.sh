#!/bin/bash
# 将 ELK 镜像打包为离线 tar 文件，用于无网络环境部署
# 在有网络的机器上执行此脚本

set -e

ES_IMAGE="elasticsearch:8.13.0"
KIBANA_IMAGE="kibana:8.13.0"
FILEBEAT_IMAGE="elastic/filebeat:8.13.0"
OUTPUT="elk-offline.tar"

echo "拉取镜像..."
docker pull "$ES_IMAGE"
docker pull "$KIBANA_IMAGE"
docker pull "$FILEBEAT_IMAGE"

echo "导出镜像到 $OUTPUT ..."
docker save "$ES_IMAGE" "$KIBANA_IMAGE" "$FILEBEAT_IMAGE" -o "$OUTPUT"

echo "完成。文件大小："
du -sh "$OUTPUT"

echo ""
echo "将以下文件复制到目标机器："
echo "  - $OUTPUT"
echo "  - docker-compose.elk.yml"
echo "  - filebeat.yml"
echo ""
echo "在目标机器上执行："
echo "  docker load -i $OUTPUT"
echo "  docker compose -f docker-compose.elk.yml up -d"

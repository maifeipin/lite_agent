#!/bin/bash
# 自动备份 Halo 和 Lite Agent 目录到百度网盘
# 运行时间: 每天凌晨 02:00

export PATH=$PATH:/usr/local/bin:/usr/bin:/bin

echo "========== 开始执行百度网盘备份 $(date '+%Y-%m-%d %H:%M:%S') =========="

# 1. 确保远端根目录存在
echo "-> 尝试创建远端目录 lite_agent, lite_agent/halo, lite_agent/project..."
bypy mkdir lite_agent || true
bypy mkdir lite_agent/halo || true
bypy mkdir lite_agent/project || true

# 2. 备份 Halo 数据目录
echo "-> 开始增量同步 /root/.halo 到 lite_agent/halo ..."
bypy syncup /root/.halo lite_agent/halo

# 3. 备份 Lite Agent 项目目录
echo "-> 开始增量同步 /root/lite_agent 到 lite_agent/project ..."
# 同步项目时排除可能的缓存目录或大体积日志
bypy syncup /root/lite_agent lite_agent/project

echo "========== 备份结束 $(date '+%Y-%m-%d %H:%M:%S') =========="

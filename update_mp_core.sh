#!/bin/bash

# 强制脚本遇到错误时立刻退出
set -e

echo "========================================================="
echo "🚀 MoviePilot 内核更新器 【CloakBrowser 官方纯血同步版】"
echo "========================================================="

# 设置默认的 docker 基础路径
DEFAULT_DOCKER_PATH="${DOCKER_PATH}"

# 1. 引导用户输入，直接回车则使用默认值
read -p "📂 请输入你的 docker 根目录路径 [默认: $DEFAULT_DOCKER_PATH]: " USER_INPUT

# 如果用户直接敲回车（输入为空），则使用默认路径
BASE_PATH="${USER_INPUT:-$DEFAULT_DOCKER_PATH}"

# 移除用户可能多打的末尾斜杠
BASE_PATH="${BASE_PATH%/}"

# 2. 自动化拼装完整的 MoviePilot core 挂载路径
TARGET_PATH="${BASE_PATH}/moviepilot-v2/core"

echo "🎯 最终锁定的内核完整路径为: $TARGET_PATH"

# 3. 核心自动化：如果完整路径不存在，自动创建
if [ ! -d "$TARGET_PATH" ]; then
    echo "📁 检测到目标路径不存在，正在为你全自动创建目录结构..."
    mkdir -p "$TARGET_PATH"
fi

# 4. 自行判断当前机器的 CPU 架构
ARCH=$(uname -m)
case "$ARCH" in
    x86_64)
        MP_ARCH="amd64"
        echo "💻 检测到当前机器为: x86_64 (Intel/AMD) 架构"
        ;;
    aarch64|arm64)
        MP_ARCH="arm64"
        echo "📱 检测到当前机器为: ARM64 架构"
        ;;
    *)
        echo "❌ 错误：不支持的 CPU 架构: $ARCH"
        exit 1
        ;;
esac

# ==========================================
# GitHub 加速镜像配置（失效时只需修改此处，留空则直连）
# ==========================================
GITHUB_PROXY="https://ghfast.top"

# 5. 拼接 GitHub Release Assets 下载直链
DOWNLOAD_URL="https://github.com/warptr/docker/releases/download/v2-latest/mp-core-all-${MP_ARCH}.tar.gz"

echo "--------------------------------------------------------"
echo "🧹 正在深度清理旧内核及隐藏残渣..."
cd "$TARGET_PATH"
# 精准清除旧文件及以 . 开头的隐藏目录
rm -rf ..?* .[!.]* * || true

echo "📥 正在下载最新官方原厂包..."
# 先试加速，失败则回退直连
if [ -n "$GITHUB_PROXY" ]; then
    PROXY_URL="${GITHUB_PROXY}/${DOWNLOAD_URL#https://}"
    echo "⚡ 尝试加速下载..."
    wget --no-cache --timeout=15 -q -O mp-core-all.tar.gz "$PROXY_URL" 2>/dev/null || true
    if [ ! -s mp-core-all.tar.gz ]; then
        echo "⚠️ 加速失败，回退直连..."
        rm -f mp-core-all.tar.gz
        wget --no-cache --no-cookies --no-dns-cache -q --show-progress "$DOWNLOAD_URL" -O mp-core-all.tar.gz
    else
        echo "✅ 加速下载成功！"
    fi
else
    wget --no-cache --no-cookies --no-dns-cache -q --show-progress "$DOWNLOAD_URL" -O mp-core-all.tar.gz
fi

echo "📦 正在标准解压并释放原生目录树..."
tar -xzpf mp-core-all.tar.gz
rm mp-core-all.tar.gz

echo "🔒 正在强力注入本地可执行授权..."
chmod -R +x .

echo "--------------------------------------------------------"
echo "✅ 大功告成！CloakBrowser 官方原厂内核已完美注入路径: $TARGET_PATH"
echo "🐳 飞牛后台 Docker 挂载请确保为: ${DOCKER_PATH}/moviepilot-v2/core ➡️ /moviepilot/.cloakbrowser"
echo "🚀 现在你可以放心无脑启动或重启你的 MoviePilot 容器了！"
echo "========================================================="
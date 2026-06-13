#!/bin/bash

echo "======================================="
echo "  NAS Docker 交互式纯净部署脚本"
echo "======================================="
echo "正在连接极狐 GitLab 获取分支列表..."

# 极狐 GitLab 官方 API 路径
API_URL="https://jihulab.com/api/v4/projects/tmzg0000%2Fdocker/repository/branches"

if command -v curl &> /dev/null; then
    RESPONSE=$(curl -s "$API_URL")
else
    RESPONSE=$(wget -qO- "$API_URL")
fi

if [ -z "$RESPONSE" ]; then
    echo "❌ 获取分支列表失败，请检查网络或仓库权限。"
    exit 1
fi

# 提取分支名称
BRANCHES=($(echo "$RESPONSE" | grep -o '"name":"[^"]*"' | awk -F'"' '{print $4}'))

if [ ${#BRANCHES[@]} -eq 0 ]; then
    echo "❌ 未能解析到任何分支，退出。"
    exit 1
fi

echo "请选择你要部署的分支："
for i in "${!BRANCHES[@]}"; do
    echo "  $((i+1))) ${BRANCHES[$i]}"
done
echo "---------------------------------------"
read -p "请输入对应数字并回车: " choice

if ! [[ "$choice" =~ ^[0-9]+$ ]] || [ "$choice" -lt 1 ] || [ "$choice" -gt "${#BRANCHES[@]}" ]; then
    echo "❌ 输入无效，脚本终止。"
    exit 1
fi

SELECTED_BRANCH="${BRANCHES[$((choice-1))]}"
echo -e "\n-> 已选择分支: $SELECTED_BRANCH，开始准备部署..."

# ==========================================
# 1. 交互式配置环境变量
# ==========================================
echo "======================================="
echo "  配置环境变量 (直接回车使用默认值)"
echo "======================================="
read -p "请输入 DOCKER_PATH [默认: /share/Container]: " INPUT_DOCKER
DOCKER_PATH=${INPUT_DOCKER:-/share/Container}

read -p "请输入 MEDIA_PATH  [默认: /share/media]: " INPUT_MEDIA
MEDIA_PATH=${INPUT_MEDIA:-/share/media}

echo "-> 最终 DOCKER_PATH: $DOCKER_PATH"
echo "-> 最终 MEDIA_PATH:  $MEDIA_PATH"
echo "======================================="
echo "📂 正在进入目标部署路径..."
mkdir -p "$DOCKER_PATH"
cd "$DOCKER_PATH" || { echo "❌ 无法进入目录 $DOCKER_PATH，请检查权限！"; exit 1; }
# ==========================================
# 2. 下载临时 ZIP 包
# ==========================================
ZIP_URL="https://jihulab.com/tmzg0000/docker/-/archive/${SELECTED_BRANCH}/docker-${SELECTED_BRANCH}.zip"
ZIP_NAME="temp_archive.zip"
TMP_DIR="temp_extract_dir"

echo "📦 正在下载分支代码..."
if command -v wget &> /dev/null; then
    wget -q --show-progress -O $ZIP_NAME "$ZIP_URL"
else
    curl -L -o $ZIP_NAME "$ZIP_URL"
fi

if [ ! -f "$ZIP_NAME" ]; then
    echo "❌ 下载失败，请重试。"
    exit 1
fi

# ==========================================
# 3. 解压并精准提取/释放文件
# ==========================================
echo "📂 正在解析安装包..."
mkdir -p "$TMP_DIR"

if command -v unzip >/dev/null 2>&1; then
    unzip -qo "$ZIP_NAME" -d "$TMP_DIR"
elif command -v 7zz >/dev/null 2>&1; then
    7zz x "$ZIP_NAME" -o"$TMP_DIR" -y >/dev/null
elif command -v 7z >/dev/null 2>&1; then
    7z x "$ZIP_NAME" -o"$TMP_DIR" -y >/dev/null
else
    echo "❌ 未找到可用解压工具"
    exit 1
fi

# 获取极狐解压后的真实根目录
EXTRACTED_ROOT=$(ls -d $TMP_DIR/*/)

# 🌟 新增核心逻辑：解压项目根目录里的 media.zip 到用户指定的 MEDIA_PATH
if [ -f "${EXTRACTED_ROOT}media.zip" ]; then
    echo "======================================="
    echo "📦 检测到媒体预设包 media.zip，开始释放到媒体目录..."
    echo "-> 目标媒体路径: $MEDIA_PATH"

    # 确保宿主机的 MEDIA_PATH 物理目录存在
    mkdir -p "$MEDIA_PATH"

    # 将临时目录中的 media.zip 静默解压到指定的 MEDIA_PATH
    unzip -qo "${EXTRACTED_ROOT}media.zip" -d "$MEDIA_PATH"

    echo "📦 媒体预设包释放完毕！"
    echo "======================================="
else
    echo "提示：未在项目根目录检测到 media.zip，跳过媒体初始化。"
fi

# 提取 docker 目录下的所有子配置到当前 DOCKER_PATH
if [ -d "${EXTRACTED_ROOT}docker" ]; then
    echo "📂 正在提取容器配置文件..."
    cp -r ${EXTRACTED_ROOT}docker/* .
fi

# 提取核心脚本到当前 DOCKER_PATH
if [ -f "${EXTRACTED_ROOT}update_mp_core.sh" ]; then
    cp ${EXTRACTED_ROOT}update_mp_core.sh .
fi

# 阅后即焚：清理下载的临时压缩包和解压目录
rm -rf $TMP_DIR $ZIP_NAME
echo "🧹 临时安装包已清理完毕！"

# ==========================================
# 4. 运行浏览器核心更新脚本
# ==========================================
if [ -f "update_mp_core.sh" ]; then
    echo "======================================="
    echo "⚙️ 检测到 update_mp_core.sh，准备执行核心环境初始化..."
    chmod +x update_mp_core.sh
    echo "$DOCKER_PATH" | bash ./update_mp_core.sh
    rm -f update_mp_core.sh
    echo "⚙️ 浏览器核心初始化脚本执行完毕！"
    echo "======================================="
else
    echo "⚠️ 未找到 update_mp_core.sh，跳过初始化。"
fi

# ==========================================
# 5. 动态定位、生成 .env 并启动容器
# ==========================================
echo "🚀 正在启动 Docker 容器..."

if [ -d "moviepilot-v2" ] && [ -f "moviepilot-v2/docker-compose.yml" ]; then
    cd moviepilot-v2

    # 将用户输入的变量动态写入 docker-compose.yml 同级目录下的 .env 文件
    echo "DOCKER_PATH=$DOCKER_PATH" > .env
    echo "MEDIA_PATH=$MEDIA_PATH" >> .env
    echo "📝 已在 moviepilot-v2 子目录下动态生成 .env 环境变量文件"

    if command -v docker-compose &> /dev/null; then
        docker-compose up -d
    else
        docker compose up -d
    fi
    cd ..
else
    echo "❌ 启动失败：未能在 moviepilot-v2 目录下找到 docker-compose.yml 文件！"
    exit 1
fi

echo "======================================="
echo "✅ 部署大功告成！当前运行分支：$SELECTED_BRANCH"
echo "======================================="
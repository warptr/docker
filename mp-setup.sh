#!/bin/bash

echo "======================================="
echo "  NAS Docker 纯净提取部署脚本"
echo "======================================="
echo "正在连接极狐 GitLab 获取分支列表..."

API_URL="https://jihulab.com/api/v4/projects/tmzg0000%2Fdocker/repository/branches"

if command -v curl &> /dev/null; then
    RESPONSE=$(curl -s "$API_URL")
else
    RESPONSE=$(wget -qO- "$API_URL")
fi

if [ -z "$RESPONSE" ]; then
    echo "获取分支列表失败，请检查网络或仓库权限。"
    exit 1
fi

BRANCHES=($(echo "$RESPONSE" | grep -o '"name":"[^"]*"' | awk -F'"' '{print $4}'))

if [ ${#BRANCHES[@]} -eq 0 ]; then
    echo "未能解析到任何分支，退出。"
    exit 1
fi

echo "请选择你要部署的分支："
for i in "${!BRANCHES[@]}"; do
    echo "  $((i+1))) ${BRANCHES[$i]}"
done
echo "---------------------------------------"
read -p "请输入对应数字并回车: " choice

if ! [[ "$choice" =~ ^[0-9]+$ ]] || [ "$choice" -lt 1 ] || [ "$choice" -gt "${#BRANCHES[@]}" ]; then
    echo "输入无效，脚本终止。"
    exit 1
fi

SELECTED_BRANCH="${BRANCHES[$((choice-1))]}"
echo -e "\n-> 已选择分支: $SELECTED_BRANCH，开始精准部署..."

# ==========================================
# 1. 下载临时 ZIP 包
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
# 2. 精准提取文件并保持当前目录干净
# ==========================================
echo "📂 正在提取 docker 文件夹和核心脚本..."
mkdir -p $TMP_DIR
unzip -qo $ZIP_NAME -d $TMP_DIR

EXTRACTED_ROOT=$(ls -d $TMP_DIR/*/)

if [ -d "${EXTRACTED_ROOT}docker" ]; then
    cp -r ${EXTRACTED_ROOT}docker/* .
fi

if [ -f "${EXTRACTED_ROOT}update_mp_core.sh" ]; then
    cp ${EXTRACTED_ROOT}update_mp_core.sh .
fi

rm -rf $TMP_DIR $ZIP_NAME
echo "🧹 临时文件已清理完毕！"

# ==========================================
# 3. 运行浏览器核心更新脚本
# ==========================================
if [ -f "update_mp_core.sh" ]; then
    echo "======================================="
    echo "⚙️ 检测到 update_mp_core.sh，准备执行核心环境初始化..."
    chmod +x update_mp_core.sh
    bash ./update_mp_core.sh
    echo "⚙️ 初始化脚本执行完毕！"
    echo "======================================="
else
    echo "⚠️ 未找到 update_mp_core.sh，跳过初始化。"
fi

# ==========================================
# 4. 启动容器 (精准定位到 moviepilot-v2 目录)
# ==========================================
echo "🚀 正在启动 Docker 容器..."

# 检查目标文件夹和配置文件是否存在
if [ -d "moviepilot-v2" ] && [ -f "moviepilot-v2/docker-compose.yml" ]; then
    # 进入子目录
    cd moviepilot-v2

    # 执行启动命令
    if command -v docker-compose &> /dev/null; then
        docker-compose up -d
    else
        docker compose up -d
    fi

    # 执行完毕后退回到外层目录，保持环境整洁
    cd ..
else
    echo "❌ 启动失败：未能在 moviepilot-v2 目录下找到 docker-compose.yml 文件！"
    exit 1
fi

echo "======================================="
echo "✅ 部署大功告成！当前运行分支：$SELECTED_BRANCH"
echo "======================================="
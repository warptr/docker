#!/bin/bash

echo "======================================="
echo "  NAS Docker 动态分支拉取部署脚本"
echo "======================================="
echo "正在连接极狐 GitLab 获取分支列表..."

# 你的仓库 API 地址（tmzg0000%2Fdocker 是 URL 编码后的项目路径）
API_URL="https://jihulab.com/api/v4/projects/tmzg0000%2Fdocker/repository/branches"

# 使用 curl 或 wget 获取分支数据
if command -v curl &> /dev/null; then
    RESPONSE=$(curl -s "$API_URL")
else
    RESPONSE=$(wget -qO- "$API_URL")
fi

if [ -z "$RESPONSE" ]; then
    echo "获取分支列表失败，请检查网络或仓库权限。"
    exit 1
fi

# 裸机解析 JSON 提取分支名称 (兼容不支持 jq 的 NAS)
# 先提取所有 "name":"分支名"，然后截取出分支名保存到数组
BRANCHES=($(echo "$RESPONSE" | grep -o '"name":"[^"]*"' | awk -F'"' '{print $4}'))

if [ ${#BRANCHES[@]} -eq 0 ]; then
    echo "未能解析到任何分支，退出。"
    exit 1
fi

echo "请选择你要部署的分支："
# 动态打印分支列表
for i in "${!BRANCHES[@]}"; do
    echo "  $((i+1))) ${BRANCHES[$i]}"
done

echo "---------------------------------------"
read -p "请输入对应数字并回车: " choice

# 验证输入是否为合法数字
if ! [[ "$choice" =~ ^[0-9]+$ ]] || [ "$choice" -lt 1 ] || [ "$choice" -gt "${#BRANCHES[@]}" ]; then
    echo "输入无效，脚本终止。"
    exit 1
fi

# 数组索引比选项数字小 1
SELECTED_BRANCH="${BRANCHES[$((choice-1))]}"

echo ""
echo "-> 已选择分支: $SELECTED_BRANCH，开始部署..."

# 变量初始化
REPO_BASE="https://jihulab.com/tmzg0000/docker"
ZIP_URL="${REPO_BASE}/-/archive/${SELECTED_BRANCH}/docker-${SELECTED_BRANCH}.zip"
ZIP_NAME="docker_archive.zip"
EXTRACT_DIR="docker-${SELECTED_BRANCH}"

# 1. 下载
echo "正在下载代码包..."
if command -v wget &> /dev/null; then
    wget -q --show-progress -O $ZIP_NAME "$ZIP_URL"
else
    curl -L -o $ZIP_NAME "$ZIP_URL"
fi

if [ ! -f "$ZIP_NAME" ]; then
    echo "下载失败，请重试。"
    exit 1
fi

# 2. 解压与覆盖
echo "正在解压并覆盖本地配置..."
unzip -qo $ZIP_NAME

cp -r ${EXTRACT_DIR}/* .
rm -rf ${EXTRACT_DIR} $ZIP_NAME

# 3. 启动容器
echo "正在启动 Docker 容器..."
if command -v docker-compose &> /dev/null; then
    docker-compose up -d
else
    docker compose up -d
fi

echo "======================================="
echo "部署完成！当前运行分支：$SELECTED_BRANCH"
echo "======================================="
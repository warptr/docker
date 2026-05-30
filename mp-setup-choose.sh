#!/bin/bash

echo "======================================="
echo "  NAS Docker 多分支部署引导器"
echo "======================================="
echo "正在连接极狐 GitLab 获取分支列表..."

# 极狐 GitLab 官方 API 路径
API_URL="https://jihulab.com/api/v4/projects/tmzg0000%2Fdocker/repository/branches"

# 获取 API 响应
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

# 打印交互菜单
echo "请选择你要部署的分支："
for i in "${!BRANCHES[@]}"; do
    echo "  $((i+1))) ${BRANCHES[$i]}"
done
echo "---------------------------------------"
read -p "请输入对应数字并回车: " choice

# 校验用户输入
if ! [[ "$choice" =~ ^[0-9]+$ ]] || [ "$choice" -lt 1 ] || [ "$choice" -gt "${#BRANCHES[@]}" ]; then
    echo "❌ 输入无效，脚本终止。"
    exit 1
fi

SELECTED_BRANCH="${BRANCHES[$((choice-1))]}"
echo -e "\n-> 🎯 已选择分支: $SELECTED_BRANCH"

# ==========================================
# 核心：拉取并直接执行目标分支的纯净部署脚本
# ==========================================
TARGET_SCRIPT_URL="https://jihulab.com/tmzg0000/docker/-/raw/${SELECTED_BRANCH}/mp-setup.sh"

echo "🔄 正在呼叫 [$SELECTED_BRANCH] 分支的专属安装脚本..."
echo "======================================="

# 无缝衔接执行子脚本
if command -v curl &> /dev/null; then
    bash <(curl -sSL "$TARGET_SCRIPT_URL")
else
    bash <(wget -qO- "$TARGET_SCRIPT_URL")
fi
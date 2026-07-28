#!/bin/bash

echo "======================================="
echo "  NAS Docker 多分支部署引导器"
echo "======================================="

# ==========================================
# GitHub 加速镜像配置（失效时只需修改此处，留空则直连）
# ==========================================
GITHUB_PROXY="https://ghfast.top"

echo "正在连接 GitHub 获取分支列表..."

# GitHub 官方 API 路径
API_URL="https://api.github.com/repos/warptr/docker/branches"

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

# 提取分支名称（去除空格以兼容 GitHub API 的 JSON 格式）
BRANCHES=($(echo "$RESPONSE" | tr -d ' ' | grep -o '"name":"[^"]*"' | awk -F'"' '{print $4}'))

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
TARGET_SCRIPT_URL="https://raw.githubusercontent.com/warptr/docker/${SELECTED_BRANCH}/mp-setup.sh"

echo "🔄 正在呼叫 [$SELECTED_BRANCH] 分支的专属安装脚本..."
echo "======================================="

# 下载目标脚本（先试加速，失败回退直连）
TMP_SCRIPT=$(mktemp)
SCRIPT_OK=false

if [ -n "$GITHUB_PROXY" ]; then
    PROXY_URL="${GITHUB_PROXY}/${TARGET_SCRIPT_URL#https://}"
    if command -v curl &> /dev/null; then
        curl -sSL --connect-timeout 10 "$PROXY_URL" -o "$TMP_SCRIPT" 2>/dev/null
    else
        wget -q --timeout=10 -O "$TMP_SCRIPT" "$PROXY_URL" 2>/dev/null
    fi
    if [ -s "$TMP_SCRIPT" ]; then
        echo "✅ 加速下载成功"
        SCRIPT_OK=true
    else
        echo "⚠️ 加速失败，回退直连..."
    fi
fi

if [ "$SCRIPT_OK" = false ]; then
    if command -v curl &> /dev/null; then
        curl -sSL "$TARGET_SCRIPT_URL" -o "$TMP_SCRIPT"
    else
        wget -q -O "$TMP_SCRIPT" "$TARGET_SCRIPT_URL"
    fi
fi

bash <(sed "s/read -p.*choice.*/choice=$choice/g" "$TMP_SCRIPT")
rm -f "$TMP_SCRIPT"
#!/bin/bash
set -Eeuo pipefail

TMP_DIR=$(mktemp -d)
trap 'rm -rf -- "$TMP_DIR"' EXIT
trap 'echo "❌ 分支安装失败（第 $LINENO 行）。" >&2' ERR
trap 'exit 130' INT
trap 'exit 143' TERM
GITHUB_PROXY="${GITHUB_PROXY-https://ghfast.top}"

download_file() {
    local url="$1" output="$2"
    if command -v curl >/dev/null 2>&1; then
        curl -fSL --connect-timeout 15 --max-time 900 --retry 2 --retry-delay 1 -o "$output" "$url"
    elif command -v wget >/dev/null 2>&1; then
        if command -v timeout >/dev/null 2>&1; then
            timeout 900 wget --timeout=30 --tries=3 -O "$output" "$url"
        else
            wget --timeout=30 --tries=3 -O "$output" "$url"
        fi
    else
        echo "❌ 需要 curl 或 wget。" >&2
        return 1
    fi
}

validate_script() {
    local first_line
    IFS= read -r first_line < "$1" || return 1
    case "$first_line" in
        '#!/bin/bash'|'#!/usr/bin/env bash') bash -n "$1" ;;
        *) echo "❌ 下载内容不是有效的 Bash 安装脚本。" >&2; return 1 ;;
    esac
}

echo "======================================="
echo "  NAS Docker 多分支部署引导器"
echo "======================================="
echo "正在连接 GitHub 获取分支列表..."
API_URL="https://api.github.com/repos/warptr/docker/branches"
download_file "$API_URL" "$TMP_DIR/branches.json"
RESPONSE=$(cat "$TMP_DIR/branches.json")
mapfile -t BRANCHES < <(echo "$RESPONSE" | tr -d ' ' | grep -o '"name":"[^"]*"' | awk -F'"' '{print $4}')
if [ "${#BRANCHES[@]}" -eq 0 ]; then
    echo "❌ 未能获取有效的分支列表。" >&2
    exit 1
fi
echo "请选择你要部署的分支："
for i in "${!BRANCHES[@]}"; do echo "  $((i+1))) ${BRANCHES[$i]}"; done
read -r -p "请输入对应数字并回车: " choice
if ! [[ "$choice" =~ ^[0-9]{1,3}$ ]]; then
    echo "❌ 输入无效。" >&2
    exit 1
fi
choice=$((10#$choice))
if [ "$choice" -lt 1 ] || [ "$choice" -gt "${#BRANCHES[@]}" ]; then
    echo "❌ 输入无效。" >&2
    exit 1
fi
SELECTED_BRANCH="${BRANCHES[$((choice-1))]}"
TARGET_SCRIPT_URL="https://raw.githubusercontent.com/warptr/docker/${SELECTED_BRANCH}/mp-setup.sh"
echo "🔄 正在下载并校验 [$SELECTED_BRANCH] 分支的安装脚本..."
TMP_SCRIPT="$TMP_DIR/mp-setup.sh"
SOURCES=()
if [ -n "$GITHUB_PROXY" ]; then SOURCES+=("${GITHUB_PROXY}/${TARGET_SCRIPT_URL#https://}"); fi
SOURCES+=("$TARGET_SCRIPT_URL")
SCRIPT_OK=false
for source in "${SOURCES[@]}"; do
    rm -f "$TMP_SCRIPT"
    if download_file "$source" "$TMP_SCRIPT" && [ -s "$TMP_SCRIPT" ] && validate_script "$TMP_SCRIPT"; then
        SCRIPT_OK=true
        break
    fi
    echo "⚠️ 下载或脚本校验失败：$source" >&2
done
if [ "$SCRIPT_OK" != true ]; then
    echo "❌ 无法获取安装脚本。" >&2
    exit 1
fi
# 保留现有菜单序号传递方式，不调整平台/分支结构。
sed "s/read -[rp ]*.*choice.*/choice=$choice/g" "$TMP_SCRIPT" > "$TMP_DIR/selected-setup.sh"
bash -n "$TMP_DIR/selected-setup.sh"
bash "$TMP_DIR/selected-setup.sh"

#!/bin/bash
set -Eeuo pipefail

WORK_DIR=""
LOCK_DIR=""
LOCK_HELD=false
BACKUP_PATH=""
PROMOTED=false
CHECKSUM_INVALID=false
cleanup() {
    local status=$?
    trap - EXIT
    if [ "$PROMOTED" = false ] && [ -n "$BACKUP_PATH" ] && [ -d "$BACKUP_PATH" ]; then
        if [ ! -e "$TARGET_PATH" ]; then
            if ! mv -- "$BACKUP_PATH" "$TARGET_PATH"; then
                echo "❌ 自动恢复失败，旧内核仍保存在 $BACKUP_PATH。" >&2
                status=1
            fi
        fi
    fi
    if [ -n "$WORK_DIR" ]; then rm -rf -- "$WORK_DIR"; fi
    if [ "$LOCK_HELD" = true ]; then rmdir -- "$LOCK_DIR" || true; fi
    exit "$status"
}
trap cleanup EXIT
trap 'echo "❌ 内核更新失败（第 $LINENO 行），旧内核及备份已保留。" >&2' ERR
trap 'exit 130' INT
trap 'exit 143' TERM

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

sha256_file() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | awk '{print $1}'
    else
        echo "❌ 校验文件要求 sha256sum 或 shasum。" >&2
        return 1
    fi
}

validate_checksum() {
    local hash name extra
    if read -r hash name extra < "$1" && [[ "$hash" =~ ^[[:xdigit:]]{64}$ && "${name#\*}" == "$ASSET_NAME" && -z "$extra" ]]; then
        return 0
    fi
    CHECKSUM_INVALID=true
    return 1
}

validate_core() {
    local archive="$1" actual
    tar -tzf "$archive" > "$WORK_DIR/members.txt" || return 1
    if ! awk '/^\// || /(^|\/)\.\.(\/|$)/ { bad=1 } END { exit bad }' "$WORK_DIR/members.txt"; then
        echo "❌ 内核包包含异常路径。" >&2
        return 1
    fi
    if [ -n "$EXPECTED_HASH" ]; then
        actual=$(sha256_file "$archive") || return 1
        if [ "$actual" != "${EXPECTED_HASH,,}" ]; then
            echo "❌ 内核包 SHA256 不匹配。" >&2
            return 1
        fi
    fi
}

smart_download() {
    local url="$1" output="$2" validator="$3" source
    local sources=()
    if [ -n "$GITHUB_PROXY" ]; then sources+=("${GITHUB_PROXY}/${url#https://}"); fi
    sources+=("$url")
    for source in "${sources[@]}"; do
        rm -f -- "$output"
        if download_file "$source" "$output" && [ -s "$output" ] && "$validator" "$output"; then
            return 0
        fi
        echo "⚠️ 下载或校验失败：$source" >&2
    done
    rm -f -- "$output"
    return 1
}

echo "========================================================="
echo "🚀 MoviePilot 浏览器内核更新器"
echo "========================================================="
DEFAULT_DOCKER_PATH="${DOCKER_PATH:-/vol1/1000/docker}"
read -r -p "📂 请输入 docker 根目录路径 [默认: $DEFAULT_DOCKER_PATH]: " USER_INPUT
BASE_PATH="${USER_INPUT:-$DEFAULT_DOCKER_PATH}"
if [[ "$BASE_PATH" != /* || "$BASE_PATH" == / || "$BASE_PATH" == *$'\n'* || "$BASE_PATH" == *$'\r'* ]]; then
    echo "❌ 必须提供非根目录的绝对路径。" >&2
    exit 1
fi
case "$(uname -m)" in
    x86_64) MP_ARCH=amd64 ;;
    aarch64|arm64) MP_ARCH=arm64 ;;
    *) echo "❌ 不支持的 CPU 架构。" >&2; exit 1 ;;
esac
mkdir -p -- "$BASE_PATH"
BASE_PATH=$(cd "$BASE_PATH" && pwd -P)
if [ "$BASE_PATH" = / ]; then
    echo "❌ 部署路径不能解析为根目录。" >&2
    exit 1
fi
mkdir -p -- "$BASE_PATH/moviepilot-v2"
CORE_PARENT=$(cd "$BASE_PATH/moviepilot-v2" && pwd -P)
TARGET_PATH="$CORE_PARENT/core"
if [ -L "$TARGET_PATH" ] || { [ -e "$TARGET_PATH" ] && [ ! -d "$TARGET_PATH" ]; }; then
    echo "❌ core 必须是普通目录，不能是链接或文件。" >&2
    exit 1
fi
LOCK_DIR="$CORE_PARENT/.core-update.lock"
if ! mkdir -- "$LOCK_DIR"; then
    echo "❌ 已有内核更新正在执行；若上次被强制终止，请确认无更新进程后移除 $LOCK_DIR。" >&2
    exit 1
fi
LOCK_HELD=true
WORK_DIR=$(mktemp -d "$CORE_PARENT/.core-update.XXXXXX")
GITHUB_PROXY="${GITHUB_PROXY-https://ghfast.top}"
ASSET_NAME="mp-core-all-${MP_ARCH}.tar.gz"
DOWNLOAD_URL="https://github.com/warptr/docker/releases/download/v2-latest/$ASSET_NAME"
EXPECTED_HASH=""
if smart_download "$DOWNLOAD_URL.sha256" "$WORK_DIR/$ASSET_NAME.sha256" validate_checksum; then
    read -r EXPECTED_HASH _ < "$WORK_DIR/$ASSET_NAME.sha256"
else
    if [ "$CHECKSUM_INVALID" = true ]; then
        echo "❌ 已收到校验文件，但格式无效；拒绝跳过 SHA256 校验。" >&2
        exit 1
    fi
    # 旧版 v2-latest 没有伴随校验文件，保留兼容并说明验证范围。
    echo "⚠️ 校验文件不可用：兼容旧版 Release，仅检查压缩包完整性和目录结构。" >&2
fi
echo "📥 正在下载并校验内核，旧版本暂不改动..."
smart_download "$DOWNLOAD_URL" "$WORK_DIR/$ASSET_NAME" validate_core
mkdir "$WORK_DIR/new-core"
tar -xzpf "$WORK_DIR/$ASSET_NAME" -C "$WORK_DIR/new-core"
shopt -s nullglob dotglob
ENTRIES=("$WORK_DIR/new-core/"*)
BROWSERS=("$WORK_DIR/new-core/"chromium-*)
if [ "${#BROWSERS[@]}" -ne 1 ] || [ ! -x "${BROWSERS[0]}/chrome" ]; then
    echo "❌ 内核包缺少唯一的 chromium 目录或可执行 chrome。" >&2
    exit 1
fi
for entry in "${ENTRIES[@]}"; do
    case "$(basename "$entry")" in
        chromium-*|.last_update_check|.welcome_shown) ;;
        *) echo "❌ 内核包顶层结构异常：$entry" >&2; exit 1 ;;
    esac
done

echo "📦 校验和解压成功，开始替换内核..."
if [ -d "$TARGET_PATH" ]; then
    BACKUP_PATH="$CORE_PARENT/core.backup.$(date +%Y%m%d-%H%M%S).$$"
    mv -- "$TARGET_PATH" "$BACKUP_PATH"
fi
mv -- "$WORK_DIR/new-core" "$TARGET_PATH"
PROMOTED=true
echo "✅ 内核更新完成：$TARGET_PATH"
if [ -n "$BACKUP_PATH" ]; then echo "📂 上一版本备份：$BACKUP_PATH"; fi
echo "🐳 请重建或重启 MoviePilot 容器，以重新挂载新的内核目录。"

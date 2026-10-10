#!/bin/bash
set -Eeuo pipefail

TMP_DIR=""
ENV_TMP=""
cleanup() {
    local status=$?
    if [ -n "$ENV_TMP" ]; then rm -f -- "$ENV_TMP"; fi
    if [ -n "$TMP_DIR" ]; then rm -rf -- "$TMP_DIR"; fi
    return "$status"
}
trap cleanup EXIT
trap 'echo "❌ 部署失败（第 $LINENO 行），请查看上方错误；已有配置和备份已保留。" >&2' ERR
trap 'exit 130' INT
trap 'exit 143' TERM

echo "======================================="
echo "  NAS Docker 交互式部署脚本"
echo "======================================="
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

validate_zip() {
    if command -v unzip >/dev/null 2>&1; then
        unzip -tq "$1" >/dev/null
    elif command -v 7zz >/dev/null 2>&1; then
        7zz t "$1" >/dev/null
    elif command -v 7z >/dev/null 2>&1; then
        7z t "$1" >/dev/null
    else
        echo "❌ 需要 unzip、7zz 或 7z。" >&2
        return 1
    fi
}

smart_download() {
    local url="$1" output="$2" source
    local sources=()
    if [ -n "$GITHUB_PROXY" ]; then sources+=("${GITHUB_PROXY}/${url#https://}"); fi
    sources+=("$url")
    for source in "${sources[@]}"; do
        rm -f -- "$output"
        if download_file "$source" "$output" && [ -s "$output" ] && validate_zip "$output"; then
            return 0
        fi
        echo "⚠️ 下载或压缩包校验失败：$source" >&2
    done
    rm -f -- "$output"
    return 1
}

extract_zip() {
    local archive="$1" destination="$2"
    mkdir -p -- "$destination"
    if command -v unzip >/dev/null 2>&1; then
        unzip -qo "$archive" -d "$destination"
    elif command -v 7zz >/dev/null 2>&1; then
        7zz x "$archive" -o"$destination" -y >/dev/null
    else
        7z x "$archive" -o"$destination" -y >/dev/null
    fi
}

read_env_path() {
    local file="$1" key="$2" line value="" result=""
    local single_quoted="^'([^']*)'[[:space:]]*(#.*)?$"
    local double_quoted='^"([^"]*)"[[:space:]]*(#.*)?$'
    if [ ! -f "$file" ]; then return 0; fi
    # 只读取路径键，不 source/eval 分支中的 .env。
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line%$'\r'}"
        if [[ "$line" =~ ^[[:space:]]*$key[[:space:]]*=(.*)$ ]]; then
            value="${BASH_REMATCH[1]}"
            value="${value#"${value%%[![:space:]]*}"}"
            value="${value%"${value##*[![:space:]]}"}"
            if [[ "$value" =~ $single_quoted || "$value" =~ $double_quoted ]]; then
                value="${BASH_REMATCH[1]}"
            elif [[ "$value" == \"* || "$value" == \'* ]]; then
                echo "❌ 分支 .env 中的 $key 引号格式无效。" >&2
                return 1
            else
                value="${value%%[[:space:]]#*}"
                value="${value%"${value##*[![:space:]]}"}"
            fi
            result="$value"
        fi
    done < "$file"
    printf '%s' "$result"
}

echo "正在连接 GitHub 获取分支列表..."
TMP_DIR=$(mktemp -d)
API_URL="https://api.github.com/repos/warptr/docker/branches"
download_file "$API_URL" "$TMP_DIR/branches.json"
RESPONSE=$(cat "$TMP_DIR/branches.json")
# 保留现有菜单和分支选择方式。
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
echo "-> 已选择分支: $SELECTED_BRANCH"

ZIP_URL="https://github.com/warptr/docker/archive/refs/heads/${SELECTED_BRANCH}.zip"
echo "📦 正在下载并校验分支代码..."
smart_download "$ZIP_URL" "$TMP_DIR/archive.zip"
extract_zip "$TMP_DIR/archive.zip" "$TMP_DIR/source"
shopt -s nullglob
ROOTS=("$TMP_DIR/source/"*/)
if [ "${#ROOTS[@]}" -ne 1 ]; then
    echo "❌ 安装包根目录结构异常。" >&2
    exit 1
fi
EXTRACTED_ROOT="${ROOTS[0]}"
SOURCE_COMPOSE="${EXTRACTED_ROOT}docker/moviepilot-v2/docker-compose.yml"
SOURCE_UPDATER="${EXTRACTED_ROOT}update_mp_core.sh"
if [ ! -f "$SOURCE_COMPOSE" ] || [ ! -f "$SOURCE_UPDATER" ]; then
    echo "❌ 安装包缺少 Compose 文件或内核更新脚本。" >&2
    exit 1
fi
bash -n "$SOURCE_UPDATER"

SOURCE_ENV="${EXTRACTED_ROOT}docker/moviepilot-v2/.env"
DEFAULT_DOCKER_PATH=$(read_env_path "$SOURCE_ENV" DOCKER_PATH)
DEFAULT_MEDIA_PATH=$(read_env_path "$SOURCE_ENV" MEDIA_PATH)
if [ -z "$DEFAULT_DOCKER_PATH" ] || [ -z "$DEFAULT_MEDIA_PATH" ]; then
    echo "提示：所选分支的 .env 未配置完整路径，缺失的路径请手动输入。"
fi

case "$SELECTED_BRANCH" in
    zspace|zspace-arm|zspace-z4s)
        echo "📂 极空间安装路径查询："
        echo "   Docker → Compose → 新增项目 → 查询路径 → 选择目标文件夹 → 复制路径。"
        echo "   请分别查询 Docker 安装目录和媒体目录，并在下方粘贴完整的主机路径。"
        echo "   路径中的用户名一般是完整手机号；若带 a、b 等字母后缀，也要一并填写。"
        echo "   sata11、nvme11 等存储池名称以查询结果为准；158******** 是占位符，不能直接使用。"
        echo "   若下方默认路径仍包含占位符，请替换为实际用户名和存储池后再输入。"
        ;;
esac

read -r -p "请输入 DOCKER_PATH [默认: ${DEFAULT_DOCKER_PATH:-未配置，必须手动输入}]: " INPUT_DOCKER
DOCKER_PATH=${INPUT_DOCKER:-$DEFAULT_DOCKER_PATH}
read -r -p "请输入 MEDIA_PATH [默认: ${DEFAULT_MEDIA_PATH:-未配置，必须手动输入}]: " INPUT_MEDIA
MEDIA_PATH=${INPUT_MEDIA:-$DEFAULT_MEDIA_PATH}
for path in "$DOCKER_PATH" "$MEDIA_PATH"; do
    if [[ "$path" == *'*'* ]]; then
        echo "❌ 路径中仍有星号占位符，请通过极空间查询路径并填写实际用户名和存储池。" >&2
        exit 1
    fi
    if [[ "$path" != /* || "$path" == / || "$path" == *$'\n'* || "$path" == *$'\r'* ]]; then
        echo "❌ 部署路径必须是非根目录的绝对路径，且不能包含换行。" >&2
        exit 1
    fi
done
mkdir -p -- "$DOCKER_PATH" "$MEDIA_PATH"
DOCKER_PATH=$(cd "$DOCKER_PATH" && pwd -P)
MEDIA_PATH=$(cd "$MEDIA_PATH" && pwd -P)
if [ "$DOCKER_PATH" = / ] || [ "$MEDIA_PATH" = / ]; then
    echo "❌ 部署路径不能解析为根目录。" >&2
    exit 1
fi
cd "$DOCKER_PATH"
if command -v docker-compose >/dev/null 2>&1; then
    COMPOSE=(docker-compose)
elif command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose)
else
    echo "❌ 未找到可用的 Docker Compose。" >&2
    exit 1
fi

# 媒体预设先完整解压到临时目录，再只补齐缺失文件。
if [ -f "${EXTRACTED_ROOT}media.zip" ]; then
    validate_zip "${EXTRACTED_ROOT}media.zip"
    extract_zip "${EXTRACTED_ROOT}media.zip" "$TMP_DIR/media"
    cp -Rn "$TMP_DIR/media/." "$MEDIA_PATH/"
fi

DEPLOY_DIR="$DOCKER_PATH/moviepilot-v2"
BACKUP_DIR="$DOCKER_PATH/.nas-deploy-backups/$(date +%Y%m%d-%H%M%S)-$$"
for file in docker-compose.yml .env; do
    if [ -e "$DEPLOY_DIR/$file" ]; then
        mkdir -p "$BACKUP_DIR"
        cp -p "$DEPLOY_DIR/$file" "$BACKUP_DIR/$file"
    fi
done
# 数据库、账号、插件和用户修改的配置均不覆盖。
cp -Rn "${EXTRACTED_ROOT}docker/." "$DOCKER_PATH/"
cp "$SOURCE_COMPOSE" "$DEPLOY_DIR/docker-compose.yml"

# 只更新用户本次选择的两个路径变量，保留 .env 中的其他设置。
ENV_TMP=$(mktemp "$DEPLOY_DIR/.env.XXXXXX")
if [ -f "$DEPLOY_DIR/.env" ]; then
    while IFS= read -r line || [ -n "$line" ]; do
        if [[ ! "$line" =~ ^[[:space:]]*(DOCKER_PATH|MEDIA_PATH)[[:space:]]*= ]]; then
            printf '%s\n' "$line" >> "$ENV_TMP"
        fi
    done < "$DEPLOY_DIR/.env"
fi
printf "DOCKER_PATH='%s'\nMEDIA_PATH='%s'\n" "${DOCKER_PATH//\'/\\\'}" "${MEDIA_PATH//\'/\\\'}" >> "$ENV_TMP"
mv -f "$ENV_TMP" "$DEPLOY_DIR/.env"
cd "$DEPLOY_DIR"
# 防止调用者的同名环境变量覆盖交互输入。
export DOCKER_PATH MEDIA_PATH
"${COMPOSE[@]}" config -q
echo "⚙️ 正在初始化浏览器内核..."
printf '%s\n' "$DOCKER_PATH" | bash "$SOURCE_UPDATER"

echo "🚀 正在启动 Docker 容器..."
"${COMPOSE[@]}" up -d
# 内核目录经过替换，需要重建 MoviePilot 的 bind mount。
"${COMPOSE[@]}" up -d --no-deps --force-recreate moviepilot

# 不要求镜像新增 HEALTHCHECK；若已有检查则等待 healthy，否则检查 running。
ready=false
services_output=$("${COMPOSE[@]}" config --services)
mapfile -t SERVICES <<< "$services_output"
if [ -z "$services_output" ]; then
    echo "❌ Compose 未返回任何服务。" >&2
    exit 1
fi
for attempt in {1..30}; do
    ready=true
    for service in "${SERVICES[@]}"; do
        ids=$("${COMPOSE[@]}" ps -q "$service")
        if [ -z "$ids" ]; then ready=false; break; fi
        for id in $ids; do
            state=$(docker inspect --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$id")
            case "$state" in
                'running healthy'|'running none') ;;
                *unhealthy*|exited*|dead*)
                    echo "❌ 容器 $service 状态异常：$state" >&2
                    exit 1
                    ;;
                *) ready=false ;;
            esac
        done
    done
    if [ "$ready" = true ] && [ "${#SERVICES[@]}" -gt 0 ]; then break; fi
    ready=false
    sleep 2
done
if [ "$ready" != true ]; then
    echo "❌ 容器未在 60 秒内进入运行/健康状态，请检查容器日志。" >&2
    exit 1
fi
if [ -d "$BACKUP_DIR" ]; then echo "📂 部署文件备份：$BACKUP_DIR"; fi
echo "✅ 部署完成！当前运行分支：$SELECTED_BRANCH"
echo "提示：未配置 HEALTHCHECK 的镜像仅确认容器正在运行。"

# docker

一键安装命令

```bash
bash <(curl -sSL "https://raw.githubusercontent.com/warptr/docker/main/mp-setup-choose.sh")
```

一键安装浏览器核心命令

```bash
bash <(curl -sSL "https://raw.githubusercontent.com/warptr/docker/main/update_mp_core.sh")

bash <(curl -sSL "https://raw.githubusercontent.com/warptr/docker/main/update_mp_core.sh" | tr -d '\r')
```



各分支同步main命令

```
$branches = "dsm", "fnos", "fnos-arm", "qnap", "ugreen", "zspace", "zspace-arm", "zspace-z4s"; foreach ($b in $branches) { git checkout $b; git merge main --no-edit; git push } ; git checkout main
```



## 部署与更新行为

- 安装时先读取所选分支的 `docker/moviepilot-v2/.env`，使用其中的 `DOCKER_PATH` 和 `MEDIA_PATH` 作为交互默认值，可手动覆盖。分支未配置路径时必须手动输入；极空间路径中的用户名、存储池及星号占位符需按实际查询结果补全。
- 重复安装只补齐缺失的配置和媒体预设，不覆盖已有数据库、账号或插件设置。所选分支的 `docker-compose.yml` 会更新，`.env` 只更新 `DOCKER_PATH` 和 `MEDIA_PATH`，原文件先备份到安装目录的 `.nas-deploy-backups/`。
- 内核更新先下载、校验和解压，再替换 `core`。失败时保留或恢复旧内核；成功时将旧版本保存在同级 `core.backup.<时间>.<进程号>` 目录。确认新版本正常后可以自行删除备份以释放空间。
- 新发布的内核包带有 `.sha256` 和 `.source.json`。旧 Release 若无法取得校验文件，会明确提示仅检查压缩包完整性及目录结构；取得校验文件后发现格式错误或 SHA256 不匹配则终止更新。
- 安装、更新或容器启动失败时返回非零退出码。安装后最多轮询 30 次容器状态，每次间隔 2 秒；有 HEALTHCHECK 时等待 healthy，没有时仅确认 running。
- 独立更新内核后，在安装目录的 `moviepilot-v2` 下运行 `docker compose up -d --no-deps --force-recreate moviepilot`，让容器重新挂载新目录。旧版 Compose 可将命令替换为 `docker-compose`。安装脚本会自动执行此步骤。
- 三个脚本均可通过 `GITHUB_PROXY` 环境变量覆盖加速地址；设为空字符串即可直连，例如 `GITHUB_PROXY='' bash ./update_mp_core.sh`。

## 本地验证

需要 Bash 4+、Python 3.12+、常见 Linux 文件工具及 unzip。Python 仅用于测试和 GitHub Actions 打包，NAS 安装脚本无需 Python。

```bash
bash -n mp-setup.sh && bash -n mp-setup-choose.sh && bash -n update_mp_core.sh
python3 -m unittest discover -s tests -v
```

回归测试使用临时目录、模拟下载和 Docker 命令，不访问网络或启动真实容器。自动打包按架构查找稳定版 Release，支持分页，使用上游 digest、SHA256SUMS 或发布说明中的校验值；两个架构均打包成功后统一上传到当前仓库。

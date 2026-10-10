"""Offline regressions: real scripts and archives, mocked network and Docker."""

import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
import zipfile


REPO = Path(__file__).resolve().parents[1]
ASSET = "mp-core-all-amd64.tar.gz"
VERSION = "150.0.7871.114.6"

NETWORK_MOCK = r'''#!/usr/bin/python3
import json, os, pathlib, sys
args = sys.argv[1:]
url = args[-1]
flag = '-o' if '-o' in args else '-O'
output = pathlib.Path(args[args.index(flag) + 1])
with open(os.environ['NETWORK_LOG'], 'a') as log:
    log.write(json.dumps({'url':url, 'args':args}) + '\n')
routes = json.loads(pathlib.Path(os.environ['NETWORK_ROUTES']).read_text())
for key, route in routes.items():
    if key in url:
        if 'file' in route:
            output.write_bytes(pathlib.Path(route['file']).read_bytes())
        else:
            output.write_text(route.get('body', ''))
        sys.exit(route.get('status', 0))
output.write_text('')
sys.exit(22)
'''

DOCKER_MOCK = r'''#!/usr/bin/python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ['DOCKER_LOG'], 'a') as log:
    log.write(json.dumps(args) + '\n')
if args[0] == 'inspect':
    print(os.environ.get('MOCK_STATE', 'running healthy'))
    sys.exit(0)
if args[0] == 'compose': args = args[1:]
if args[0] == 'version': sys.exit(0)
if args[0] == 'config':
    if '--services' in args:
        if os.environ.get('MOCK_SERVICES_STATUS'):
            sys.exit(int(os.environ['MOCK_SERVICES_STATUS']))
        print('qbittorrent\nmoviepilot\namilys-emby')
    sys.exit(int(os.environ.get('MOCK_CONFIG_STATUS', '0')))
if args[0] == 'up': sys.exit(int(os.environ.get('MOCK_UP_STATUS', '0')))
if args[0] == 'ps':
    print(args[-1] + '-id')
    sys.exit(0)
sys.exit(2)
'''


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="nas-tests-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base = self.root / "docker space"
        self.media = self.root / "media #$'space"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        # Restricted PATH also lets us exercise wget-only NAS installations.
        for command in ["awk", "basename", "bash", "cat", "cp", "date", "grep", "gzip",
                        "mkdir", "mktemp", "mv", "rm", "rmdir", "sed", "sha256sum",
                        "sleep", "tar", "timeout", "tr", "uname", "unzip"]:
            source = shutil.which(command)
            if source:
                (self.bin / command).symlink_to(source)
        for command, contents in [("curl", NETWORK_MOCK), ("wget", NETWORK_MOCK),
                                  ("docker", DOCKER_MOCK)]:
            self.write_command(command, contents)
        self.routes = self.root / "routes.json"
        self.netlog = self.root / "network.log"
        self.dockerlog = self.root / "docker.log"
        self.env = dict(os.environ, PATH=str(self.bin), GITHUB_PROXY="",
                        NETWORK_ROUTES=str(self.routes), NETWORK_LOG=str(self.netlog),
                        DOCKER_LOG=str(self.dockerlog))
        self.env.pop("BASH_ENV", None)
        self.env.pop("DOCKER_PATH", None)
        self.env.pop("MEDIA_PATH", None)
        self.route_map = {"api.github.com": {"body": '[{"name":"main"}]'}}
        self.make_core()
        self.make_install_archive()

    def write_command(self, name, contents):
        path = self.bin / name
        if path.is_symlink():
            path.unlink()
        path.write_text(contents)
        path.chmod(0o755)

    def make_core(self, *, executable=True, bad_structure=False):
        archive = self.root / "core.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            name = "wrong/chrome" if bad_structure else f"chromium-{VERSION}/chrome"
            info = tarfile.TarInfo(name)
            content = b"#!/bin/sh\nexit 0\n"
            info.size = len(content)
            info.mode = 0o755 if executable else 0o644
            tar.addfile(info, io.BytesIO(content))
            tar.addfile(tarfile.TarInfo(".last_update_check"))
            tar.addfile(tarfile.TarInfo(".welcome_shown"))
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        self.route_map[ASSET + ".sha256"] = {"body": f"{digest}  {ASSET}\n"}
        self.route_map[ASSET] = {"file": str(archive)}

    def make_install_archive(self, updater=None, env=None):
        media_zip = io.BytesIO()
        with zipfile.ZipFile(media_zip, "w") as z:
            z.writestr("existing.txt", "preset media")
            z.writestr("new.txt", "new media")
        archive = self.root / "install.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("docker-main/docker/moviepilot-v2/docker-compose.yml", "services: {}\n")
            z.writestr("docker-main/docker/moviepilot-v2/config/user.db", "preset database")
            z.writestr("docker-main/docker/moviepilot-v2/config/new.conf", "new config")
            z.writestr("docker-main/docker/qbittorrent/config/qBittorrent.conf", "preset qb")
            z.writestr("docker-main/update_mp_core.sh", updater or (REPO / "update_mp_core.sh").read_text())
            z.writestr("docker-main/media.zip", media_zip.getvalue())
            if env is not None:
                z.writestr("docker-main/docker/moviepilot-v2/.env", env)
        self.route_map["archive/refs/heads"] = {"file": str(archive)}

    def run_script(self, script, stdin, **env):
        self.routes.write_text(json.dumps(self.route_map))
        return subprocess.run([str(self.bin / "bash"), str(REPO / script)],
                              input=stdin, text=True, capture_output=True,
                              cwd=self.root, env=dict(self.env, **env), timeout=15)

    def run_update(self, **env):
        return self.run_script("update_mp_core.sh", str(self.base) + "\n", **env)

    def run_install(self, **env):
        return self.run_script("mp-setup.sh", f"1\n{self.base}\n{self.media}\n", **env)

    def old_core(self):
        core = self.base / "moviepilot-v2/core"
        core.mkdir(parents=True)
        (core / "old-browser").write_text("working old browser")
        return core

    def assert_old_preserved(self, result):
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.base / "moviepilot-v2/core/old-browser").read_text(), "working old browser")
        self.assertFalse((self.base / "moviepilot-v2/.core-update.lock").exists())
        self.assertFalse(list((self.base / "moviepilot-v2").glob(".core-update.*")))

    def test_failed_download_preserves_old_core(self):
        self.old_core()
        self.route_map[ASSET] = {"body": "partial archive", "status": 8}
        self.assert_old_preserved(self.run_update())

    def test_corrupt_archive_preserves_old_core(self):
        self.old_core()
        self.route_map[ASSET] = {"body": "<html>proxy error</html>"}
        self.assert_old_preserved(self.run_update())

    def test_checksum_mismatch_preserves_old_core(self):
        self.old_core()
        self.route_map[ASSET + ".sha256"] = {"body": f"{'0' * 64}  {ASSET}\n"}
        self.assert_old_preserved(self.run_update())

    def test_missing_browser_preserves_old_core(self):
        self.old_core()
        self.make_core(bad_structure=True)
        self.assert_old_preserved(self.run_update())

    def test_invalid_checksum_file_is_not_silently_ignored(self):
        self.old_core()
        self.route_map[ASSET + ".sha256"] = {"body": "not a checksum\n"}
        self.assert_old_preserved(self.run_update())

    def test_nonexecutable_browser_preserves_old_core(self):
        self.old_core()
        self.make_core(executable=False)
        self.assert_old_preserved(self.run_update())

    def test_success_keeps_backup_and_executable(self):
        self.old_core()
        result = self.run_update()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        chrome = self.base / f"moviepilot-v2/core/chromium-{VERSION}/chrome"
        self.assertTrue(os.access(chrome, os.X_OK))
        backups = list((self.base / "moviepilot-v2").glob("core.backup.*"))
        self.assertEqual(len(backups), 1)
        self.assertTrue((backups[0] / "old-browser").is_file())

    def test_promotion_failure_restores_old_core(self):
        self.old_core()
        real_mv = shutil.which("mv")
        self.write_command("mv", f'''#!/bin/bash
for arg in "$@"; do case "$arg" in */new-core) exit 19 ;; esac; done
exec '{real_mv}' "$@"
''')
        self.assert_old_preserved(self.run_update())
        self.assertFalse(list((self.base / "moviepilot-v2").glob("core.backup.*")))

    def test_legacy_release_without_checksum_still_checks_archive(self):
        self.route_map[ASSET + ".sha256"] = {"status": 22}
        result = self.run_update()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("兼容旧版 Release", result.stderr)

    def test_invalid_proxy_archive_falls_back_to_direct(self):
        self.route_map = {"proxy.invalid": {"body": "broken", "status": 8}, **self.route_map}
        result = self.run_update(GITHUB_PROXY="https://proxy.invalid")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("github.com/warptr/docker/releases", self.netlog.read_text())

    def test_wget_only_installation(self):
        (self.bin / "curl").unlink()
        result = self.run_update()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_existing_update_lock_is_respected(self):
        self.old_core()
        lock = self.base / "moviepilot-v2/.core-update.lock"
        lock.mkdir()
        result = self.run_update()
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(lock.is_dir())
        self.assertFalse(self.netlog.exists())

    def test_rejects_empty_root_and_relative_paths(self):
        for value in ["/", "/tmp/..", "relative/path"]:
            with self.subTest(value=value):
                result = self.run_script("update_mp_core.sh", value + "\n")
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.netlog.exists())

    def test_reinstall_preserves_user_data_and_backs_up_deployment(self):
        deploy = self.base / "moviepilot-v2"
        (deploy / "config").mkdir(parents=True)
        (deploy / "config/user.db").write_text("user database")
        (deploy / "docker-compose.yml").write_text("old compose")
        (deploy / ".env").write_text("CUSTOM=keep\nDOCKER_PATH=/old\nMEDIA_PATH=/old\n")
        self.media.mkdir()
        (self.media / "existing.txt").write_text("user media")
        result = self.run_install(DOCKER_PATH="/wrong-inherited", MEDIA_PATH="/wrong-inherited")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((deploy / "config/user.db").read_text(), "user database")
        self.assertEqual((self.media / "existing.txt").read_text(), "user media")
        self.assertTrue((self.media / "new.txt").is_file())
        self.assertTrue((deploy / "config/new.conf").is_file())
        env = (deploy / ".env").read_text()
        self.assertIn("CUSTOM=keep", env)
        self.assertIn("DOCKER_PATH='" + str(self.base) + "'", env)
        self.assertIn(str(self.media).replace("'", "\\'"), env)
        backup = list((self.base / ".nas-deploy-backups").glob("*"))
        self.assertEqual(len(backup), 1)
        self.assertEqual((backup[0] / "docker-compose.yml").read_text(), "old compose")
        self.assertIn("CUSTOM=keep", (backup[0] / ".env").read_text())
        self.assertIn('--force-recreate', self.dockerlog.read_text())

    def test_selected_branch_env_supplies_defaults(self):
        self.route_map["api.github.com"] = {"body": '[{"name":"dsm"}]'}
        self.make_install_archive(env=f'DOCKER_PATH={self.base}\nMEDIA_PATH="{self.media}"\nCUSTOM=branch-setting\n')
        result = self.run_script("mp-setup.sh", "1\n\n\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.base / "moviepilot-v2/core").is_dir())
        self.assertTrue((self.media / "new.txt").is_file())
        self.assertIn("CUSTOM=branch-setting", (self.base / "moviepilot-v2/.env").read_text())
        self.assertIn("/dsm.zip", self.netlog.read_text())

    def test_manual_paths_override_branch_defaults(self):
        other = self.root / "unused branch default"
        self.make_install_archive(env=f"DOCKER_PATH={other}/docker\nMEDIA_PATH={other}/media\n")
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.base / "moviepilot-v2/core").is_dir())
        self.assertFalse(other.exists())

    def test_missing_branch_defaults_require_input(self):
        result = self.run_script("mp-setup.sh", "1\n\n\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("缺失的路径请手动输入", result.stdout)
        self.assertFalse(self.base.exists())
        self.assertFalse(self.dockerlog.exists())

    def test_zspace_placeholders_are_rejected_before_creating_directories(self):
        self.route_map["api.github.com"] = {"body": '[{"name":"zspace-arm"}]'}
        placeholder = self.root / "sata11/158********/data"
        self.make_install_archive(env=f"DOCKER_PATH={placeholder}/docker\nMEDIA_PATH={placeholder}/media\n")
        result = self.run_script("mp-setup.sh", "1\n\n\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("查询路径", result.stdout)
        self.assertIn("星号占位符", result.stderr)
        self.assertFalse((self.root / "sata11").exists())

    def test_env_paths_support_quotes_comments_crlf_without_execution(self):
        marker = self.root / "should-not-exist"
        # Phone/account placeholders are data, and unrelated .env keys never execute.
        self.media = self.root / "quoted media #space"
        self.make_install_archive(env=f"IGNORED=$(touch '{marker}')\r\n DOCKER_PATH = '{self.base}' # docker\r\nMEDIA_PATH=\"{self.media}\" # media\r\n")
        result = self.run_script("mp-setup.sh", "1\n\n\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(marker.exists())
        self.assertTrue((self.media / "new.txt").is_file())

    def test_failed_core_initialization_propagates(self):
        self.make_install_archive("#!/bin/bash\nexit 17\n")
        result = self.run_install()
        self.assertEqual(result.returncode, 17, result.stdout + result.stderr)
        self.assertNotIn('"up"', self.dockerlog.read_text())
        self.assertNotIn("✅ 部署完成", result.stdout)

    def test_failed_compose_start_propagates(self):
        result = self.run_install(MOCK_UP_STATUS="23")
        self.assertEqual(result.returncode, 23, result.stdout + result.stderr)
        self.assertNotIn("✅ 部署完成", result.stdout)

    def test_invalid_compose_aborts_before_core_download(self):
        result = self.run_install(MOCK_CONFIG_STATUS="24")
        self.assertEqual(result.returncode, 24, result.stdout + result.stderr)
        self.assertNotIn("releases/download", self.netlog.read_text())

    def test_unhealthy_container_is_not_reported_as_success(self):
        result = self.run_install(MOCK_STATE="running unhealthy")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("✅ 部署完成", result.stdout)

    def test_service_query_failure_propagates(self):
        result = self.run_install(MOCK_SERVICES_STATUS="25")
        self.assertEqual(result.returncode, 25, result.stdout + result.stderr)

    def test_failed_nonempty_zip_is_not_extracted(self):
        self.route_map["archive/refs/heads"] = {"body": "partial", "status": 8}
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.base / "moviepilot-v2").exists())

    def test_chooser_propagates_child_exit(self):
        self.route_map["raw.githubusercontent.com"] = {"body": "#!/bin/bash\nread -p 'choice: ' choice\nexit 17\n"}
        result = self.run_script("mp-setup-choose.sh", "1\n")
        self.assertEqual(result.returncode, 17, result.stdout + result.stderr)

    def test_chooser_rejects_html_response(self):
        self.route_map["raw.githubusercontent.com"] = {"body": "<html>not found</html>\n"}
        result = self.run_script("mp-setup-choose.sh", "1\n")
        self.assertNotEqual(result.returncode, 0)

    def test_chooser_runs_current_menu_without_second_read(self):
        self.route_map["raw.githubusercontent.com"] = {"body": (REPO / "mp-setup.sh").read_text()}
        result = self.run_script("mp-setup-choose.sh", f"01\n{self.base}\n{self.media}\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()

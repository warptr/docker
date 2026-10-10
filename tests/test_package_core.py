import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import URLError


SCRIPT = Path(__file__).resolve().parents[1] / ".github/scripts/package_core.py"
spec = importlib.util.spec_from_file_location("package_core", SCRIPT)
packager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(packager)


class PackageCoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="core-package-tests-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.raw = self.root / "source.tar.gz"
        with tarfile.open(self.raw, "w:gz") as tar:
            info = tarfile.TarInfo("chrome")
            content = b"#!/bin/sh\nexit 0\n"
            info.size = len(content)
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(content))
        self.digest = hashlib.sha256(self.raw.read_bytes()).hexdigest()
        self.asset = {
            "name": "cloakbrowser-linux-x64.tar.gz",
            "browser_download_url": "https://example.invalid/core.tar.gz",
            "digest": "sha256:" + self.digest,
            "size": self.raw.stat().st_size,
        }
        self.release = {"tag_name": "chromium-v150.0.7871.114.6", "assets": [self.asset]}
        self.pages = [[self.release]]
        self.requests = []

    def download(self, url, destination, **kwargs):
        self.requests.append(url)
        if "api.github.com" in url:
            page = int(url.rsplit("page=", 1)[1])
            releases = self.pages[page - 1] if page <= len(self.pages) else []
            destination.write_text(json.dumps(releases))
        elif url.endswith("SHA256SUMS"):
            destination.write_text(self.manifest)
        else:
            destination.write_bytes(self.raw.read_bytes())

    def package(self, architecture="amd64"):
        with patch.object(packager, "download", side_effect=self.download):
            packager.package_core(architecture, self.root / "dist")

    def test_supports_new_chromium_major_and_preserves_executable(self):
        self.package()
        target = self.root / "dist/mp-core-all-amd64.tar.gz"
        with tarfile.open(target) as tar:
            chrome = tar.getmember("chromium-150.0.7871.114.6/chrome")
            self.assertTrue(chrome.mode & 0o111)
            self.assertIn(".last_update_check", tar.getnames())
        actual = hashlib.sha256(target.read_bytes()).hexdigest()
        self.assertTrue(target.with_name(target.name + ".sha256").read_text().startswith(actual))
        metadata = json.loads(target.with_name(target.name + ".source.json").read_text())
        self.assertTrue(metadata["upstream_checksum_verified"])
        self.assertEqual(metadata["version"], "150.0.7871.114.6")

    def test_release_search_paginates_and_skips_prereleases(self):
        prerelease = dict(self.release, prerelease=True, tag_name="chromium-v151.0.1.2.3")
        self.pages = [[{"tag_name": "wrapper-v0.1", "assets": []}] * 100,
                      [prerelease, self.release]]
        self.package()
        self.assertTrue(any("page=2" in url for url in self.requests))

    def test_arm64_asset_and_metadata(self):
        self.asset["name"] = "cloakbrowser-linux-arm64.tar.gz"
        self.package("arm64")
        self.assertTrue((self.root / "dist/mp-core-all-arm64.tar.gz").is_file())

    def test_hash_mismatch_does_not_produce_release_archive(self):
        self.asset["digest"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(ValueError, "SHA256 mismatch"):
            self.package()
        self.assertFalse(list((self.root / "dist").glob("*.tar.gz")))

    def test_checksum_manifest_is_used_for_older_assets(self):
        del self.asset["digest"]
        self.release["assets"].append({"name": "SHA256SUMS", "browser_download_url": "https://example.invalid/SHA256SUMS"})
        self.manifest = self.digest + "  " + self.asset["name"] + "\n"
        self.package()
        self.assertTrue(any(url.endswith("SHA256SUMS") for url in self.requests))

    def test_manifest_missing_asset_fails(self):
        del self.asset["digest"]
        self.release["assets"].append({"name": "SHA256SUMS", "browser_download_url": "https://example.invalid/SHA256SUMS"})
        self.manifest = self.digest + "  unrelated.tar.gz\n"
        with self.assertRaisesRegex(ValueError, "does not contain"):
            self.package()

    def test_legacy_release_notes_checksum(self):
        del self.asset["digest"]
        self.release["body"] = self.digest + "  " + self.asset["name"]
        self.package()
        metadata = json.loads((self.root / "dist/mp-core-all-amd64.tar.gz.source.json").read_text())
        self.assertTrue(metadata["upstream_checksum_verified"])

    def test_missing_asset_fails_without_publishing(self):
        self.pages = [[]]
        with self.assertRaisesRegex(ValueError, "No stable public release"):
            self.package()

    def test_missing_browser_rejects_upstream_structure(self):
        with tarfile.open(self.raw, "w:gz") as tar:
            tar.addfile(tarfile.TarInfo("not-chrome"))
        self.asset.pop("digest")
        self.asset.pop("size")
        with self.assertRaisesRegex(ValueError, "executable chrome"):
            self.package()

    def test_download_failure_keeps_existing_file_and_cleans_partial(self):
        target = self.root / "download.tar.gz"
        target.write_bytes(b"existing")
        class BrokenResponse:
            def __enter__(self):
                self.reads = 0
                return self

            def __exit__(self, *args):
                return False

            def read(self, size):
                self.reads += 1
                if self.reads == 1:
                    return b"partial"
                raise URLError("connection interrupted")

        with patch.object(packager, "urlopen", side_effect=lambda *args, **kwargs: BrokenResponse()) as request:
            with patch.object(packager.time, "sleep"):
                with self.assertRaises(URLError):
                    packager.download("https://example.invalid/core", target)
        self.assertEqual(request.call_count, 3)
        self.assertEqual(target.read_bytes(), b"existing")
        self.assertFalse(target.with_name(target.name + ".part").exists())


if __name__ == "__main__":
    unittest.main()

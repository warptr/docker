#!/usr/bin/env python3
"""Package the latest stable, public CloakBrowser asset for one architecture."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import tarfile
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


API_ROOT = "https://api.github.com/repos/CloakHQ/CloakBrowser/releases"


def download(url, destination, *, api=False):
    headers = {"User-Agent": "moviepilot-core-packager"}
    if api:
        headers["Accept"] = "application/vnd.github+json"
        if os.environ.get("GH_TOKEN"):
            headers["Authorization"] = "Bearer " + os.environ["GH_TOKEN"]
    partial = destination.with_name(destination.name + ".part")
    for attempt in range(3):
        try:
            deadline = time.monotonic() + 900
            with urlopen(Request(url, headers=headers), timeout=30) as response:
                with partial.open("wb") as output:
                    while chunk := response.read(1024 * 1024):
                        if time.monotonic() > deadline:
                            raise TimeoutError("Download exceeded 900 seconds")
                        output.write(chunk)
            if not partial.stat().st_size:
                raise ValueError("Empty response: " + url)
            partial.replace(destination)
            return
        except (HTTPError, URLError, TimeoutError, OSError, ValueError):
            partial.unlink(missing_ok=True)
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)


def select_release(asset_name, work):
    page = 1
    while True:
        response = work / "releases.json"
        download(f"{API_ROOT}?per_page=100&page={page}", response, api=True)
        releases = json.loads(response.read_text())
        if not isinstance(releases, list):
            raise ValueError("Unexpected GitHub releases response")
        if not releases:
            raise ValueError("No stable public release contains " + asset_name)
        for release in releases:
            if release.get("draft") or release.get("prerelease"):
                continue
            for asset in release.get("assets", []):
                if asset["name"] == asset_name:
                    return release, asset
        page += 1


def release_version(release):
    # Use the release tag, independent of download URL shape or Chromium major.
    match = re.search(r"(?<![\d.])(\d+(?:\.\d+){3,4})(?![\d.])", release["tag_name"])
    if not match:
        raise ValueError("No Chromium version in release tag: " + release["tag_name"])
    return match.group(1)


def source_checksum(release, asset, work):
    digest = asset.get("digest") or ""
    if re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
        return digest.split(":", 1)[1].lower()
    manifest = next((a for a in release["assets"] if a["name"] == "SHA256SUMS"), None)
    if manifest:
        path = work / "SHA256SUMS"
        download(manifest["browser_download_url"], path)
        text = path.read_text()
    else:
        text = release.get("body") or ""
    pattern = r"(?m)^\s*([0-9a-fA-F]{64})[ \t]+\*?" + re.escape(asset["name"]) + r"[ \t]*$"
    match = re.search(pattern, text)
    if not match:
        if manifest:
            raise ValueError("Checksum manifest does not contain " + asset["name"])
        print("Warning: historical upstream asset has no SHA256; checking archive structure.")
        return None
    return match.group(1).lower()


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def package_core(architecture, destination):
    upstream_arch = {"amd64": "x64", "arm64": "arm64"}[architecture]
    asset_name = f"cloakbrowser-linux-{upstream_arch}.tar.gz"
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="mp-core-") as temp:
        work = Path(temp)
        release, asset = select_release(asset_name, work)
        version = release_version(release)
        expected = source_checksum(release, asset, work)
        archive = work / "raw-core.tar.gz"
        download(asset["browser_download_url"], archive)
        if asset.get("size") and archive.stat().st_size != asset["size"]:
            raise ValueError("Upstream asset size mismatch")
        source_hash = sha256(archive)
        if expected and source_hash != expected:
            raise ValueError("Upstream asset SHA256 mismatch")
        stage = work / "stage"
        browser = stage / f"chromium-{version}"
        browser.mkdir(parents=True)
        with tarfile.open(archive, "r:gz") as source:
            source.extractall(browser, filter="data")
        chrome = browser / "chrome"
        if not chrome.is_file() or not os.access(chrome, os.X_OK):
            raise ValueError("Upstream archive does not contain executable chrome")
        for marker in [".last_update_check", ".welcome_shown"]:
            (stage / marker).touch()
        target = destination / f"mp-core-all-{architecture}.tar.gz"
        with tarfile.open(target, "w:gz") as result:
            for entry in sorted(stage.iterdir()):
                result.add(entry, arcname=entry.name)
        digest = sha256(target)
        target.with_name(target.name + ".sha256").write_text(f"{digest}  {target.name}\n")
        target.with_name(target.name + ".source.json").write_text(json.dumps({
            "architecture": architecture,
            "version": version,
            "upstream_release": release["tag_name"],
            "upstream_url": asset["browser_download_url"],
            "upstream_sha256": source_hash,
            "upstream_checksum_verified": expected is not None,
            "sha256": digest,
        }, indent=2) + "\n")
        print(f"Packaged {target.name}: Chromium {version}, SHA256 {digest}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--architecture", choices=["amd64", "arm64"], required=True)
    parser.add_argument("--output", type=Path, default=Path("dist"))
    args = parser.parse_args()
    package_core(args.architecture, args.output)

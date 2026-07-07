#!/usr/bin/env python3
"""Download and install the matching static ffmpeg-multikey release asset."""

from __future__ import annotations

import argparse
import json
import os
import platform
import stat
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import urlparse


ARCH_TOKENS = {
    "amd64": ("amd64", "x86_64", "x64"),
    "x86_64": ("amd64", "x86_64", "x64"),
    "arm64": ("arm64", "aarch64"),
    "aarch64": ("arm64", "aarch64"),
}


def log(message: str) -> None:
    print(f"[ffmpeg-multikey] {message}", flush=True)


def request_json(url: str) -> dict:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "tr-eventmux-installer",
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def download(url: str, target: Path) -> None:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "tr-eventmux-installer"},
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        target.write_bytes(response.read())


def architecture_tokens(explicit_arch: str = "", variant: str = "") -> tuple[str, ...]:
    arch = explicit_arch.strip().lower()
    normalized_variant = variant.strip().lower().lstrip("v")
    if not arch:
        arch = platform.machine().strip().lower()
    if arch in ARCH_TOKENS:
        return ARCH_TOKENS[arch]
    if arch in {"arm", "armv7l", "armv6l"}:
        if normalized_variant == "7" or arch == "armv7l":
            return ("armv7", "armhf", "arm-7", "armv7l")
        if normalized_variant == "6" or arch == "armv6l":
            return ("armv6", "armel", "arm-6")
        return ("arm",)
    return (arch,)


def is_archive(name: str) -> bool:
    lowered = name.lower()
    return lowered.endswith((".tar.xz", ".txz", ".tar.gz", ".tgz", ".zip"))


def asset_score(name: str, tokens: tuple[str, ...]) -> int:
    lowered = name.lower()
    if "static" not in lowered or not is_archive(lowered):
        return -1
    if any(
        value in lowered
        for value in ("windows", "win64", "win32", "macos", "darwin", "android")
    ):
        return -1
    if not any(token in lowered for token in tokens):
        return -1
    score = 10
    if "linux" in lowered:
        score += 5
    if "_static" in lowered or "-static" in lowered:
        score += 2
    return score


def release_asset_url(
    repository: str,
    version: str,
    explicit_url: str,
    tokens: tuple[str, ...],
) -> str:
    if explicit_url:
        log(f"using explicit asset URL: {explicit_url}")
        return explicit_url
    api_url = (
        f"https://api.github.com/repos/{repository}/releases/latest"
        if version == "latest"
        else f"https://api.github.com/repos/{repository}/releases/tags/{version}"
    )
    log(f"querying {api_url}")
    log(f"looking for architecture tokens: {', '.join(tokens)}")
    release = request_json(api_url)
    candidates = []
    for asset in release.get("assets", []):
        name = str(asset.get("name", ""))
        url = str(asset.get("browser_download_url", ""))
        score = asset_score(name, tokens)
        if score >= 0 and url:
            candidates.append((score, name, url))
    if not candidates:
        available = [
            str(asset.get("name", ""))
            for asset in release.get("assets", [])
            if "static" in str(asset.get("name", "")).lower()
        ]
        listing = "\n  ".join(available) or "(none)"
        raise RuntimeError(
            "No matching static archive found. Available static assets:\n"
            f"  {listing}"
        )
    candidates.sort(reverse=True)
    _, name, url = candidates[0]
    log(f"selected asset: {name}")
    return url


def safe_member_name(name: str) -> str:
    return Path(name.replace("\\", "/")).name


def make_executable(path: Path) -> None:
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def install_from_tar(archive_path: Path, install_dir: Path) -> set[str]:
    installed: set[str] = set()
    with tarfile.open(archive_path, mode="r:*") as archive:
        for member in archive.getmembers():
            basename = safe_member_name(member.name)
            if basename not in {"ffmpeg", "ffprobe"} or not member.isfile():
                continue
            source = archive.extractfile(member)
            if source is None:
                continue
            target = install_dir / basename
            target.write_bytes(source.read())
            make_executable(target)
            installed.add(basename)
    return installed


def install_from_zip(archive_path: Path, install_dir: Path) -> set[str]:
    installed: set[str] = set()
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.infolist():
            basename = safe_member_name(member.filename)
            if basename not in {"ffmpeg", "ffprobe"} or member.is_dir():
                continue
            target = install_dir / basename
            target.write_bytes(archive.read(member))
            make_executable(target)
            installed.add(basename)
    return installed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--install-dir", default="/opt/ffmpeg")
    parser.add_argument(
        "--repository",
        default=os.environ.get(
            "FFMPEG_MULTIKEY_REPO", "DEvmIb/ffmpeg-multikey"
        ),
    )
    parser.add_argument(
        "--version",
        default=os.environ.get("FFMPEG_MULTIKEY_VERSION", "latest"),
    )
    parser.add_argument(
        "--asset-url",
        default=os.environ.get("FFMPEG_MULTIKEY_ASSET_URL", ""),
    )
    parser.add_argument(
        "--arch",
        default=os.environ.get("TARGETARCH", ""),
    )
    parser.add_argument(
        "--variant",
        default=os.environ.get("TARGETVARIANT", ""),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    install_dir = Path(args.install_dir).resolve()
    install_dir.mkdir(parents=True, exist_ok=True)
    tokens = architecture_tokens(args.arch, args.variant)
    url = release_asset_url(
        args.repository.strip(),
        args.version.strip(),
        args.asset_url.strip(),
        tokens,
    )
    suffix = Path(urlparse(url).path).name or "ffmpeg-multikey-archive"
    with tempfile.TemporaryDirectory() as temporary:
        archive_path = Path(temporary) / suffix
        log(f"downloading {url}")
        download(url, archive_path)
        installed = (
            install_from_zip(archive_path, install_dir)
            if zipfile.is_zipfile(archive_path)
            else install_from_tar(archive_path, install_dir)
        )
    if "ffmpeg" not in installed:
        raise RuntimeError("Archive did not contain an ffmpeg binary")
    log(f"installed: {', '.join(sorted(installed))}")
    subprocess.run(
        [str(install_dir / "ffmpeg"), "-hide_banner", "-version"],
        check=True,
    )


if __name__ == "__main__":
    main()

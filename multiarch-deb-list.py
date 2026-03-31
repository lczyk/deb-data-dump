#!/usr/bin/env python3

import argparse
import gzip
import io
import logging
import os
import subprocess as sub
import sys
import tempfile
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Literal

COLORS = {
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "reset": "\033[0m",
}

Arch = Literal["amd64", "i386", "arm64", "riscv64", "armhf", "ppc64el", "s390x"]
ARCHES: list[Arch] = ["amd64", "i386", "arm64", "riscv64", "armhf", "ppc64el", "s390x"]


def _arch_base_url(arch: Arch) -> str:
    """Return the Ubuntu mirror base URL for the given architecture."""
    if arch in ("amd64", "i386"):
        return "http://archive.ubuntu.com/ubuntu"
    return "http://ports.ubuntu.com/ubuntu-ports"


def _fetch_url(url: str, timeout: int = 30) -> bytes | None:
    """Fetch a URL and return its content as bytes, or None on failure."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.read()
    except Exception as e:
        logging.warning("Could not fetch %s: %s", url, e)
        return None


def _parse_packages_gz(data: bytes, package: str) -> dict[str, str] | None:
    """Parse a Packages.gz blob and return the stanza for the given package name."""
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as fh:
            text = fh.read().decode("utf-8", errors="replace")
    except Exception:
        text = data.decode("utf-8", errors="replace")

    current: dict[str, str] = {}
    last_key: str | None = None
    for line in text.splitlines():
        if not line:
            if current.get("Package") == package:
                return current
            current = {}
            last_key = None
        elif line[0] == " " and last_key is not None:
            current[last_key] = current[last_key] + "\n" + line[1:]
        elif ":" in line:
            idx = line.index(":")
            key = line[:idx]
            value = line[idx + 2 :] if len(line) > idx + 1 else ""
            current[key] = value
            last_key = key
    if current.get("Package") == package:
        return current
    return None


def download_deb(arch: Arch, package: str, suite: str, workdir: Path) -> Path | None:
    """Download a .deb file directly from Ubuntu mirrors."""
    base_url = _arch_base_url(arch)
    suites_to_try = [f"{suite}-updates", f"{suite}-security", suite]
    components = ["main", "universe", "restricted", "multiverse"]

    entry: dict[str, str] | None = None
    for try_suite in suites_to_try:
        if entry:
            break
        for component in components:
            url = f"{base_url}/dists/{try_suite}/{component}/binary-{arch}/Packages.gz"
            data = _fetch_url(url)
            if data is None:
                continue
            entry = _parse_packages_gz(data, package)
            if entry:
                break

    if not entry:
        logging.warning(
            "Package '%s' not found for arch '%s' in any suite/component.",
            package,
            arch,
        )
        return None

    filename = entry.get("Filename")
    if not filename:
        logging.warning("No Filename field for '%s' on arch '%s'.", package, arch)
        return None

    deb_url = f"{base_url}/{filename}"
    deb_dest = workdir / f"{package}_{arch}.deb"
    logging.info("Downloading %s ...", deb_url)
    try:
        urllib.request.urlretrieve(deb_url, str(deb_dest))
        return deb_dest
    except Exception as e:
        logging.warning("Failed to download %s: %s", deb_url, e)
        return None


def list_deb_contents(deb_path: Path) -> list[str]:
    """List non-directory file paths inside a .deb using dpkg-deb."""
    result = sub.run(["dpkg-deb", "-c", str(deb_path)], capture_output=True, text=True)
    files: list[str] = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 6:
            file_path = " ".join(parts[5:]).rstrip().lstrip("./")
            file_path = "/" + file_path if not file_path.startswith("/") else file_path
            if not file_path.endswith("/"):
                files.append(file_path)
    return files


def main():
    parser = argparse.ArgumentParser(
        description="List contents of a Debian package across multiple architectures."
    )
    parser.add_argument("package", help="Name of the Debian package to inspect.")
    parser.add_argument("suite", help="Ubuntu suite name (e.g., plucky, oracular)")
    parser.add_argument(
        "--arch",
        action="append",
        default=[],
        help="Architecture to process (can be specified multiple times). Default: all.",
    )
    args = parser.parse_args()

    requested_arches: list[Arch] = []
    if args.arch:
        for item in args.arch:
            for a in item.split(","):
                a = a.strip().lower()
                if a:
                    requested_arches.append(a)
    if not requested_arches:
        requested_arches = list(ARCHES)

    package = args.package
    suite = args.suite

    with tempfile.TemporaryDirectory() as tmpdir:
        workdir = Path(tmpdir)

        def process_arch(arch: Arch):
            logging.info("Processing %s on %s (%s)", package, arch, suite)
            deb_path = download_deb(arch, package, suite, workdir)
            if deb_path is None:
                logging.warning("Skipping %s: download failed.", arch)
                return
            contents = list_deb_contents(deb_path)
            out_file = f"{package}-{arch}-{suite}.txt"
            with open(out_file, "w") as f:
                for line in contents:
                    f.write(line + "\n")
            logging.info("Wrote %s (%d files)", out_file, len(contents))

        clamp = lambda x, lo, hi: max(lo, min(hi, x))
        executor = ThreadPoolExecutor(max_workers=clamp(len(requested_arches), 1, 8))
        try:
            futures = [executor.submit(process_arch, arch) for arch in requested_arches]
            for future in as_completed(futures):
                future.result()
        except KeyboardInterrupt:
            executor.shutdown(wait=False, cancel_futures=True)
            raise
        else:
            executor.shutdown()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stderr)],
    )
    logging.addLevelName(logging.INFO, f"{COLORS['green']}INFO{COLORS['reset']}")
    logging.addLevelName(logging.WARNING, f"{COLORS['yellow']}WARNING{COLORS['reset']}")
    logging.addLevelName(logging.ERROR, f"{COLORS['red']}ERROR{COLORS['reset']}")

    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        os._exit(130)

#!/usr/bin/env python3
import argparse
import gzip
import io
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Literal

Arch = Literal["amd64", "i386", "arm64", "riscv64", "armhf", "ppc64el", "s390x"]
ARCHES: set[Arch] = {"amd64", "i386", "arm64", "riscv64", "armhf", "ppc64el", "s390x"}


def get_suite(chisel_release_path: Path):
    chisel_yaml = chisel_release_path / "chisel.yaml"
    result = subprocess.run(
        ["yq", ".archives.ubuntu.suites[0]", str(chisel_yaml)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logging.error(
            f"Could not get suite from {chisel_yaml}",
        )
        sys.exit(1)
    return result.stdout.strip()


def get_slices_from_sdf(package: str, chisel_release_path: Path):
    sdf_file = chisel_release_path / "slices" / f"{package}.yaml"
    if not sdf_file.exists():
        logging.error("SDF file not found for package '%s': %s", package, sdf_file)
        sys.exit(1)
    result = subprocess.run(
        ["yq", "-r", ".slices | keys[]", str(sdf_file)], capture_output=True, text=True
    )
    if result.returncode != 0:
        logging.error("Could not parse slices from SDF file: %s", sdf_file)
        sys.exit(1)
    slices = result.stdout.strip().split("\n")
    return [s for s in slices if s]


def get_files(directory):
    files = []
    for path in Path(directory).rglob("*"):
        if path.is_file() or path.is_symlink():
            files.append("/" + str(path.relative_to(directory)))
    files.sort()
    return files


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
        logging.warning(f"Warning: could not fetch {url}: {e}")
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


def download_all_debs(
    workdir: Path,
    package: str,
    arches: set[Arch],
    suite: str,
) -> dict[Arch, Path]:
    """Download .deb files directly from Ubuntu mirrors without Docker."""
    suites_to_try = [f"{suite}-updates", f"{suite}-security", suite]
    components = ["main", "universe", "restricted", "multiverse"]

    deb_dict: dict[Arch, Path] = {}

    def download_for_arch(arch: Arch) -> tuple[Arch, Path | None]:
        base_url = _arch_base_url(arch)
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
                f"Package '{package}' not found for arch '{arch}' in any suite/component."
            )
            return arch, None

        filename = entry.get("Filename")
        if not filename:
            logging.warning(f"No Filename field for '{package}' on arch '{arch}'.")
            return arch, None

        deb_url = f"{base_url}/{filename}"
        (workdir / arch).mkdir(parents=True, exist_ok=True)
        deb_dest = workdir / arch / Path(filename).name
        logging.info("Downloading %s ...", deb_url)
        try:
            urllib.request.urlretrieve(deb_url, str(deb_dest))
            logging.info("Saved to %s", deb_dest.name)
            return arch, deb_dest
        except Exception as e:
            logging.warning("Failed to download %s: %s", deb_url, e)
            return arch, None

    clamp = lambda x, min_val, max_val: max(min_val, min(max_val, x))
    executor = ThreadPoolExecutor(max_workers=clamp(len(arches), 1, 8))
    try:
        futures = [executor.submit(download_for_arch, arch) for arch in arches]
        for future in as_completed(futures):
            arch, deb_path = future.result()
            if deb_path is not None:
                deb_dict[arch] = deb_path
    except KeyboardInterrupt:
        executor.shutdown(wait=False, cancel_futures=True)
        raise
    else:
        executor.shutdown()

    return deb_dict


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check slice coverage for a Debian package across architectures."
    )
    parser.add_argument(
        "chisel_releases",
        help="Path to the chisel-releases folder (contains chisel.yaml)",
    )
    parser.add_argument(
        "package", help="Package name to check coverage against (e.g., libc6)"
    )
    parser.add_argument(
        "--slice", action="append", help="Slices to check (default: all from SDF)"
    )

    parser.add_argument(
        "--arch",
        action="append",
        default=[],
        help="Architecture to process (e.g., amd64). Can be specified multiple times or as a comma-separated list. Default is all arches.",
    )
    parser.add_argument(
        "--ignore",
        action="append",
        default=[],
        help="Paths to ignore in uncovered files (e.g., ./usr/lib/). Can be specified multiple times or as a comma-separated list.",
    )
    parser.add_argument(
        "--workdir",
        help="Optional working directory. If omitted, a temporary directory is used.",
    )
    parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="When used with --workdir, remove the existing directory before running.",
    )

    args = parser.parse_args()

    # process arches
    processed_arch: set[Arch] = set()
    for item in args.arch:
        _arches = [arch.strip().lower() for arch in item.split(",")]
        _arches = [arch for arch in _arches if arch]
        for arch in _arches:
            if arch == "ppc64le":
                logging.warning(
                    "Mapping 'ppc64le' to 'ppc64el' for compatibility with Ubuntu naming."
                )
                arch = "ppc64el"
            if arch not in ARCHES and arch != "all":
                parser.error(
                    f"Invalid arch: {arch}. Valid options: {', '.join(ARCHES)} or 'all'"
                )
            if arch == "all":
                processed_arch.update(ARCHES)
            else:
                processed_arch.add(arch)

    if not processed_arch:
        processed_arch.update(ARCHES)

    args.arch = sorted(processed_arch)

    # process ignores
    processed_ignores: set[str] = set()
    for item in args.ignore:
        _paths = [path.strip() for path in item.split(",")]
        _paths = [path for path in _paths if path]
        processed_ignores.update(_paths)
    args.ignore = sorted(processed_ignores)

    if args.force and not args.workdir:
        logging.warning("--force has no effect when --workdir is not specified.")

    return args


def main(args: argparse.Namespace) -> None:
    chisel_version = subprocess.run(
        ["chisel", "--version"], capture_output=True, text=True
    )
    if chisel_version.returncode != 0:
        logging.error("chisel is not installed or not in PATH.")
        sys.exit(1)

    logging.info("Using chisel version: %s", chisel_version.stdout.strip())
    chisel_release_path = Path(args.chisel_releases).resolve()
    if not chisel_release_path.is_dir():
        logging.error(
            "chisel-releases path '%s' is not a directory.", chisel_release_path
        )
        sys.exit(1)
    if not (chisel_release_path / "chisel.yaml").exists():
        logging.error(
            "chisel.yaml not found in chisel-releases path '%s'.", chisel_release_path
        )
        sys.exit(1)

    if args.workdir:
        workdir = Path(args.workdir).resolve()
        cleanup_workdir = False

        if workdir.exists() and not workdir.is_dir():
            logging.error("workdir path '%s' exists and is not a directory.", workdir)
            sys.exit(1)

        if workdir.exists() and args.force:
            shutil.rmtree(workdir, ignore_errors=True)
        elif workdir.exists() and any(workdir.iterdir()):
            logging.error(
                "workdir '%s' already exists and is not empty. Use --force to overwrite it.",
                workdir,
            )
            sys.exit(1)
    else:
        workdir = Path(tempfile.mkdtemp())
        cleanup_workdir = True
    workdir.mkdir(parents=True, exist_ok=True)

    suite = get_suite(chisel_release_path)
    logging.info("Suite: %s", suite)

    # Process slice: split on commas and flatten
    if not args.slice:
        logging.info("No slices specified, parsing sdf file")
        slices = get_slices_from_sdf(args.package, chisel_release_path)
    else:
        slice_list = [
            s.strip() for item in args.slice for s in item.split(",") if s.strip()
        ]
        if "all" in slice_list:
            print("Slices set to 'all', parsing sdf file...")
            slices = get_slices_from_sdf(args.package, chisel_release_path)
        else:
            slices = slice_list

    # Strip package_ from slices if present
    slices = [s.replace(args.package + "_", "", 1) for s in slices]

    # Verify no underscores in slice names
    for slice in slices:
        if "_" in slice:
            logging.error(
                f"Slice name '{slice}' contains underscore '_'. Slice names should not contain underscores.",
            )
            sys.exit(1)

    logging.info("Slices: %s", ", ".join(slices))

    # Prepend package name
    slices_to_cut = [f"{args.package}_{slice}" for slice in slices]

    # Download all debs
    logging.info("Downloading .debs for %d architectures...", len(args.arch))
    logging.info("Architectures: %s", ", ".join(args.arch))
    deb_files = download_all_debs(workdir, args.package, args.arch, suite)

    # Prepare directories
    arch_data = {}
    for arch in args.arch:
        archdir = workdir / arch
        rootfs_dir = archdir / "rootfs"
        deb_extract_dir = archdir / "deb_extract"

        # Clean old data
        shutil.rmtree(rootfs_dir, ignore_errors=True)
        shutil.rmtree(deb_extract_dir, ignore_errors=True)
        rootfs_dir.mkdir(parents=True, exist_ok=True)
        deb_extract_dir.mkdir(parents=True, exist_ok=True)

        arch_data[arch] = {
            "archdir": archdir,
            "rootfs_dir": rootfs_dir,
            "deb_extract_dir": deb_extract_dir,
        }

    def run_chisel(
        arch: str,
        rootfs_dir: Path,
        slices_to_cut: list[str],
    ) -> tuple[str, set[str], int, str]:
        cmd = [
            "chisel",
            "cut",
            "--arch",
            arch,
            "--release",
            str(chisel_release_path),
            "--root",
            str(rootfs_dir),
            "--ignore=unmaintained",
            "--ignore=unstable",
        ] + slices_to_cut
        result = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
        cut_packages = set(
            re.findall(r'Extracting files from package "([^"]+)"', result.stdout)
        )
        return arch, cut_packages, result.returncode, result.stdout

    # Run chisel cuts in parallel
    logging.info(
        "Cutting %d slices on %d architectures", len(slices_to_cut), len(args.arch)
    )
    executor = ThreadPoolExecutor(max_workers=len(args.arch))
    try:
        futures = [
            executor.submit(run_chisel, arch, data["rootfs_dir"], slices_to_cut)
            for arch, data in arch_data.items()
        ]
        chisel_results: dict[str, set[str]] = {}
        for future in futures:
            arch, cut_packages, code, stdout = future.result()
            if code != 0:
                logging.error(f"Error running chisel for {arch}, skipping...")
                logging.error(stdout)
                continue
            chisel_results[arch] = cut_packages
    except KeyboardInterrupt:
        executor.shutdown(wait=False, cancel_futures=True)
        raise
    else:
        executor.shutdown()

    for arch in args.arch:
        data = arch_data[arch]
        deb_file_src = deb_files.get(arch)
        if deb_file_src is None:
            continue
        data["deb_file"] = deb_file_src

    def run_extract(
        arch: Arch, deb_file: Path, deb_extract_dir: Path
    ) -> tuple[Arch, int]:
        result = subprocess.run(["dpkg-deb", "-x", str(deb_file), str(deb_extract_dir)])
        return arch, result.returncode

    # Run extractions in parallel
    logging.info("Extracting debs for %d architectures", len(args.arch))
    executor = ThreadPoolExecutor(max_workers=len(args.arch))
    try:
        futures = [
            executor.submit(
                run_extract, arch, data["deb_file"], data["deb_extract_dir"]
            )
            for arch, data in arch_data.items()
            if "deb_file" in data
        ]
        extract_results: dict[Arch, int] = {}
        for future in futures:
            arch, result = future.result()
            if result != 0:
                logging.error("Failed to extract deb for %s", arch)
            extract_results[arch] = result
    except KeyboardInterrupt:
        executor.shutdown(wait=False, cancel_futures=True)
        raise
    else:
        executor.shutdown()

    # Process each architecture
    for arch in args.arch:
        data = arch_data[arch]
        archdir = data["archdir"]
        rootfs_dir = data["rootfs_dir"]
        deb_extract_dir = data["deb_extract_dir"]

        cut_packages = chisel_results.get(arch, set())
        if not cut_packages:
            logging.warning("No packages cut")
            continue

        # Check extraction
        extract_result = extract_results.get(arch)
        if extract_result is None:
            logging.error("Failed to extract deb for %s", arch)
            continue

        # Compare files
        deb_files_list = get_files(deb_extract_dir)
        rootfs_files_list = get_files(rootfs_dir)

        # Save file lists
        with open(archdir / "deb_files.txt", "w") as f:
            for file in deb_files_list:
                f.write(file + "\n")
        with open(archdir / "rootfs_files.txt", "w") as f:
            for file in rootfs_files_list:
                f.write(file + "\n")

        # Calculate coverage
        deb_set = set(deb_files_list)
        rootfs_set = set(rootfs_files_list)
        covered = deb_set & rootfs_set
        all_uncovered = deb_set - rootfs_set

        # Filter ignored paths
        uncovered = {
            f
            for f in all_uncovered
            if not any(f.startswith(ignore) for ignore in args.ignore)
        }

        ignored = all_uncovered - uncovered

        covered_count = len(covered)
        ignored_count = len(ignored)
        total = len(deb_files_list)
        effective_total = total - ignored_count
        percent = (100 * covered_count // effective_total) if effective_total else 0

        # Save covered and uncovered
        with open(archdir / "covered.txt", "w") as f:
            for file in sorted(covered):
                f.write(file + "\n")
        with open(archdir / "uncovered.txt", "w") as f:
            for file in sorted(uncovered):
                f.write(file + "\n")
        with open(archdir / "ignored.txt", "w") as f:
            for file in sorted(ignored):
                f.write(file + "\n")

        logging.info(
            "Coverage for %s: %d / %d files (%d%%) [ignored: %d]",
            arch,
            covered_count,
            effective_total,
            percent,
            ignored_count,
        )
        logging.info("Uncovered files for %s:", arch)
        for file in sorted(uncovered):
            print(file)
        print()

    # Clean up only when we created a temporary workdir.
    if cleanup_workdir:
        shutil.rmtree(workdir, ignore_errors=True)


COLORS = {
    "green": "\033[32m",
    "red": "\033[31m",
    "yellow": "\033[33m",
    "reset": "\033[0m",
}

if __name__ == "__main__":
    # logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stderr)],
    )
    logging.addLevelName(logging.INFO, f"{COLORS['green']}INFO{COLORS['reset']}")
    logging.addLevelName(logging.WARNING, f"{COLORS['yellow']}WARNING{COLORS['reset']}")
    logging.addLevelName(logging.ERROR, f"{COLORS['red']}ERROR{COLORS['reset']}")

    args = parse_args()
    try:
        main(args)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        os._exit(130)

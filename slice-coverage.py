#!/usr/bin/env python3

import argparse
import gzip
import io
import subprocess
import sys
import shutil
import re
import tempfile
import urllib.request
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

ARCHES = ["amd64", "i386", "arm64", "riscv64", "armhf", "ppc64el", "s390x"]

def get_suite(chisel_release_path: Path):
    chisel_yaml = chisel_release_path / "chisel.yaml"
    result = subprocess.run(
        ["yq", ".archives.ubuntu.suites[0]", str(chisel_yaml)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(
            f"Error getting suite from {chisel_yaml}",
            file=sys.stderr,
        )
        sys.exit(1)
    return result.stdout.strip()

def get_slices_from_sdf(package: str, chisel_release_path: Path):
    sdf_file = chisel_release_path / "slices" / f"{package}.yaml"
    if not sdf_file.exists():
        print(f"Error: SDF file '{sdf_file}' not found.", file=sys.stderr)
        sys.exit(1)
    result = subprocess.run(["yq", "-r", ".slices | keys[]", str(sdf_file)], capture_output=True, text=True)
    if result.returncode != 0:
        print("Error parsing slices", file=sys.stderr)
        sys.exit(1)
    slices = result.stdout.strip().split('\n')
    return [s for s in slices if s]

def get_files(directory):
    files = []
    for path in Path(directory).rglob("*"):
        if path.is_file() or path.is_symlink():
            files.append("/" + str(path.relative_to(directory)))
    files.sort()
    return files

def _arch_base_url(arch: str) -> str:
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
        print(f"Warning: could not fetch {url}: {e}", file=sys.stderr)
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
    arches: list[str],
    suite: str,
) -> dict[str, Path]:
    """Download .deb files directly from Ubuntu mirrors without Docker."""
    suites_to_try = [f"{suite}-updates", f"{suite}-security", suite]
    components = ["main", "universe", "restricted", "multiverse"]

    deb_dict: dict[str, Path] = {}

    def download_for_arch(arch: str) -> tuple[str, Path | None]:
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
            print(
                f"Warning: package '{package}' not found for arch '{arch}' in any suite/component.",
                file=sys.stderr,
            )
            return arch, None

        filename = entry.get("Filename")
        if not filename:
            print(
                f"Warning: no Filename field for '{package}' on arch '{arch}'.",
                file=sys.stderr,
            )
            return arch, None

        deb_url = f"{base_url}/{filename}"
        (workdir / arch).mkdir(parents=True, exist_ok=True)
        deb_dest = workdir / arch / Path(filename).name
        print(f"  [{arch}] Downloading {deb_url} ...")
        try:
            urllib.request.urlretrieve(deb_url, str(deb_dest))
            print(f"  [{arch}] Saved to {deb_dest.name}")
            return arch, deb_dest
        except Exception as e:
            print(f"Warning: failed to download {deb_url}: {e}", file=sys.stderr)
            return arch, None

    max_workers = max(1, min(8, len(arches)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(download_for_arch, arch) for arch in arches]
        for future in as_completed(futures):
            arch, deb_path = future.result()
            if deb_path is not None:
                deb_dict[arch] = deb_path

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
        "--arch", action="append", help="Architecture to process (default: all)"
    )
    parser.add_argument(
        "--ignore",
        action="append",
        default=[],
        help="Paths to ignore in uncovered files (e.g., ./usr/lib/)",
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

    return parser.parse_args()


def main(args: argparse.Namespace) -> None:
    chisel_version = subprocess.run(
        ["chisel", "--version"], capture_output=True, text=True
    )
    if chisel_version.returncode != 0:
        print("Error: chisel is not installed or not in PATH.", file=sys.stderr)
        sys.exit(1)

    print(f"Using chisel version: {chisel_version.stdout.strip()}")
    chisel_release_path = Path(args.chisel_releases).resolve()
    if not chisel_release_path.is_dir():
        print(
            f"Error: chisel-releases path '{chisel_release_path}' is not a directory.",
            file=sys.stderr,
        )
        sys.exit(1)
    if not (chisel_release_path / "chisel.yaml").exists():
        print(
            f"Error: '{chisel_release_path / 'chisel.yaml'}' not found.",
            file=sys.stderr,
        )
        sys.exit(1)

    package = args.package

    # Process ignore paths: split on commas and flatten
    ignore_paths = [
        path.strip()
        for item in args.ignore or []
        for path in item.split(",")
        if path.strip()
    ]

    if args.force and not args.workdir:
        print(
            "Error: --force can only be used together with --workdir.", file=sys.stderr
        )
        sys.exit(1)

    if args.workdir:
        workdir = Path(args.workdir).resolve()
        cleanup_workdir = False

        if workdir.exists() and not workdir.is_dir():
            print(
                f"Error: workdir path '{workdir}' exists and is not a directory.",
                file=sys.stderr,
            )
            sys.exit(1)

        if workdir.exists() and args.force:
            shutil.rmtree(workdir, ignore_errors=True)
        elif workdir.exists() and any(workdir.iterdir()):
            print(
                f"Error: workdir '{workdir}' already exists and is not empty. "
                "Use --force to overwrite it.",
                file=sys.stderr,
            )
            sys.exit(1)
    else:
        workdir = Path(tempfile.mkdtemp())
        cleanup_workdir = True
    workdir.mkdir(parents=True, exist_ok=True)

    suite = get_suite(chisel_release_path)
    print(f"Suite: {suite}")

    # Process slice: split on commas and flatten
    if not args.slice:
        print("--- No slices specified, parsing sdf file...")
        slices = get_slices_from_sdf(package, chisel_release_path)
    else:
        slice_list = [
            s.strip() for item in args.slice for s in item.split(",") if s.strip()
        ]
        if "all" in slice_list:
            print("Slices set to 'all', parsing sdf file...")
            slices = get_slices_from_sdf(package, chisel_release_path)
        else:
            slices = slice_list

    # Process arch: split on commas and flatten
    if not args.arch:
        arches_to_process = ARCHES
    else:
        arch_list = [
            arch.strip()
            for item in args.arch
            for arch in item.split(",")
            if arch.strip()
        ]
        if "all" in arch_list:
            arches_to_process = ARCHES
        else:
            arches_to_process = []
            for arch in arch_list:
                if arch not in ARCHES:
                    print(
                        f"Invalid arch: {arch}. Valid options: {', '.join(ARCHES)} or 'all'",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                arches_to_process.append(arch)

    # Strip package_ from slices if present
    slices = [s.replace(f"{package}_", "", 1) for s in slices]

    # Verify no underscores in slice names
    for slice in slices:
        if "_" in slice:
            print(
                f"Error: Slice name '{slice}' contains underscore '_'. Slice names should not contain underscores.",
                file=sys.stderr,
            )
            sys.exit(1)

    print(f"Slices: {', '.join(slices)}")

    # Prepend package name
    slices_to_cut = [f"{package}_{slice}" for slice in slices]

    # Download all debs
    print(f"--- Downloading .debs for {len(arches_to_process)} architectures...")
    print(f"Architectures: {', '.join(arches_to_process)}")
    deb_files = download_all_debs(workdir, package, arches_to_process, suite)

    # Prepare directories
    arch_data = {}
    for arch in arches_to_process:
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

    def run_chisel(arch, rootfs_dir, slices_to_cut):
        print(f"--- Running chisel cut for {arch}...")
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
        extracted_packages = set(
            re.findall(r'Extracting files from package "([^"]+)"', result.stdout)
        )
        return arch, extracted_packages, result.returncode, result.stdout

    # Run chisel cuts in parallel
    print("--- Running chisel cuts for all architectures...")
    with ThreadPoolExecutor(max_workers=len(arches_to_process)) as executor:
        futures = [
            executor.submit(run_chisel, arch, data["rootfs_dir"], slices_to_cut)
            for arch, data in arch_data.items()
        ]
        chisel_results = {}
        for future in futures:
            arch, packages, code, stdout = future.result()
            chisel_results[arch] = (packages, code, stdout)

    # Copy debs to deb_dirs
    for arch in arches_to_process:
        data = arch_data[arch]
        deb_file_src = deb_files.get(arch)
        if deb_file_src is None:
            continue
        shutil.copy(str(deb_file_src), str(deb_file_src.name))
        data["deb_file"] = deb_file_src.name

    def run_extract(arch, deb_file, deb_extract_dir):
        print(f"--- Extracting .deb for {arch}...")
        result = subprocess.run(["dpkg-deb", "-x", str(deb_file), str(deb_extract_dir)])
        return arch, result

    # Run extractions in parallel
    print("--- Extracting .debs for all architectures...")
    with ThreadPoolExecutor(max_workers=len(arches_to_process)) as executor:
        futures = [
            executor.submit(
                run_extract, arch, data["deb_file"], data["deb_extract_dir"]
            )
            for arch, data in arch_data.items()
            if "deb_file" in data
        ]
        extract_results = {}
        for future in futures:
            arch, result = future.result()
            extract_results[arch] = result

    # Process each architecture
    for arch in arches_to_process:
        print(f"=== Processing architecture: {arch} ===")
        data = arch_data[arch]
        archdir = data["archdir"]
        rootfs_dir = data["rootfs_dir"]
        deb_extract_dir = data["deb_extract_dir"]

        packages, code, stdout = chisel_results[arch]
        if code != 0:
            print(f"Error running chisel for {arch}, skipping...", file=sys.stderr)
            print(stdout, file=sys.stderr)
            continue

        # Parse extracted packages from output
        if packages:
            print(", ".join(sorted(packages)))

        # Check extraction
        extract_result = extract_results.get(arch)
        if extract_result is None or extract_result.returncode != 0:
            print(f"Error extracting deb for {arch}, skipping...", file=sys.stderr)
            continue

        # Compare files
        print("--- Comparing files...")
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
            if not any(f.startswith(ignore) for ignore in ignore_paths)
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

        print(
            f"===> Coverage for {arch}: {covered_count} / {effective_total} files ({percent}%) [ignored: {ignored_count}]"
        )
        print(f"===> Uncovered files for {arch}:")
        for file in sorted(uncovered):
            print(file)
        print()

    # Clean up only when we created a temporary workdir.
    if cleanup_workdir:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    args = parse_args()
    main(args)
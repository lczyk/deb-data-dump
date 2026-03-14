#!/usr/bin/env python3

import argparse
import subprocess
import sys
import shutil
import re
import tempfile
from pathlib import Path

ARCHES = ["amd64", "i386", "arm64", "riscv64", "armhf", "ppc64el", "s390x"]

def get_suite():
    result = subprocess.run(["yq", ".archives.ubuntu.suites[0]", "chisel.yaml"], capture_output=True, text=True)
    if result.returncode != 0:
        print("Error getting suite from chisel.yaml", file=sys.stderr)
        sys.exit(1)
    return result.stdout.strip()

def get_slices_from_sdf(package):
    sdf_file = Path("slices") / f"{package}.yaml"
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

def download_deb(deb_dir, package, arch, suite, original_cwd):
    # Adjust arch for ppc64el
    download_arch = arch
    if arch == "ppc64el":
        download_arch = "ppc64le"

    download_dir = original_cwd / f"deb_download_{arch}"
    download_dir.mkdir(parents=True, exist_ok=True)

    work_path = f"/work/deb_download_{arch}"

    cmd = [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{original_cwd}:/work",
        "-w",
        work_path,
        f"ubuntu:{suite}",
    ]

    if arch in ["i386", "amd64"]:
        platform = "linux/amd64"
        bash_cmd = f"dpkg --add-architecture i386 && apt update && apt download {package}:{download_arch}"
    else:
        platform = f"linux/{download_arch}"
        bash_cmd = f"apt update && apt download {package}"

    cmd.extend(["--platform", platform])
    cmd.extend(["bash", "-c", bash_cmd])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"Error downloading deb for {arch}: {result.stderr}", file=sys.stderr)
            return None

        # Move the downloaded .deb to deb_dir
        deb_files = list(download_dir.glob("*.deb"))
        if deb_files:
            deb_path = deb_dir / deb_files[0].name
            shutil.move(str(deb_files[0]), str(deb_path))
            return deb_path
        else:
            return None
    finally:
        # Clean up download_dir
        shutil.rmtree(download_dir, ignore_errors=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check slice coverage for a Debian package across architectures."
    )
    parser.add_argument(
        "package", help="Package name to check coverage against (e.g., libc6)"
    )
    parser.add_argument("slices", nargs="*", help="Slices to check (optional)")
    parser.add_argument(
        "--arch", action="append", help="Architecture to process (default: all)"
    )
    parser.add_argument(
        "--ignore",
        action="append",
        default=[],
        help="Paths to ignore in uncovered files (e.g., ./usr/lib/)",
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
    package = args.package
    slices = args.slices

    original_cwd = Path.cwd()

    # Process ignore paths: split on commas and flatten
    ignore_paths = [path.strip() for item in args.ignore or [] for path in item.split(',') if path.strip()]

    temp_dir = tempfile.mkdtemp()
    workdir = Path(temp_dir)
    outdir = Path.cwd() / "out"
    workdir.mkdir(parents=True, exist_ok=True)
    outdir.mkdir(parents=True, exist_ok=True)
    
    suite = get_suite()
    
    # Process arch: split on commas and flatten
    if not args.arch:
        arches_to_process = ARCHES
    else:
        arch_list = [arch.strip() for item in args.arch for arch in item.split(',') if arch.strip()]
        if 'all' in arch_list:
            arches_to_process = ARCHES
        else:
            arches_to_process = []
            for arch in arch_list:
                if arch not in ARCHES:
                    print(f"Invalid arch: {arch}. Valid options: {', '.join(ARCHES)} or 'all'", file=sys.stderr)
                    sys.exit(1)
                arches_to_process.append(arch)
    
    # Strip package_ from slices if present
    slices = [s.replace(f"{package}_", "", 1) for s in slices]
    
    # If no slices provided, parse from sdf file
    if not slices:
        print("No slices provided, parsing sdf file...")
        slices = get_slices_from_sdf(package)
    
    # Verify no underscores in slice names
    for slice in slices:
        if "_" in slice:
            print(f"Error: Slice name '{slice}' contains underscore '_'. Slice names should not contain underscores.", file=sys.stderr)
            sys.exit(1)
    
    # Prepend package name
    slices_to_cut = [f"{package}_{slice}" for slice in slices]
    
    # Process each architecture
    for arch in arches_to_process:
        print(f"=== Processing architecture: {arch} ===")
        archdir = workdir / arch
        rootfs_dir = archdir / "rootfs"
        deb_dir = archdir / "deb"
        deb_extract_dir = archdir / "deb_extract"
        
        # Clean old data
        shutil.rmtree(rootfs_dir, ignore_errors=True)
        shutil.rmtree(deb_dir, ignore_errors=True)
        shutil.rmtree(deb_extract_dir, ignore_errors=True)
        rootfs_dir.mkdir(parents=True, exist_ok=True)
        deb_dir.mkdir(parents=True, exist_ok=True)
        deb_extract_dir.mkdir(parents=True, exist_ok=True)
        
        # Download the rootfs using chisel
        print("--- Running chisel cut...")
        cmd = ["chisel", "cut", "--arch", arch, "--release", "./", "--root", str(rootfs_dir), "--ignore=unmaintained", "--ignore=unstable"] + slices_to_cut
        result = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
        if result.returncode != 0:
            print(f"Error running chisel for {arch}, skipping...", file=sys.stderr)
            print(result.stdout, file=sys.stderr)
            continue

        # Parse extracted packages from output
        extracted_packages = re.findall(
            r'Extracting files from package "([^"]+)"', result.stdout
        )
        if extracted_packages:
            print(", ".join(sorted(set(extracted_packages))))

        # Download the deb
        print(f"--- Downloading .deb for {package}:{arch}...")
        deb_file = download_deb(deb_dir, package, arch, suite, original_cwd)
        if deb_file is None:
            print(f"Failed to download deb for {arch}, skipping...", file=sys.stderr)
            continue
        
        # Extract deb
        print("--- Extracting .deb...")
        result = subprocess.run(["dpkg-deb", "-x", str(deb_file), str(deb_extract_dir)])
        if result.returncode != 0:
            print(f"Error extracting deb for {arch}", file=sys.stderr)
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
        uncovered_count = len(uncovered)
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

    # Clean up temp directory
    shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    args = parse_args()
    main(args)
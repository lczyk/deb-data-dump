#!/usr/bin/env python3

import argparse
import subprocess
import sys
import shutil
import re
import tempfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

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

def download_all_debs(deb_temp_dir, package, arches, suite, original_cwd):
    download_dir = original_cwd / "deb_download_all"
    download_dir.mkdir(parents=True, exist_ok=True)

    work_path = "/work/deb_download_all"

    cmd = [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{original_cwd}:/work",
        "-w",
        work_path,
    ]

    platform = "linux/amd64"

    # Add all architectures except amd64 (default)
    add_arch_cmds = [
        f"dpkg --add-architecture {arch}" for arch in arches if arch != "amd64"
    ]

    # Add all arches to ubuntu.sources
    sed_part: list[str] = []

    # For ports, if any non-amd64/i386 arches
    non_amd64_arches = [a for a in arches if a not in ["amd64", "i386"]]
    if non_amd64_arches:
        ports_arches = " ".join(non_amd64_arches)
        ports_main_content = f"""
Types: deb
URIs: http://ports.ubuntu.com/ubuntu-ports/
Suites: {suite} {suite}-updates {suite}-backports
Components: main universe restricted multiverse
Architectures: {ports_arches}
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
"""
        ports_security_content = f"""
Types: deb
URIs: http://ports.ubuntu.com/ubuntu-ports/
Suites: {suite}-security
Components: main universe restricted multiverse
Architectures: {ports_arches}
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
"""
        sed_part = [
            "sed -i '/^Types: deb$/a Architectures: amd64 i386' /etc/apt/sources.list.d/ubuntu.sources",
            f"echo '{ports_main_content}' >> /etc/apt/sources.list.d/ubuntu.sources",
            f"echo '{ports_security_content}' >> /etc/apt/sources.list.d/ubuntu.sources",
        ]

    # apt update
    bash_parts = (
        add_arch_cmds
        + sed_part
        + [
            "apt update",
        ]
    )

    # Download each package for each arch
    download_cmds = [
        f"apt download {package}:{arch} -o=dir::cache={work_path}" for arch in arches
    ]
    bash_parts.extend(download_cmds)

    bash_cmd = " && ".join(bash_parts)

    cmd.extend(["--platform", platform])
    cmd.append(f"ubuntu:{suite}")
    cmd.extend(["bash", "-c", bash_cmd])

    try:
        result = subprocess.run(cmd)
        if result.returncode != 0:
            print(f"Error downloading debs: {result.stderr}", file=sys.stderr)
            return {}

        # Move debs to deb_temp_dir, keeping original names
        deb_files_list = list(download_dir.glob("*.deb"))
        deb_dict = {}
        for deb_file in deb_files_list:
            name = deb_file.name
            for arch in arches:
                if (
                    f"_{arch}.deb" in name or f":{arch}_" in name
                ):  # assuming format like package_version:arch.deb or package_version_arch.deb
                    target = deb_temp_dir / name  # keep original name
                    shutil.move(str(deb_file), str(target))
                    deb_dict[arch] = target
                    break
        return deb_dict
    finally:
        shutil.rmtree(download_dir, ignore_errors=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check slice coverage for a Debian package across architectures."
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

    original_cwd = Path.cwd()

    # Process ignore paths: split on commas and flatten
    ignore_paths = [
        path.strip()
        for item in args.ignore or []
        for path in item.split(",")
        if path.strip()
    ]

    temp_dir = tempfile.mkdtemp()
    workdir = Path(temp_dir)
    outdir = Path.cwd() / "out"
    workdir.mkdir(parents=True, exist_ok=True)
    outdir.mkdir(parents=True, exist_ok=True)

    suite = get_suite()

    # Process slice: split on commas and flatten
    if not args.slice:
        print("--- No slices specified, parsing sdf file...")
        slices = get_slices_from_sdf(package)
    else:
        slice_list = [
            s.strip() for item in args.slice for s in item.split(",") if s.strip()
        ]
        if "all" in slice_list:
            print("Slices set to 'all', parsing sdf file...")
            slices = get_slices_from_sdf(package)
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
    deb_temp_dir = workdir / "debs"
    deb_temp_dir.mkdir(parents=True, exist_ok=True)
    deb_files = download_all_debs(
        deb_temp_dir, package, arches_to_process, suite, original_cwd
    )

    # Prepare directories
    arch_data = {}
    for arch in arches_to_process:
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

        arch_data[arch] = {
            "archdir": archdir,
            "rootfs_dir": rootfs_dir,
            "deb_dir": deb_dir,
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
            "./",
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
        deb_dir = data["deb_dir"]
        deb_file_src = deb_files.get(arch)
        if deb_file_src is None:
            continue
        shutil.copy(str(deb_file_src), str(deb_dir / deb_file_src.name))
        data["deb_file"] = deb_dir / deb_file_src.name

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
        deb_dir = data["deb_dir"]
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

    # Clean up temp directory
    shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    args = parse_args()
    main(args)
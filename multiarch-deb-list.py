#!/usr/bin/env python3

import argparse
import subprocess as sub

# Mapping from arch to strategy to use
ARCHS = {
    "i386": 1,
    "amd64": 1, # 2 works too
    "arm64": 2,
    "armhf": 2,
    "ppc64le": 2,
    "s390x": 2,
    "riscv64": 2,
}

def bash_cmd_1(arch: str, package: str) -> str:
    return f"dpkg --add-architecture {arch} && apt update &>/dev/null && apt download {package}:{arch} &> /dev/null && dpkg-deb -c {package}*"

def bash_cmd_2(package: str) -> str:
    return f"apt update &>/dev/null && apt download {package} &> /dev/null && dpkg-deb -c {package}*"

def list_contents_1(*, arch: str, package: str, ubuntu: str) -> list[str]:
    cmd = ["docker", "run", "--quiet", "-t", "--rm", "--platform", "linux/amd64", f"ubuntu:{ubuntu}", "bash", "-c", bash_cmd_1(arch, package),]
    result = sub.run(cmd, capture_output=True, text=True)
    contents = result.stdout
    lines = contents.splitlines()
    return parse_dpkg_output(lines)

def list_contents_2(*, arch: str, package: str, ubuntu: str) -> list[str]:
    cmd = ["docker", "run", "--quiet", "-t", "--rm", "--platform", f"linux/{arch}", f"ubuntu:{ubuntu}", "bash", "-c", bash_cmd_2(package),]
    result = sub.run(cmd, capture_output=True, text=True)
    contents = result.stdout
    lines = contents.splitlines()
    return parse_dpkg_output(lines)

def parse_dpkg_output(lines: list[str]) -> list[str]:
    files: list[str] = []
    for line in lines:
        parts = line.split()
        if len(parts) >= 6:
            file_path = ' '.join(parts[5:]).rstrip().lstrip('./')
            file_path = '/' + file_path if not file_path.startswith('/') else file_path
            if not file_path.endswith('/'):
                files.append(file_path)
    return files

def main():
    parser = argparse.ArgumentParser(description="List contents of a Debian package across multiple architectures.")
    parser.add_argument("package", help="Name of the Debian package to inspect.")
    parser.add_argument("ubuntu" , help="Ubuntu version to use in Docker")
    args = parser.parse_args()

    package = args.package
    ubuntu = args.ubuntu
    for arch, strategy in ARCHS.items():
        print(f"Processing {package} on {arch} ({ubuntu}) with strategy {strategy}")
        if strategy == 1:
            contents = list_contents_1(arch=arch, package=package, ubuntu=ubuntu)
        else:
            contents = list_contents_2(arch=arch, package=package, ubuntu=ubuntu)
        with open(f"{package}-{arch}-{ubuntu}.txt", "w") as f:
            for line in contents:
                f.write(line + "\n")


if __name__ == "__main__":
    main()
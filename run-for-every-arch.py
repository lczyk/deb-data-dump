#!/usr/bin/env python3

import argparse
import subprocess as sub
import os
import platform

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

def get_host_arch():
    machine = platform.machine()
    mapping = {
        "x86_64": "amd64",
        "aarch64": "arm64",
        "armv7l": "armhf",
        "i686": "i386",
        "ppc64le": "ppc64le",
        "s390x": "s390x",
        "riscv64": "riscv64",
    }
    return mapping.get(machine, "amd64")

def arch_to_triplet(arch: str) -> str:
    triplet_mapping = {
        "i386": "i686-linux-gnu",
        "amd64": "x86_64-linux-gnu",
        "arm64": "aarch64-linux-gnu",
        "armhf": "arm-linux-gnueabihf",
        "ppc64le": "powerpc64le-linux-gnu",
        "s390x": "s390x-linux-gnu",
        "riscv64": "riscv64-linux-gnu",
    }
    return triplet_mapping.get(arch, arch)

def get_extra_env(arch: str) -> dict[str, str]:
    return {
        "ARCH": arch,
        "ARCH_TRIPLET": arch_to_triplet(arch),
        "DEBIAN_FRONTEND": "noninteractive",
        "TZ": "UTC",
    }


def bash_cmd_1(
    arch: str, package: str, extract_cmd: str, install_packages: str, verbose: bool
) -> str:
    update_cmd = "apt update" + ("" if verbose else " &>/dev/null")
    install_cmd = (
        f" && apt install -y {install_packages}" + ("" if verbose else " &>/dev/null")
        if install_packages
        else ""
    )
    download_cmd = f"apt download {package}:{arch}" + (
        "" if verbose else " &>/dev/null"
    )
    cmds = [
        f"dpkg --add-architecture {arch}",
        f"{update_cmd}{install_cmd}",
        download_cmd,
        f"dpkg-deb -x {package}*.deb /tmp/extract",
        "cd /tmp/extract",
        f"{extract_cmd}",
    ]
    return " && ".join(cmds)


def bash_cmd_2(
    package: str, extract_cmd: str, install_packages: str, verbose: bool
) -> str:
    update_cmd = "apt update" + ("" if verbose else " &>/dev/null")
    install_cmd = (
        f" && apt install -y {install_packages}" + ("" if verbose else " &>/dev/null")
        if install_packages
        else ""
    )
    download_cmd = f"apt download {package}" + ("" if verbose else " &>/dev/null")
    cmds = [
        f"{update_cmd}{install_cmd}",
        download_cmd,
        f"dpkg-deb -x {package}*.deb /tmp/extract",
        "cd /tmp/extract",
        f"{extract_cmd}",
    ]
    return " && ".join(cmds)


def run_for_arch_1(
    arch: str,
    package: str,
    ubuntu: str,
    extract_cmd: str,
    *,
    mount: str = "",
    install_packages: str = "",
    verbose: bool = False,
) -> tuple[str, str, int]:
    cmd = ["docker", "run", "--quiet", "-t", "--rm"]
    cmd.extend(["--platform", "linux/amd64"])
    for key, value in get_extra_env(arch).items():
        cmd.extend(["-e", f"{key}={value}"])
    if mount:
        cmd.extend(["-v", mount])
    cmd.extend(
        [
            f"ubuntu:{ubuntu}",
            "bash",
            "-c",
            bash_cmd_1(arch, package, extract_cmd, install_packages, verbose),
        ]
    )
    if verbose:
        result = sub.run(cmd, capture_output=False, text=True)
        output = ""
        err = ""
    else:
        result = sub.run(cmd, capture_output=True, text=True)
        output = result.stdout
        err = result.stderr
    return output, err, result.returncode

def run_for_arch_2(
    arch: str,
    package: str,
    ubuntu: str,
    extract_cmd: str,
    *,
    mount: str = "",
    install_packages: str = "",
    verbose: bool = False,
) -> tuple[str, str, int]:
    cmd = ["docker", "run", "--quiet", "-t", "--rm"]
    cmd.extend(["--platform", f"linux/{arch}"])
    for key, value in get_extra_env(arch).items():
        cmd.extend(["-e", f"{key}={value}"])
    if mount:
        cmd.extend(["-v", mount])
    cmd.extend(
        [
            f"ubuntu:{ubuntu}",
            "bash",
            "-c",
            bash_cmd_2(package, extract_cmd, install_packages, verbose),
        ]
    )
    if verbose:
        result = sub.run(cmd, capture_output=False, text=True)
        output = ""
        err = ""
    else:
        result = sub.run(cmd, capture_output=True, text=True)
        output = result.stdout
        err = result.stderr
    return output, err, result.returncode

def main():
    parser = argparse.ArgumentParser(description="Run a bash command on the content of the deb on multiple architectures.")
    parser.add_argument("package", help="Name of the Debian package to inspect.")
    parser.add_argument("ubuntu" , help="Ubuntu version to use in Docker")

    parser.add_argument(
        "-I",
        "--install",
        help="Comma-separated list of packages to install in the container before running the command",
        default="",
    )

    parser.add_argument(
        "-F",
        "--fail-fast",
        help="Stop processing further architectures if one fails",
        action="store_true",
    )

    parser.add_argument(
        "-V",
        "--verbose",
        help="Print output during all setup steps (apt update, install, download)",
        action="store_true",
    )

    # create a mutually exclusive group
    mutex_group = parser.add_mutually_exclusive_group(required=True)
    mutex_group.add_argument("-p", "--path", help="Path to the bash script called at the root path of the deb content")
    mutex_group.add_argument("-c", "--command", help="Bash command to run at the root path of the deb content")

    args = parser.parse_args()

    package = args.package
    ubuntu = args.ubuntu
    install_packages = args.install.replace(",", " ") if args.install else ""
    if args.command:
        extract_cmd = f"bash -c '{args.command}'"
        mount = ""
    elif args.path:
        script_path = os.path.realpath(args.path)
        extract_cmd = "bash /script.sh"
        mount = f"{script_path}:/script.sh"

    host_arch = get_host_arch()
    print(f"Host architecture detected as: {host_arch}")
    arches = list(ARCHS.keys())
    if host_arch in arches:
        arches.remove(host_arch)
        arches.insert(0, host_arch)

    print(arches)
    for arch in arches:
        strategy = ARCHS[arch]
        print(f"Processing {package} on {arch} ({ubuntu}) with strategy {strategy}")
        if strategy == 1:
            output, err, code = run_for_arch_1(
                arch,
                package,
                ubuntu,
                extract_cmd,
                mount=mount,
                install_packages=install_packages,
                verbose=args.verbose,
            )
        else:
            output, err, code = run_for_arch_2(
                arch,
                package,
                ubuntu,
                extract_cmd,
                mount=mount,
                install_packages=install_packages,
                verbose=args.verbose,
            )
        print(f"Output for {arch}:")
        if not args.verbose:
            print(output)
        if code != 0:
            if not args.verbose:
                print(f"Error for {arch}:")
                print(err)
            if args.fail_fast:
                print("Failing fast due to error.")
                break


if __name__ == "__main__":
    main()
#!/usr/bin/env python3

import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, List, Union

from deploykit import DeployGroup, DeployHost
from invoke import task

ROOT = Path(__file__).parent.resolve()
os.chdir(ROOT)


# Deploy to all hosts in parallel
def deploy_nixos(hosts: List[DeployHost]) -> None:
    g = DeployGroup(hosts)

    def deploy(h: DeployHost) -> None:
        if "darwin" in h.host:
            # don't use sudo for darwin-rebuild
            command = "darwin-rebuild"
            target = f"{h.user}@{h.host}"
        else:
            command = "sudo nixos-rebuild"
            target = f"{h.host}"

        res = h.run_local(
            ["nix", "flake", "archive", "--to", f"ssh://{target}", "--json"],
            stdout=subprocess.PIPE,
        )
        data = json.loads(res.stdout)
        path = data["path"]

        hostname = h.host.replace(".nix-community.org", "")
        h.run(
            f"{command} switch --option accept-flake-config true --flake {path}#{hostname}"
        )

    g.run_function(deploy)


@task
def sotp(c: Any, acct: str) -> None:
    """
    Get TOTP token from sops
    """
    c.run(f"nix develop .#sotp -c sotp {acct}")


@task
def update_agenix_files(c: Any) -> None:
    """
    Update all agenix secrets
    """
    os.chdir("secrets")
    c.run("agenix --rekey", pty=True)


@task
def update_sops_files(c: Any) -> None:
    """
    Update all sops yaml and json files according to .sops.yaml rules
    """
    c.run(
        """
find . \
        -type f \
        \( -iname '*.enc.json' -o -iname 'secrets.yaml' \) \
        -exec sops updatekeys --yes {} \;
"""
    )


@task
def print_keys(c: Any, flake_attr: str) -> None:
    """
    Decrypt host private key, print ssh and age public keys. Use inv print-keys --flake-attr build01
    """
    with TemporaryDirectory() as tmpdir:
        decrypt_host_key(flake_attr, tmpdir)
        key = f"{tmpdir}/etc/ssh/ssh_host_ed25519_key"
        pubkey = subprocess.run(
            ["ssh-keygen", "-y", "-f", f"{key}"],
            stdout=subprocess.PIPE,
            text=True,
            check=True,
        )
        print("###### Public keys ######")
        print(pubkey.stdout)
        print("###### Age keys ######")
        subprocess.run(
            ["ssh-to-age"],
            input=pubkey.stdout,
            check=True,
            text=True,
        )


@task
def mkdocs(c: Any) -> None:
    """
    Serve docs (mkdoc serve)
    """
    c.run("nix develop .#mkdocs -c mkdocs serve")


@task
def docs_linkcheck(c: Any) -> None:
    """
    Run docs online linkchecker
    """
    c.run("nix run .#docs-linkcheck.online")


def get_hosts(hosts: str) -> List[DeployHost]:
    if hosts == "":
        res = subprocess.run(
            ["nix", "flake", "show", "--json", "--all-systems"],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        )
        data = json.loads(res.stdout)
        systems = data["nixosConfigurations"]
        return [DeployHost(f"{n}.nix-community.org") for n in systems]

    if "darwin" in hosts:
        if hosts == "darwin01":
            return [DeployHost("darwin01.nix-community.org", user="customer")]
        else:
            return [
                DeployHost(f"{h}.nix-community.org", user="hetzner")
                for h in hosts.split(",")
            ]

    return [DeployHost(f"{h}.nix-community.org") for h in hosts.split(",")]


@task
def deploy(c: Any, hosts: str = "") -> None:
    """
    Deploy to all servers. Use inv deploy --hosts build01 to deploy to a single server
    """
    deploy_nixos(get_hosts(hosts))


def decrypt_host_key(flake_attr: str, tmpdir: str) -> None:
    def opener(path: str, flags: int) -> Union[str, int]:
        return os.open(path, flags, 0o400)

    t = Path(tmpdir)
    t.mkdir(parents=True, exist_ok=True)
    t.chmod(0o755)
    host_key = t / "etc/ssh/ssh_host_ed25519_key"
    host_key.parent.mkdir(parents=True, exist_ok=True)
    with open(host_key, "w", opener=opener) as fh:
        subprocess.run(
            [
                "sops",
                "--extract",
                '["ssh_host_ed25519_key"]',
                "--decrypt",
                f"{ROOT}/hosts/{flake_attr}/secrets.yaml",
            ],
            check=True,
            stdout=fh,
        )


@task
def install(c: Any, flake_attr: str, hostname: str) -> None:
    """
    Decrypt host private key, install with nixos-anywhere. Use inv install --flake-attr build01 --hostname build01.nix-community.org
    """
    ask = input(f"Install {hostname} with {flake_attr}? [y/N] ")
    if ask != "y":
        return
    with TemporaryDirectory() as tmpdir:
        decrypt_host_key(flake_attr, tmpdir)
        flags = "--debug --no-reboot --option accept-flake-config true"
        c.run(
            f"nix run --inputs-from . nixpkgs#nixos-anywhere -- {hostname} --extra-files {tmpdir} --flake .#{flake_attr} {flags}",
            echo=True,
        )


@task
def build_local(c: Any, hosts: str = "") -> None:
    """
    Build all servers. Use inv build-local --hosts build01 to build a single server
    """
    g = DeployGroup(get_hosts(hosts))

    def build_local(h: DeployHost) -> None:
        hostname = h.host.replace(".nix-community.org", "")
        h.run_local(
            [
                "nixos-rebuild",
                "build",
                "--option",
                "accept-flake-config",
                "true",
                "--flake",
                f".#{hostname}",
            ]
        )

    g.run_function(build_local)


def wait_for_port(host: str, port: int, shutdown: bool = False) -> None:
    import socket
    import time

    while True:
        try:
            with socket.create_connection((host, port), timeout=1):
                if shutdown:
                    time.sleep(1)
                    sys.stdout.write(".")
                    sys.stdout.flush()
                else:
                    break
        except OSError:
            if shutdown:
                break
            else:
                time.sleep(0.01)
                sys.stdout.write(".")
                sys.stdout.flush()


@task
def reboot(c: Any, hosts: str = "") -> None:
    """
    Reboot hosts. example usage: inv reboot --hosts build01,build02
    """
    for h in get_hosts(hosts):
        h.run("sudo reboot &")

        print(f"Wait for {h.host} to shutdown", end="")
        sys.stdout.flush()
        port = h.port or 22
        wait_for_port(h.host, port, shutdown=True)
        print("")

        print(f"Wait for {h.host} to start", end="")
        sys.stdout.flush()
        wait_for_port(h.host, port)
        print("")


@task
def cleanup_gcroots(c: Any, hosts: str = "") -> None:
    g = DeployGroup(get_hosts(hosts))
    g.run("sudo find /nix/var/nix/gcroots/auto -type s -delete")
    g.run("sudo systemctl restart nix-gc")

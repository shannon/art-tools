import os
import re
import subprocess

import click
import koji
import requests
import yaml
from artcommonlib.arch_util import brew_arch_for_go_arch
from artcommonlib.constants import BREW_DOWNLOAD_URL, BREW_HUB
from artcommonlib.util import isolate_major_minor_in_group
from errata_tool import Erratum

from pyartcd import jenkins, util
from pyartcd.cli import cli, click_coroutine, pass_runtime
from pyartcd.runtime import Runtime


class OperatorSDKPipeline:
    def __init__(
        self, runtime: Runtime, group: str, assembly: str, nvr: str, prerelease: bool, updatelatest: bool, arches: str
    ) -> None:
        self.runtime = runtime
        self._logger = runtime.logger
        self.assembly = assembly
        self.prerelease = prerelease
        self.nvr = nvr
        self.updatelatest = updatelatest
        self.sdk = "operator-sdk"
        self.group = group
        self.extra_ad_id = ""
        self.parent_jira_key = ""
        self._jira_client = runtime.new_jira_client()
        self.arches = arches

    async def run(self):
        if self.assembly:
            group_config = await util.load_group_config(self.group, self.assembly, env=os.environ.copy())
            self.extra_ad_id = group_config.get("advisories", {}).get("extras", 0)
            self.parent_jira_key = group_config.get("release_jira")
            advisory = Erratum(errata_id=self.extra_ad_id)
            self._logger.info("Check advisory status ...")
            if advisory.errata_state in ["QE", "NEW_FILES"]:
                raise ValueError("Advisory status not in REL_PREP yet ...")
            if advisory.errata_state == "SHIPPED_LIVE":
                self._logger.info("Advisory status already in SHIPPED_LIVE, update subtask 9 ...")
                self._jira_client.complete_subtask(
                    self.parent_jira_key,
                    "pushes advisory content to production CDN",
                    "Advisory status already in SHIPPED_LIVE",
                )
            self._logger.info("Advisory status already in post REL_PREP, update subtask 7 ...")
            self._jira_client.complete_subtask(
                self.parent_jira_key, "moves advisories to REL_PREP", "Advisory status already in REL_PREP"
            )

            sdk_build = [
                b
                for b in sum(list(map(list, advisory.errata_builds.values())), [])
                if b.startswith('openshift-enterprise-operator-sdk-container')
            ]
            if not sdk_build:
                self._logger.info("No SDK build to ship, update subtask 8 then close ...")
                self._jira_client.complete_subtask(
                    self.parent_jira_key,
                    "operator-sdk",
                    f"No SDK build to ship, operator_sdk_sync job: {jenkins.get_build_url()}",
                )
                return
            build = koji.ClientSession(BREW_HUB).getBuild(sdk_build[0])
        elif self.nvr:
            build = koji.ClientSession(BREW_HUB).getBuild(self.nvr)
        else:
            raise ValueError("no assembly or nvr provided")

        sdkVersion = self._get_sdkversion(build)
        self._logger.info(sdkVersion)
        for arch in self.arches.split(','):
            self._extract_binaries(arch, sdkVersion, build['extra']['image']['index']['pull'][0])
        if self.assembly:
            self._jira_client.complete_subtask(
                self.parent_jira_key, "operator-sdk", f"operator_sdk_sync job: {jenkins.get_build_url()}"
            )

    def _get_sdkversion(self, build):
        build_log_res = requests.get(
            f"{BREW_DOWNLOAD_URL}/packages/{build['name']}/{build['version']}/{build['release']}/data/logs/x86_64.log"
        )
        match = re.search(r"v\d+\.\d+\.\d+-ocp", build_log_res.text)
        if match:
            return match.group()
        else:
            raise ValueError("Can't find operator SDK version in build log")

    def _extract_binaries(self, arch, sdkVersion, build):
        output = subprocess.getoutput(f"oc image info --filter-by-os {arch} -o json {build} | jq .digest")

        registry_repo = re.findall(r"^[^@]+", build)[0]
        shasum = re.findall(r"sha256:\w*", output)[0]
        pullspec = f'{registry_repo}@{shasum}'

        rarch = brew_arch_for_go_arch(arch)
        tarballFilename = f"{self.sdk}-{sdkVersion}-linux-{rarch}.tar.gz"

        cmd = (
            f"rm -rf ./{rarch} && mkdir ./{rarch}"
            + f" && oc image extract {pullspec} --path /usr/local/bin/{self.sdk}:./{rarch}/ --confirm"
            + f" && chmod +x ./{rarch}/{self.sdk} && tar -c -z -v --file ./{rarch}/{tarballFilename} ./{rarch}/{self.sdk}"
            + f" && ln -s {tarballFilename} ./{rarch}/{self.sdk}-linux-{rarch}.tar.gz && rm -f ./{rarch}/{self.sdk}"
        )
        self.exec_cmd(cmd)
        if arch == 'amd64' or arch == 'arm64':
            tarballFilename = f"{self.sdk}-{sdkVersion}-darwin-{rarch}.tar.gz"
            major, minor = isolate_major_minor_in_group(self.group)
            share_path = "mac_arm64" if arch == 'arm64' and (major, minor) >= (4, 12) else "mac"
            cmd = (
                f"oc image extract {pullspec} --path /usr/share/{self.sdk}/{share_path}/{self.sdk}:./{rarch}/ --confirm"
                + f" && chmod +x ./{rarch}/{self.sdk} && tar -c -z -v --file ./{rarch}/{tarballFilename} ./{rarch}/{self.sdk}"
                + f" && ln -s {tarballFilename} ./{rarch}/{self.sdk}-darwin-{rarch}.tar.gz && rm -f ./{rarch}/{self.sdk}"
            )
            self.exec_cmd(cmd)
        self._sync_mirror(rarch)

    def _sync_mirror(self, arch):
        extra_args = "--exclude '*' --include '*.tar.gz'"
        if self.prerelease:
            s3_path = f"/pub/openshift-v4/{arch}/clients/operator-sdk/pre-release/"
        else:
            s3_path = f"/pub/openshift-v4/{arch}/clients/operator-sdk/{self.assembly}/"
        cmd = f"aws s3 sync --no-progress --exact-timestamps {extra_args} --delete ./{arch}/ s3://art-srv-enterprise{s3_path}"
        self.exec_cmd(cmd)

        # Sync temporarily to Cloudflare as well
        self.exec_cmd(cmd + f" --profile cloudflare --endpoint-url {os.environ['CLOUDFLARE_ENDPOINT']}")
        if self.updatelatest:
            s3_path_latest = f"/pub/openshift-v4/{arch}/clients/operator-sdk/latest/"
            cmd = f"aws s3 sync --no-progress --exact-timestamps {extra_args} --delete ./{arch}/ s3://art-srv-enterprise{s3_path_latest}"
            self.exec_cmd(cmd)

            # Sync temporarily to Cloudflare as well
            self.exec_cmd(cmd + f" --profile cloudflare --endpoint-url {os.environ['CLOUDFLARE_ENDPOINT']}")

    def exec_cmd(self, cmd):
        self._logger.info(f"running command: {cmd}")
        subprocess.run(cmd, shell=True, check=True)


@cli.command("operator-sdk-sync")
@click.option(
    "-g",
    "--group",
    metavar='NAME',
    required=True,
    help="The group of components on which to operate. e.g. openshift-4.9",
)
@click.option("--assembly", metavar="ASSEMBLY_NAME", required=True, help="The name of an assembly. e.g. 4.9.1")
@click.option("--nvr", metavar="BUILD_NVR", required=False, help="Pin specific Build NVR")
@click.option(
    "--prerelease", metavar="PRE_RELEASE", is_flag=True, required=False, help="Use pre-release as directory name."
)
@click.option(
    "--updatelatest",
    metavar="UPDATE_LATEST_SYMLINK",
    is_flag=True,
    required=False,
    help="Update latest symlink on mirror",
)
@click.option("--arches", metavar="ARCHES", required=False, help="Arches in the build")
@pass_runtime
@click_coroutine
async def operator_sdk_sync(
    runtime: Runtime, group: str, assembly: str, nvr: str, prerelease: bool, updatelatest: bool, arches: str
):
    pipeline = OperatorSDKPipeline(runtime, group, assembly, nvr, prerelease, updatelatest, arches)
    await pipeline.run()

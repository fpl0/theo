"""Repository-bound GitHub App publication with exact remote receipts.

Private keys and short-lived tokens remain in the controller. Candidate commands
never inherit them. Ambiguous mutations stop until the exact remote state resolves.
"""

import asyncio
import base64
import json
import time
from pathlib import Path
from typing import cast
from urllib.parse import quote

import httpx

from theo.backends.process import stop_process
from theo.domain import Conflict, Denied, Json
from theo.maintenance.configuration import ControllerConfig, read_protected
from theo.maintenance.contracts import CandidateIdentity
from theo.maintenance.policy import MaintenancePolicy
from theo.maintenance.source import git, git_environment


class NoEffect(Denied):
    """The transport established that this mutation was not accepted."""


class Uncertain(Conflict):
    """A remote effect may have happened; only reconciliation can establish its outcome."""


class BaseAdvanced(NoEffect):
    """The merge was not sent because its reviewed base is no longer current."""


class CheckFailed(Denied):
    """An expected CI check completed without success for this exact candidate."""


class GitHub:
    def __init__(self, config: ControllerConfig, policy: MaintenancePolicy):
        self.config, self.policy = config, policy
        self.token = ""
        self.expires = 0.0
        self.client = httpx.AsyncClient(
            base_url="https://api.github.com", timeout=20, follow_redirects=False, trust_env=False
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def authenticate(self) -> None:
        if time.time() < self.expires:
            return
        if self.config.github_auth == "gh_cli":
            assert self.config.github_cli and self.config.github_cli_config
            process = await asyncio.create_subprocess_exec(
                str(self.config.github_cli),
                "auth",
                "token",
                "--hostname",
                "github.com",
                env={**git_environment(), "GH_CONFIG_DIR": str(self.config.github_cli_config)},
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                token, _ = await asyncio.wait_for(process.communicate(), 15)
                if process.returncode or not token.strip() or len(token) > 65536:
                    raise Denied("Existing GitHub CLI authentication is unavailable")
                self.token = token.decode().strip()
                self.expires = time.time() + 300
                return
            finally:
                await stop_process(process)
        assert self.config.github_key_file
        read_protected(self.config.github_key_file)

        def part(value: Json) -> bytes:
            return base64.urlsafe_b64encode(
                json.dumps(value, separators=(",", ":")).encode()
            ).rstrip(b"=")

        unsigned = (
            part({"alg": "RS256", "typ": "JWT"})
            + b"."
            + part(
                {
                    "iat": int(time.time()) - 60,
                    "exp": int(time.time()) + 480,
                    "iss": self.config.github_app_id,
                }
            )
        )
        process = await asyncio.create_subprocess_exec(
            "/usr/bin/openssl",
            "dgst",
            "-sha256",
            "-sign",
            str(self.config.github_key_file),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=git_environment(),
            start_new_session=True,
        )
        try:
            signed, _ = await asyncio.wait_for(process.communicate(unsigned), 10)
            if process.returncode:
                raise Denied("GitHub App key could not sign installation authentication")
        finally:
            await stop_process(process)
        jwt = (unsigned + b"." + base64.urlsafe_b64encode(signed).rstrip(b"=")).decode()
        response = await self.client.post(
            f"/app/installations/{self.config.github_installation_id}/access_tokens",
            headers={"Authorization": "Bearer " + jwt, "Accept": "application/vnd.github+json"},
            json={
                "repository_ids": [self.policy.repository_id],
                "permissions": {
                    "contents": "write",
                    "pull_requests": "write",
                    "checks": "read",
                    "actions": "read",
                },
            },
        )
        if response.status_code != 201:
            raise Denied("GitHub App installation authentication denied")
        self.token = str(response.json()["token"])
        self.expires = time.time() + 3000

    async def api(self, method: str, suffix: str, body: Json | None = None) -> Json:
        try:
            await self.authenticate()
        except Denied:
            if method != "GET":
                raise NoEffect("GitHub authentication failed before the mutation") from None
            raise
        try:
            response = await self.client.request(
                method,
                "/repos/" + self.policy.repository + suffix,
                headers={
                    "Authorization": "Bearer " + self.token,
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2026-03-10",
                },
                json=body,
            )
        except httpx.HTTPError:
            if method != "GET":
                raise Uncertain("GitHub mutation acknowledgement is unavailable") from None
            raise Conflict("GitHub is unavailable") from None
        if response.status_code == 404 and method == "GET":
            return {"missing": True}
        if response.status_code >= 500 and method != "GET":
            raise Uncertain("GitHub mutation outcome is unknown")
        if 400 <= response.status_code < 500 and method != "GET":
            raise NoEffect("GitHub refused the mutation: HTTP " + str(response.status_code))
        if not 200 <= response.status_code < 300:
            raise Denied("GitHub rejected operation: HTTP " + str(response.status_code))
        payload = response.json()
        return cast(Json, payload) if isinstance(payload, dict) else {"items": payload}

    async def identity(self) -> None:
        repository = await self.api("GET", "")
        if (
            repository.get("id") != self.policy.repository_id
            or repository.get("private") is not False
            or str(repository.get("full_name", "")).lower() != self.policy.repository.lower()
        ):
            raise Denied("Repository identity or public-only CI policy no longer matches")

    async def ref(self, branch: str) -> str | None:
        response = await self.api("GET", "/git/ref/heads/" + quote(branch, safe=""))
        return None if response.get("missing") else str(response["object"]["sha"])

    async def push(
        self, path: Path, candidate: CandidateIdentity, branch: str, *, previous: str | None = None
    ) -> Json:
        await self.identity()
        existing = await self.ref(branch)
        if existing == candidate.commit:
            return {"branch": branch, "sha": existing}
        if existing != previous:
            raise Conflict("Publication branch is owned by a different commit")
        await self.authenticate()
        helper = self.config.root / "git-askpass"
        # This fixed helper runs only within the credential-owning publisher environment.
        helper.write_text(
            '#!/bin/sh\ncase "$1" in *Username*) printf "%s\\n" x-access-token;; *) printf "%s\\n" "$THEO_GITHUB_TOKEN";; esac\n'
        )
        helper.chmod(0o700)
        environment = {
            **git_environment(),
            "GIT_ASKPASS": str(helper),
            "THEO_GITHUB_TOKEN": self.token,
        }
        try:
            await git(
                path,
                "push",
                "--porcelain",
                "--force-with-lease=refs/heads/" + branch + ":" + (previous or ""),
                "https://github.com/" + self.policy.repository + ".git",
                candidate.commit + ":refs/heads/" + branch,
                env=environment,
            )
        except Conflict, TimeoutError:
            if await self.ref(branch) != candidate.commit:
                raise Uncertain(
                    "Push may have been accepted; exact branch receipt is missing"
                ) from None
        if await self.ref(branch) != candidate.commit:
            raise Uncertain("Published ref has no matching remote receipt")
        return {"branch": branch, "sha": candidate.commit}

    async def pull_request(
        self, branch: str, candidate: CandidateIdentity, *, create: bool
    ) -> Json | None:
        owner = self.policy.repository.split("/")[0]
        response = await self.api(
            "GET", "/pulls?state=all&head=" + quote(owner + ":" + branch, safe="") + "&per_page=100"
        )
        matches = response["items"]
        if len(matches) > 1:
            raise Uncertain("Multiple pull requests match the publication identity")
        if matches:
            pr = matches[0]
        elif create:
            pr = await self.api(
                "POST",
                "/pulls",
                {
                    "title": "Theo maintenance " + candidate.change_id,
                    "head": branch,
                    "base": self.policy.base_branch,
                    "body": "Automated maintenance. Every candidate revision requires fresh checks "
                    "and an independent review bound to its exact source commit.",
                },
            )
        else:
            return None
        if (
            pr["head"]["sha"] != candidate.commit
            or pr["base"]["ref"] != self.policy.base_branch
            or pr["head"]["repo"]["id"] != self.policy.repository_id
        ):
            raise Conflict("Pull request no longer matches accepted source")
        return {
            "number": pr["number"],
            "url": pr["html_url"],
            "sha": candidate.commit,
            "branch": branch,
        }

    async def checks(self, sha: str) -> Json | None:
        checks = await self.api("GET", "/commits/" + sha + "/check-runs?per_page=100&filter=latest")
        if checks.get("total_count", 0) > 100:
            raise Denied("Check inventory exceeds bounded verification limit")
        runs = await self.api("GET", "/actions/runs?head_sha=" + sha + "&per_page=100")
        suites = {
            run["check_suite_id"]
            for run in runs.get("workflow_runs", [])
            if run.get("head_sha") == sha
            and run.get("path") == self.config.required_workflow
            and run.get("conclusion") == "success"
            and run.get("event") in ("push", "pull_request")
        }
        receipts: list[Json] = []
        for name, app_id in self.config.required_checks.items():
            matches = [
                run
                for run in checks.get("check_runs", [])
                if run["name"] == name and run["app"]["id"] == app_id
            ]
            if not matches or any(run.get("status") != "completed" for run in matches):
                return None
            if any(
                run.get("head_sha") == sha
                and run.get("conclusion") in {"failure", "timed_out", "action_required"}
                for run in matches
            ):
                raise CheckFailed("Required check failed: " + name)
            if any(
                run.get("head_sha") != sha
                or run.get("conclusion") != "success"
                or run.get("check_suite", {}).get("id") not in suites
                for run in matches
            ):
                raise Denied(
                    "Required check failed, skipped, or has unexpected workflow provenance"
                )
            receipts.extend(
                {"id": run["id"], "name": name, "sha": sha, "conclusion": "success"}
                for run in matches
            )
        return {"sha": sha, "checks": receipts}

    async def merge(self, pr: Json, candidate: CandidateIdentity, *, send: bool) -> Json | None:
        observed = await self.api("GET", "/pulls/" + str(pr["number"]))
        if observed.get("merged"):
            if observed["head"]["sha"] != candidate.commit:
                raise Conflict("Merged pull request has a different head")
            return {"sha": observed["merge_commit_sha"], "number": pr["number"], "url": pr["url"]}
        if not send:
            return None
        if await self.ref(self.policy.base_branch) != candidate.base_commit:
            raise BaseAdvanced(
                "Base advanced; a new integration and independent review are required"
            )
        # This read needs metadata permission only. The maintenance App must not
        # need repository administration merely to verify its merge safeguards.
        rules = await self.api(
            "GET", "/rules/branches/" + quote(self.policy.base_branch, safe="") + "?per_page=100"
        )
        protected = any(
            rule.get("type") == "required_status_checks"
            and rule.get("parameters", {}).get("strict_required_status_checks_policy") is True
            and all(
                any(
                    check.get("context") == name and check.get("integration_id") == app_id
                    for check in rule.get("parameters", {}).get("required_status_checks", [])
                )
                for name, app_id in self.config.required_checks.items()
            )
            for rule in rules.get("items", [])
        )
        if not protected:
            raise Denied(
                "Automatic merge requires active strict status-check rules for the bound branch"
            )
        if not await self.checks(candidate.commit):
            raise Conflict("Required checks are pending")
        result = await self.api(
            "PUT",
            "/pulls/" + str(pr["number"]) + "/merge",
            {"sha": candidate.commit, "merge_method": "merge"},
        )
        if not result.get("merged"):
            raise Denied("Repository rules did not permit merging")
        return {"sha": result["sha"], "number": pr["number"], "url": pr["url"]}

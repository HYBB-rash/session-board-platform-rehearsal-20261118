import argparse
import base64
from datetime import datetime, timezone
import json
from pathlib import Path
import os
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from scripts.main_release_evidence import CandidatePending, implementation, select, verify_merge
from scripts.promote_candidate import git, github
from scripts.release_plugin import GitHub, platform_tag
from scripts.package_plugin import TARGETS
from scripts.integration_plan import ancestor


def dispatch(repository, source):
    result = {"status": "pending", "requested_source": source,
              "requested_at": datetime.now(timezone.utc).isoformat()}
    for attempt in range(1, 4):
        try:
            github("workflow", "run", "plugin-release.yml", "--repo", repository, "--ref", "main",
                   "-f", f"source={source}", timeout=10)
            result.update(trigger="workflow_dispatch", attempts=attempt)
            return result
        except Exception as error:
            result.update(dispatch_error=str(error), attempts=attempt)
    result["retry"] = "./scripts/release main dispatch --repository " + repository
    result["automatic_retry"] = "plugin-release.yml scheduled reconciliation"
    try:
        from scripts.main_release_feedback import failure
        GitHub(repository).api(
            f"statuses/{source}", "POST", state="failure", context="Plugin release",
            description="代码已合并，发布触发失败；自动调和将重试",
            target_url=f"https://github.com/{repository}/actions/workflows/plugin-release.yml")
        result["feedback"] = failure(repository, source, "dispatch",
                                     f"https://github.com/{repository}/actions/workflows/plugin-release.yml",
                                     phase="dispatch")
    except Exception as error:
        result["feedback_error"] = str(error)
    return result


def channels(forge):
    result = {}
    for target in sorted(TARGETS):
        saved = forge.api(f"contents/RELEASE.json?ref=candidate%2F{target}")
        if saved is None:
            result[target] = None
        else:
            result[target] = json.loads(base64.b64decode(saved["content"]))
    return result


def already_published(repository, selected, target=None):
    forge = GitHub(repository)
    published = channels(forge)
    if target:
        published = {target: published[target]}
    if not all(item and item["source_commit"] == selected["source"]
               and item["version"] == selected["version"] and item["target"] == target
               for target, item in published.items()):
        return False
    tag = platform_tag(selected["tag"], target) if target else selected["tag"]
    saved = forge.release(tag)
    if not saved and target:
        tag = selected["tag"]
        saved = forge.release(tag)
    if not saved or saved["draft"] or not saved.get("immutable"):
        return False
    commit = forge.api(f"commits/{tag}")
    return commit["sha"] == selected["source"]


def status(repository, source):
    git("fetch", "--quiet", "origin", "main")
    merged = ancestor(source, git("rev-parse", "FETCH_HEAD"))
    forge = GitHub(repository)
    published = channels(forge)
    canonical = implementation(source)
    statuses = forge.api(f"commits/{canonical}/statuses?per_page=100")
    scoped = {target: next((entry for entry in statuses if entry["context"] == release_context(target)), None)
              for target in TARGETS}
    if any(scoped.values()) or len({item["source_commit"] for item in published.values() if item}) > 1:
        return platform_status(source, canonical, merged, published, scoped)
    latest = next((entry for entry in statuses if entry["context"] == "Plugin release"), None)
    origins = {item["source_commit"] for item in published.values() if item}
    coherent = len(origins) == 1 and all(published.values())
    covered = coherent and ancestor(canonical, next(iter(origins)))
    complete = covered and canonical in origins
    state = ("published" if complete else "covered" if covered else
             {"success": "inconsistent", "failure": "failed", "pending": "pending"}.get(
                 latest["state"] if latest else "", "pending"))
    return {"source": source, "merge_status": "merged" if merged else "not_merged", "published": published,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "release_status": state if merged else "not_requested",
            "covered_by": next(iter(origins)) if covered else None,
            "latest_attempt": latest}


def release_context(target):
    return f"Plugin release ({target})"


def platform_status(source, canonical, merged, published, scoped):
    results = {}
    for target, current in published.items():
        attempt = scoped[target]
        if current and current["source_commit"] == canonical:
            state = "published"
        elif current and ancestor(canonical, current["source_commit"]):
            state = "covered"
        else:
            state = {"failure": "failed", "success": "inconsistent"}.get(
                attempt["state"] if attempt else None, "pending")
        results[target] = {"release_status": state, "latest_attempt": attempt, "published": current}
    return {"source": source, "merge_status": "merged" if merged else "not_merged",
            "published": published, "platforms": results,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "release_status": results["x86_64-unknown-linux-gnu"]["release_status"] if merged else "not_requested"}


def record_platform(repository, source, state, run, target, exercise=False, phase="unknown"):
    forge = GitHub(repository)
    current = channels(forge)[target]
    observed = current["source_commit"] if current else None
    covered = observed not in (None, source) and ancestor(source, observed)
    if state == "success" and not (observed == source or covered):
        raise ValueError("The platform channel did not confirm publication or a newer source")
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1")
    url = f"https://github.com/{repository}/actions/runs/{run}/attempts/{attempt}"
    jobs = forge.api(f"actions/runs/{run}/attempts/{attempt}/jobs?per_page=100")
    job = next((item for item in jobs["jobs"] if item["name"] == f"Release ({target})"), None)
    result = {"target": target, "source": source, "state": state, "run": run,
              "attempt": attempt, "job": job["id"] if job else None,
              "failed_steps": [step["name"] for step in job.get("steps", [])
                               if step["conclusion"] == "failure"] if job else [],
              "phase": phase, "url": job["html_url"] if job else url, "published": current}
    if exercise:
        return result
    forge.api(f"statuses/{source}", "POST", state=state, context=release_context(target),
              description=f"{phase}: {state}", target_url=result["url"])
    if target == "x86_64-unknown-linux-gnu":
        from scripts.main_release_feedback import failure, recovered
        if state == "failure" and observed != source and not covered:
            result["feedback"] = failure(repository, source, run, url, phase=phase, target=target)
        elif state == "success":
            result["resolved_incidents"] = recovered(repository, source, run)
    return result


def record(repository, source, state, run, exercise=False, phase="unknown"):
    if state == "success":
        current = channels(GitHub(repository))
        if not all(item and item["source_commit"] == source for item in current.values()):
            raise ValueError("Both delivery channels must confirm the exact published source")
    if exercise:
        if state == "failure":
            from scripts.main_release_feedback import failure
            return failure(repository, source, run,
                           f"https://github.com/{repository}/actions/runs/{run}", exercise=True, phase=phase)
        return {"source": source, "status": state, "exercise": True, "run": run}
    forge = GitHub(repository)
    url = f"https://github.com/{repository}/actions/runs/{run}"
    descriptions = {"pending": "代码已合并，插件发布中", "success": "双平台不可变插件已发布",
                    "failure": "代码已合并，尚未发布；保留上一可用版本"}
    forge.api(f"statuses/{source}", "POST", state=state, context="Plugin release",
              description=descriptions[state], target_url=url)
    if state == "failure":
        from scripts.main_release_feedback import failure
        return failure(repository, source, run, url, phase=phase)
    result = {"source": source, "status": state, "run": run,
              "observed_at": datetime.now(timezone.utc).isoformat()}
    if state == "success":
        from scripts.main_release_feedback import recovered
        result["published"] = current
        result["resolved_incidents"] = recovered(repository, source, run)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("select", "verify", "dispatch", "status", "record"))
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--source")
    parser.add_argument("--target", choices=sorted(TARGETS))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--state", choices=("pending", "success", "failure"))
    parser.add_argument("--run", default=os.environ.get("GITHUB_RUN_ID"))
    parser.add_argument("--exercise", action="store_true")
    parser.add_argument("--allow-unmerged", action="store_true")
    parser.add_argument("--defer-pending", action="store_true")
    parser.add_argument("--phase", default="unknown")
    arguments = parser.parse_args()
    repository = arguments.repository or json.loads(
        github("repo", "view", "--json", "nameWithOwner"))["nameWithOwner"]
    source = arguments.source or git("rev-parse", "HEAD")
    if arguments.action == "select":
        result = select(repository, arguments.source, allow_unmerged=arguments.allow_unmerged)
        if not result.get("unmerged"):
            result["published"] = str(not arguments.exercise and already_published(
                repository, result, arguments.target)).lower()
    elif arguments.action == "verify":
        if arguments.output is None:
            parser.error("verify needs an external evidence directory")
        try:
            result = verify_merge(repository, arguments.output, arguments.target)
        except CandidatePending:
            if not arguments.defer_pending:
                raise
            result = {"deferred": "true"}
    elif arguments.action == "dispatch":
        result = dispatch(repository, source)
    elif arguments.action == "status":
        result = status(repository, source)
    else:
        if not arguments.state or not arguments.run:
            parser.error("record needs --state and --run")
        if arguments.target:
            result = record_platform(repository, source, arguments.state, arguments.run,
                                     arguments.target, arguments.exercise, arguments.phase)
        else:
            result = record(repository, source, arguments.state, arguments.run, arguments.exercise,
                            arguments.phase)
    if arguments.github_output:
        with arguments.github_output.open("a") as stream:
            for name in ("main", "source", "version", "tag", "published", "deferred"):
                if name in result:
                    stream.write(f"{name}={result[name]}\n")
    if arguments.output and arguments.action != "verify":
        arguments.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()

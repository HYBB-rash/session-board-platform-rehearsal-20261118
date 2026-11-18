from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import tomllib

from scripts.promote_candidate import ROOT, JOBS, LINUX_JOBS, PLATFORMS, TARGETS, artifact_name
from scripts.promote_candidate import git, github, identity, validate_artifact, validate_run
from scripts.integration_plan import ancestor, changed, context_path, modes


def implementation(main):
    source = main
    while True:
        parents = git("rev-list", "--parents", "-n", "1", source).split()[1:]
        if not parents:
            return source
        parent = parents[0]
        paths = changed(parent, source)
        if not paths or any(not context_path(path) for path in paths):
            return source
        before, after = modes(parent, paths), modes(source, paths)
        if any(before.get(path, after.get(path)) != "100644"
               or after.get(path, before.get(path)) != "100644" for path in paths):
            return source
        source = parent


def select(repository, requested=None, *, allow_unmerged=False):
    if git("status", "--porcelain", "--untracked-files=all"):
        raise ValueError("Release selection requires a clean checkout")
    observed = json.loads(github("api", f"repos/{repository}/git/ref/heads/main"))["object"]["sha"]
    subprocess.run(["git", "fetch", "--quiet", "origin", "main"], cwd=ROOT, check=True)
    main = git("rev-parse", f"{observed}^{{commit}}")
    if main != observed:
        raise ValueError("Pinned main source is unavailable after fetching")
    if requested is not None:
        if not ancestor(requested, main):
            if allow_unmerged:
                return {"unmerged": True}
            raise ValueError("Requested publication source is not contained in main")
        main = requested
    source = implementation(main)
    version = tomllib.loads(git("show", f"{source}:Cargo.toml"))["package"]["version"]
    return {"main": main, "source": source, "version": version, "tag": f"v{version}",
            "selected_at": datetime.now(timezone.utc).isoformat()}


def require_version(repository, base):
    from scripts.release_plugin import GitHub, platform_tag
    previous = tomllib.loads(git("show", f"{base}:Cargo.toml"))["package"]["version"]
    version = tomllib.loads(git("show", "HEAD:Cargo.toml"))["package"]["version"]
    if tuple(map(int, version.split("."))) <= tuple(map(int, previous.split("."))):
        raise ValueError("Freeze a fresh release version before validating a changed implementation")
    forge = GitHub(repository)
    tag = f"v{version}"
    for claim in (tag, *(platform_tag(tag, target) for target in sorted(TARGETS.values()))):
        if forge.api(f"git/ref/tags/{claim}") is not None and (
            forge.api(f"commits/{claim}")["sha"] != git("rev-parse", "HEAD")
        ):
            raise ValueError("This release version already belongs to different immutable source")


class CandidatePending(ValueError):
    pass


def merge_run(repository, source, label=None):
    records = json.loads(github("api", f"repos/{repository}/actions/workflows/"
                               f"plugin-candidate.yml/runs?head_sha={source}&per_page=100"))
    matches = [run for run in records["workflow_runs"]
               if run["head_sha"] == source and run["head_branch"].startswith("codex/integration-")
               and run["event"] in ("push", "workflow_dispatch")]
    if not matches:
        raise ValueError("This main implementation has no frozen merge-candidate run")
    run = max(matches, key=lambda entry: entry["id"])
    jobs = json.loads(github("api", f"repos/{repository}/actions/runs/{run['id']}"
                            "/jobs?filter=latest&per_page=100"))
    if label == "macos-arm64" and run["status"] != "completed":
        mac = next((job for job in jobs["jobs"] if job["name"] == "Candidate (macos-arm64)"), None)
        if mac is None or mac["status"] != "completed":
            raise CandidatePending("Mac candidate validation is pending")
    required = LINUX_JOBS if label == "linux-x86_64" else JOBS
    validated = validate_run(run, jobs, {"commit": source}, repository, required)
    run["validated_jobs"] = validated
    return run


def platform_evidence(repository, run, source, label, directory, job=None):
    output = directory / label
    output.mkdir()
    github("run", "download", str(run), "--repo", repository,
           "--name", artifact_name(f"acceptance-{label}", job or {}), "--dir", str(output))
    record = json.loads((output / "VALIDATION.json").read_text())
    validate_artifact(output, source, label, record.get("plan", {}).get("base"))
    return label, {"validation_sha256": hashlib.sha256(
        (output / "VALIDATION.json").read_bytes()).hexdigest(), "report": record,
        "job": {key: job.get(key) for key in ("id", "run_attempt", "html_url")} if job else None}


def verify_merge(repository, directory, target=None):
    source = identity()
    if git("status", "--porcelain", "--untracked-files=all"):
        raise ValueError("Release verification requires a clean fixed main source")
    subprocess.run(["git", "merge-base", "--is-ancestor", source["commit"], "origin/main"],
                   cwd=ROOT, check=True)
    labels = tuple(label for label, platform in TARGETS.items() if target in (None, platform))
    if not labels:
        raise ValueError("Unsupported release target")
    run = merge_run(repository, source["commit"], *labels) if target else merge_run(repository, source["commit"])
    directory.mkdir(parents=True, exist_ok=False)
    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(platform_evidence, repository, run["id"], source, label, directory,
                                  run.get("validated_jobs", {}).get(f"Candidate ({label})"))
                   for label in labels]
        reports = dict(future.result() for future in futures)
    if identity() != source:
        raise ValueError("Release source changed while checking merge evidence")
    result = {"source": source, "merge_run": run["id"], "platforms": reports,
              "verified_at": datetime.now(timezone.utc).isoformat()}
    (directory / "MERGE.json").write_text(json.dumps(result, indent=2) + "\n")
    return result

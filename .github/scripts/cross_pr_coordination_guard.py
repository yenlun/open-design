#!/usr/bin/env python3
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lib.github import GitHubError, api_json, append_summary


SIGNAL_COMPETING = "COMPETING_IMPLEMENTATIONS"
SIGNAL_REVIEW = "REVIEW_CONTRADICTION"
SIGNAL_VALIDATION = "DUPLICATE_VALIDATION"
VALIDATION_LABELS = {"needs-validation"}
ISSUE_REFERENCE_RE = re.compile(
    r"(?i)(?:fix(?:e[sd])?|close[sd]?|resolve[sd]?|issue|bug)\s*:?[ \t]*#(\d+)"
)
PR_REFERENCE_RE = re.compile(r"(?i)(?:replace[sd]?|supersede[sd]?|reopen(?:ing|ed)?)\s+(?:pr\s*)?#(\d+)")
TITLE_WORD_RE = re.compile(r"[a-z0-9]+")
CANDIDATE_TITLE_SIMILARITY = 0.45


class GuardError(ValueError):
    pass


@dataclass(frozen=True)
class Review:
    reviewer: str
    state: str
    commit_id: str
    submitted_at: str


@dataclass(frozen=True)
class PullRequest:
    number: int
    title: str
    body: str
    state: str
    draft: bool
    head_sha: str
    labels: frozenset[str]
    files: dict[str, str]
    reviews: tuple[Review, ...]


def require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GuardError(f"{label} must be an object")
    return value


def require_array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise GuardError(f"{label} must be an array")
    return value


def text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise GuardError(f"{label} must be a positive integer")
    return value


def parse_review(value: Any, label: str) -> Review:
    data = require_object(value, label)
    reviewer = text(data.get("reviewer") or data.get("user"))
    state = text(data.get("state")).upper()
    if not reviewer or state not in {"APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED"}:
        raise GuardError(f"{label} has an invalid reviewer or state")
    return Review(
        reviewer=reviewer,
        state=state,
        commit_id=text(data.get("commit_id")),
        submitted_at=text(data.get("submitted_at")),
    )


def parse_pull_request(value: Any, label: str) -> PullRequest:
    data = require_object(value, label)
    files_value = require_object(data.get("files"), f"{label}.files")
    files: dict[str, str] = {}
    for path, patch in files_value.items():
        if not isinstance(path, str) or not path or not isinstance(patch, str):
            raise GuardError(f"{label}.files must map paths to patch strings")
        files[path] = patch
    labels = require_array(data.get("labels", []), f"{label}.labels")
    if not all(isinstance(item, str) and item for item in labels):
        raise GuardError(f"{label}.labels must contain non-empty strings")
    reviews = tuple(
        parse_review(item, f"{label}.reviews[{index}]")
        for index, item in enumerate(require_array(data.get("reviews", []), f"{label}.reviews"))
    )
    return PullRequest(
        number=positive_int(data.get("number"), f"{label}.number"),
        title=text(data.get("title")),
        body=text(data.get("body")),
        state=text(data.get("state")).lower(),
        draft=data.get("draft") is True,
        head_sha=text(data.get("head_sha")),
        labels=frozenset(labels),
        files=files,
        reviews=reviews,
    )


def normalized_title(value: str) -> str:
    words = TITLE_WORD_RE.findall(value.lower())
    prefixes = {"fix", "feat", "docs", "test", "ci", "chore", "refactor"}
    while words and words[0] in prefixes:
        words.pop(0)
    return " ".join(words)


def issue_references(pr: PullRequest) -> set[int]:
    return {int(match) for match in ISSUE_REFERENCE_RE.findall(f"{pr.title}\n{pr.body}")}


def replacement_references(pr: PullRequest) -> set[int]:
    return {int(match) for match in PR_REFERENCE_RE.findall(f"{pr.title}\n{pr.body}")}


def candidate_relation(left: PullRequest, right: PullRequest) -> dict[str, Any] | None:
    shared_issues = sorted(issue_references(left) & issue_references(right))
    replacement = left.number in replacement_references(right) or right.number in replacement_references(left)
    left_title = normalized_title(left.title)
    right_title = normalized_title(right.title)
    title_similarity = (
        difflib.SequenceMatcher(None, left_title, right_title, autojunk=False).ratio()
        if left_title and right_title
        else 0.0
    )
    if not shared_issues and not replacement and title_similarity < CANDIDATE_TITLE_SIMILARITY:
        return None
    return {
        "shared_issues": shared_issues,
        "replacement": replacement,
        "title_similarity": round(title_similarity, 3),
    }


def normalized_patch_lines(patch: str) -> list[str]:
    result: list[str] = []
    for line in patch.splitlines():
        if line.startswith(("+++", "---")) or not line.startswith(("+", "-")):
            continue
        normalized = " ".join(line[1:].split())
        if normalized:
            result.append(normalized)
    return result


def implementation_similarity(left: PullRequest, right: PullRequest) -> dict[str, Any]:
    left_files = set(left.files)
    right_files = set(right.files)
    shared_files = sorted(left_files & right_files)
    union = left_files | right_files
    file_similarity = len(shared_files) / len(union) if union else 0.0
    left_lines = [line for path in shared_files for line in normalized_patch_lines(left.files[path])]
    right_lines = [line for path in shared_files for line in normalized_patch_lines(right.files[path])]
    patch_similarity = (
        difflib.SequenceMatcher(None, left_lines, right_lines, autojunk=False).ratio()
        if left_lines and right_lines
        else 0.0
    )
    return {
        "file_similarity": round(file_similarity, 3),
        "patch_similarity": round(patch_similarity, 3),
        "shared_files": shared_files,
        "same_file_set": bool(left_files) and left_files == right_files,
    }


def is_competing(relation: dict[str, Any], similarity: dict[str, Any]) -> bool:
    file_similarity = float(similarity["file_similarity"])
    patch_similarity = float(similarity["patch_similarity"])
    has_shared_files = bool(similarity["shared_files"])
    strong_relation = bool(relation["shared_issues"] or relation["replacement"])
    if strong_relation and has_shared_files and file_similarity >= 0.15:
        return True
    if (
        float(relation["title_similarity"]) >= CANDIDATE_TITLE_SIMILARITY
        and has_shared_files
        and file_similarity >= 0.3
    ):
        return True
    if file_similarity < 0.8:
        return False
    if patch_similarity >= 0.75:
        return True
    return strong_relation and similarity["same_file_set"] and patch_similarity >= 0.2


def effective_review_state(pr: PullRequest) -> str | None:
    latest_by_reviewer: dict[str, Review] = {}
    for review in sorted(pr.reviews, key=lambda item: (item.submitted_at, item.reviewer)):
        if review.commit_id != pr.head_sha:
            continue
        latest_by_reviewer[review.reviewer.casefold()] = review
    states = {review.state for review in latest_by_reviewer.values()}
    if "CHANGES_REQUESTED" in states:
        return "CHANGES_REQUESTED"
    if "APPROVED" in states:
        return "APPROVED"
    return None


def analyze(pull_requests: list[PullRequest]) -> list[dict[str, Any]]:
    active = sorted(
        (pr for pr in pull_requests if pr.state == "open" and not pr.draft),
        key=lambda pr: pr.number,
    )
    findings: list[dict[str, Any]] = []
    for index, left in enumerate(active):
        for right in active[index + 1 :]:
            relation = candidate_relation(left, right)
            if relation is None:
                continue
            similarity = implementation_similarity(left, right)
            if not is_competing(relation, similarity):
                continue
            pair = [left.number, right.number]
            evidence = {**relation, **similarity}
            findings.append({"signal": SIGNAL_COMPETING, "prs": pair, "evidence": evidence})

            left_review = effective_review_state(left)
            right_review = effective_review_state(right)
            if {left_review, right_review} == {"APPROVED", "CHANGES_REQUESTED"}:
                findings.append(
                    {
                        "signal": SIGNAL_REVIEW,
                        "prs": pair,
                        "evidence": {**evidence, "review_states": [left_review, right_review]},
                    }
                )

            if left.labels & VALIDATION_LABELS and right.labels & VALIDATION_LABELS:
                findings.append(
                    {
                        "signal": SIGNAL_VALIDATION,
                        "prs": pair,
                        "evidence": {
                            **evidence,
                            "validation_labels": sorted(
                                (left.labels & VALIDATION_LABELS) | (right.labels & VALIDATION_LABELS)
                            ),
                        },
                    }
                )
    return findings


def repository_path(repository: str, suffix: str) -> str:
    parts = repository.split("/")
    if len(parts) != 2 or not all(parts):
        raise GuardError("repository must be in owner/name form")
    quoted = "/".join(urllib.parse.quote(part, safe="") for part in parts)
    return f"/repos/{quoted}/{suffix.lstrip('/')}"


def github_pull(repository: str, value: dict[str, Any]) -> PullRequest:
    number = positive_int(value.get("number"), "pull.number")
    files_value = api_json(repository_path(repository, f"pulls/{number}/files?per_page=100"))
    reviews_value = api_json(repository_path(repository, f"pulls/{number}/reviews?per_page=100"))
    files = {
        text(item.get("filename")): text(item.get("patch"))
        for item in require_array(files_value, f"pull #{number} files")
        if isinstance(item, dict) and text(item.get("filename"))
    }
    reviews = [
        {
            "reviewer": text(require_object(item, "review").get("user", {}).get("login"))
            if isinstance(item.get("user"), dict)
            else "",
            "state": item.get("state"),
            "commit_id": item.get("commit_id"),
            "submitted_at": item.get("submitted_at"),
        }
        for item in require_array(reviews_value, f"pull #{number} reviews")
        if isinstance(item, dict)
    ]
    return parse_pull_request(
        {
            "number": number,
            "title": value.get("title"),
            "body": value.get("body"),
            "state": value.get("state"),
            "draft": value.get("draft"),
            "head_sha": require_object(value.get("head"), "pull.head").get("sha"),
            "labels": [
                text(item.get("name"))
                for item in require_array(value.get("labels", []), "pull.labels")
                if isinstance(item, dict) and text(item.get("name"))
            ],
            "files": files,
            "reviews": reviews,
        },
        f"pull #{number}",
    )


def live_pull_requests(repository: str, current_number: int) -> list[PullRequest]:
    summaries: list[dict[str, Any]] = []
    for page in range(1, 11):
        value = api_json(repository_path(repository, f"pulls?state=open&base=main&per_page=100&page={page}"))
        batch = require_array(value, f"open pulls page {page}")
        summaries.extend(require_object(item, "pull summary") for item in batch)
        if len(batch) < 100:
            break
    current_value = next((item for item in summaries if item.get("number") == current_number), None)
    if current_value is None:
        current_value = require_object(
            api_json(repository_path(repository, f"pulls/{current_number}")),
            "current pull",
        )
        summaries.append(current_value)
    current_stub = parse_pull_request(
        {
            "number": current_value.get("number"),
            "title": current_value.get("title"),
            "body": current_value.get("body"),
            "state": current_value.get("state"),
            "draft": current_value.get("draft"),
            "head_sha": require_object(current_value.get("head"), "current pull.head").get("sha"),
            "labels": [
                text(item.get("name"))
                for item in require_array(current_value.get("labels", []), "current pull.labels")
                if isinstance(item, dict) and text(item.get("name"))
            ],
            "files": {},
            "reviews": [],
        },
        "current pull",
    )
    candidates = [
        item
        for item in summaries
        if item.get("number") == current_number
        or candidate_relation(
            current_stub,
            parse_pull_request(
                {
                    "number": item.get("number"),
                    "title": item.get("title"),
                    "body": item.get("body"),
                    "state": item.get("state"),
                    "draft": item.get("draft"),
                    "head_sha": require_object(item.get("head"), "candidate.head").get("sha"),
                    "labels": [
                        text(label.get("name"))
                        for label in require_array(item.get("labels", []), "candidate.labels")
                        if isinstance(label, dict) and text(label.get("name"))
                    ],
                    "files": {},
                    "reviews": [],
                },
                "candidate",
            ),
        )
        is not None
    ]
    return [github_pull(repository, item) for item in candidates]


def finding_key(finding: dict[str, Any]) -> tuple[str, tuple[int, ...]]:
    return text(finding.get("signal")), tuple(finding.get("prs", []))


def load_fixture(path: Path) -> tuple[list[PullRequest], set[tuple[str, tuple[int, ...]]]]:
    try:
        data = require_object(json.loads(path.read_text(encoding="utf-8")), "fixture")
    except (OSError, json.JSONDecodeError) as error:
        raise GuardError(f"cannot load fixture {path}: {error}") from error
    if data.get("schema_version") != 1:
        raise GuardError("fixture.schema_version must be 1")
    pulls = [
        parse_pull_request(item, f"fixture.pull_requests[{index}]")
        for index, item in enumerate(require_array(data.get("pull_requests"), "fixture.pull_requests"))
    ]
    expected: set[tuple[str, tuple[int, ...]]] = set()
    for index, item in enumerate(require_array(data.get("expected_findings"), "fixture.expected_findings")):
        entry = require_object(item, f"fixture.expected_findings[{index}]")
        expected.add((text(entry.get("signal")), tuple(require_array(entry.get("prs"), "expected prs"))))
    return pulls, expected


def render_summary(findings: list[dict[str, Any]], *, unavailable: str | None = None) -> str:
    lines = ["## Cross-PR coordination guard (shadow)", ""]
    if unavailable:
        lines.extend(["Observation unavailable; CI remains non-blocking.", "", f"Reason: `{unavailable}`"])
        return "\n".join(lines)
    if not findings:
        lines.append("No high-confidence coordination signals detected.")
        return "\n".join(lines)
    lines.extend(
        [
            "Read-only observation only: no comments, labels, closures, or merge gates were changed.",
            "",
            "| Signal | PRs | File overlap | Patch similarity |",
            "| --- | --- | ---: | ---: |",
        ]
    )
    for finding in findings:
        evidence = require_object(finding.get("evidence"), "finding.evidence")
        lines.append(
            f"| `{finding['signal']}` | "
            f"{' / '.join(f'#{number}' for number in finding['prs'])} | "
            f"{float(evidence['file_similarity']):.3f} | {float(evidence['patch_similarity']):.3f} |"
        )
    return "\n".join(lines)


def emit_result(findings: list[dict[str, Any]], output: Path | None) -> None:
    payload = {"schema_version": 1, "mode": "shadow", "findings": findings}
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(serialized, encoding="utf-8")
    print(serialized, end="")


def main() -> int:
    parser = argparse.ArgumentParser(description="Observe high-confidence coordination risks across open PRs.")
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--pr-number", type=int)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--strict", action="store_true", help="Return non-zero on observation errors.")
    args = parser.parse_args()

    fixture = args.fixture
    if args.self_check and fixture is None:
        fixture = Path(__file__).resolve().parents[1] / "fixtures" / "cross-pr-coordination-history.json"
    try:
        expected: set[tuple[str, tuple[int, ...]]] | None = None
        if fixture:
            pull_requests, expected = load_fixture(fixture)
        else:
            if not args.repository or not args.pr_number:
                raise GuardError("--repository and --pr-number are required for live observation")
            pull_requests = live_pull_requests(args.repository, args.pr_number)
        findings = analyze(pull_requests)
        if expected is not None:
            actual = {finding_key(finding) for finding in findings}
            if actual != expected:
                raise GuardError(f"fixture findings differ: expected={sorted(expected)}, actual={sorted(actual)}")
        emit_result(findings, args.output)
        append_summary(render_summary(findings))
        return 0
    except (GuardError, GitHubError) as error:
        append_summary(render_summary([], unavailable=str(error)))
        print(f"cross-pr coordination observation unavailable: {error}", file=sys.stderr)
        return 1 if args.strict or args.self_check or fixture else 0


if __name__ == "__main__":
    raise SystemExit(main())

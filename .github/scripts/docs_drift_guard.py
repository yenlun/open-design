#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lib.github import append_summary


class DriftError(ValueError):
    pass


@dataclass(frozen=True)
class Anchor:
    path: str
    pattern: re.Pattern[str]


@dataclass(frozen=True)
class DocumentSelector:
    glob: str
    contradictory_pattern: re.Pattern[str]


@dataclass(frozen=True)
class Rule:
    rule_id: str
    description: str
    sources: tuple[str, ...]
    anchors: tuple[Anchor, ...]
    documents: tuple[DocumentSelector, ...]


def require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DriftError(f"{label} must be an object")
    return value


def require_array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise DriftError(f"{label} must be an array")
    return value


def require_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DriftError(f"{label} must be a non-empty string")
    return value.strip()


def safe_relative(value: Any, label: str) -> str:
    path = require_text(value, label)
    candidate = Path(path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise DriftError(f"{label} must be a safe repository-relative path")
    return path


def compile_pattern(value: Any, label: str) -> re.Pattern[str]:
    try:
        return re.compile(require_text(value, label), re.IGNORECASE)
    except re.error as error:
        raise DriftError(f"{label} is invalid: {error}") from error


def load_rules(path: Path) -> list[Rule]:
    try:
        config = require_object(json.loads(path.read_text(encoding="utf-8")), "config")
    except (OSError, json.JSONDecodeError) as error:
        raise DriftError(f"cannot load {path}: {error}") from error
    if config.get("schema_version") != 1:
        raise DriftError("config.schema_version must be 1")
    rules: list[Rule] = []
    seen: set[str] = set()
    for index, value in enumerate(require_array(config.get("rules"), "config.rules")):
        data = require_object(value, f"config.rules[{index}]")
        rule_id = require_text(data.get("id"), f"config.rules[{index}].id")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", rule_id) or rule_id in seen:
            raise DriftError(f"invalid or duplicate rule id: {rule_id}")
        seen.add(rule_id)
        anchors = tuple(
            Anchor(
                path=safe_relative(require_object(item, "anchor").get("path"), "anchor.path"),
                pattern=compile_pattern(require_object(item, "anchor").get("pattern"), "anchor.pattern"),
            )
            for item in require_array(data.get("truth_anchors"), f"rule {rule_id}.truth_anchors")
        )
        documents = tuple(
            DocumentSelector(
                glob=safe_relative(require_object(item, "document").get("glob"), "document.glob"),
                contradictory_pattern=compile_pattern(
                    require_object(item, "document").get("contradictory_pattern"),
                    "document.contradictory_pattern",
                ),
            )
            for item in require_array(data.get("documents"), f"rule {rule_id}.documents")
        )
        if not anchors or not documents:
            raise DriftError(f"rule {rule_id} must have truth anchors and document selectors")
        sources = tuple(require_text(item, f"rule {rule_id}.sources") for item in require_array(data.get("sources", []), f"rule {rule_id}.sources"))
        rules.append(
            Rule(
                rule_id=rule_id,
                description=require_text(data.get("description"), f"rule {rule_id}.description"),
                sources=sources,
                anchors=anchors,
                documents=documents,
            )
        )
    return rules


def read_text(root: Path, relative: str) -> str:
    path = root / relative
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise DriftError(f"cannot read truth source {relative}: {error}") from error


def active_truth(root: Path, rule: Rule) -> list[dict[str, Any]] | None:
    evidence: list[dict[str, Any]] = []
    for anchor in rule.anchors:
        source = read_text(root, anchor.path)
        match = anchor.pattern.search(source)
        if match is None:
            return None
        line = source.count("\n", 0, match.start()) + 1
        evidence.append({"path": anchor.path, "line": line})
    return evidence


def analyze(root: Path, rules: list[Rule]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for rule in rules:
        truth_evidence = active_truth(root, rule)
        if truth_evidence is None:
            continue
        seen_paths: set[str] = set()
        for selector in rule.documents:
            for path in sorted(root.glob(selector.glob)):
                if not path.is_file():
                    continue
                relative = path.relative_to(root).as_posix()
                if relative in seen_paths:
                    continue
                seen_paths.add(relative)
                try:
                    lines = path.read_text(encoding="utf-8").splitlines()
                except OSError as error:
                    raise DriftError(f"cannot read document {relative}: {error}") from error
                for line_number, line in enumerate(lines, start=1):
                    if selector.contradictory_pattern.search(line):
                        findings.append(
                            {
                                "rule": rule.rule_id,
                                "description": rule.description,
                                "document": {"path": relative, "line": line_number, "text": line.strip()},
                                "truth": truth_evidence,
                                "sources": list(rule.sources),
                            }
                        )
    return findings


def render_summary(findings: list[dict[str, Any]], unavailable: str | None = None) -> str:
    lines = ["## Documentation drift guard (shadow)", ""]
    if unavailable:
        lines.extend(["Observation unavailable; CI remains non-blocking.", "", f"Reason: `{unavailable}`"])
    elif not findings:
        lines.append("No configured high-confidence documentation contradictions detected.")
    else:
        lines.extend(
            [
                "Read-only observation only: no documentation or merge state was changed.",
                "",
                "| Rule | Document claim | Machine-backed truth |",
                "| --- | --- | --- |",
            ]
        )
        for finding in findings:
            document = require_object(finding.get("document"), "finding.document")
            truth = require_array(finding.get("truth"), "finding.truth")
            anchors = ", ".join(
                f"`{require_object(item, 'truth').get('path')}:{require_object(item, 'truth').get('line')}`"
                for item in truth
            )
            lines.append(
                f"| `{finding['rule']}` | `{document['path']}:{document['line']}` | {anchors} |"
            )
    return "\n".join(lines)


def emit(findings: list[dict[str, Any]], output: Path | None) -> None:
    payload = {"schema_version": 1, "mode": "shadow", "findings": findings}
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(serialized, encoding="utf-8")
    print(serialized, end="")


def main() -> int:
    default_config = Path(__file__).resolve().parents[1] / "config" / "docs-drift.json"
    parser = argparse.ArgumentParser(description="Observe explicit documentation claims that contradict repository truth sources.")
    parser.add_argument("--config", type=Path, default=default_config)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path)
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    if args.self_check:
        root = Path(__file__).resolve().parents[1] / "fixtures" / "docs-drift" / "typescript-first-daemon"
    try:
        findings = analyze(root, load_rules(args.config))
        if args.self_check:
            keys = {(finding["rule"], finding["document"]["path"]) for finding in findings}
            expected = {("typescript-first-daemon", "CONTRIBUTING.md")}
            if keys != expected:
                raise DriftError(f"fixture findings differ: expected={sorted(expected)}, actual={sorted(keys)}")
        emit(findings, args.output)
        append_summary(render_summary(findings))
        return 0
    except DriftError as error:
        append_summary(render_summary([], unavailable=str(error)))
        print(f"documentation drift observation unavailable: {error}", file=sys.stderr)
        return 1 if args.strict or args.self_check else 0


if __name__ == "__main__":
    raise SystemExit(main())

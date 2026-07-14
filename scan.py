#!/usr/bin/env python3
import argparse
import concurrent.futures
import csv
import html
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from server import (
    DEFAULT_REPOS,
    LOOKUP_LIMIT,
    MAX_WORKERS,
    REPO_ENV,
    TRANSITIVE_PER_DEP_LIMIT,
    lookup_one,
    parse_gradle_dependencies,
    parse_pom_xml,
    resolve_maven_dependencies,
)


RULES = [
    {
        "severity": "high",
        "title": "Internal JDK API usage (sun.* / com.sun.*)",
        "why": "These APIs are encapsulated in newer JDKs and often fail at runtime on Java 17+.",
        "action": "Replace with supported java.* APIs or maintained libraries.",
        "pattern": re.compile(r"\b(?:sun\.misc|sun\.reflect|com\.sun\.)\b"),
        "kinds": {"java", "pasted"},
    },
    {
        "severity": "high",
        "title": "Legacy javax imports detected",
        "why": "Modern Spring Boot/Jakarta stacks require jakarta.* namespace for many APIs.",
        "action": "Migrate javax.* imports to jakarta.* where framework versions require it.",
        "pattern": re.compile(r"\bimport\s+javax\.(?:annotation|xml\.bind|activation|servlet|persistence|validation|ws\.rs)\b"),
        "kinds": {"java", "pasted"},
    },
    {
        "severity": "high",
        "title": "Deep reflection (setAccessible(true))",
        "why": "Strong encapsulation in modern JDKs can block this without extra JVM flags.",
        "action": "Prefer public APIs. If needed short-term, use explicit --add-opens while migrating.",
        "pattern": re.compile(r"\bsetAccessible\s*\(\s*true\s*\)"),
        "kinds": {"java", "pasted"},
    },
    {
        "severity": "medium",
        "title": "SecurityManager usage",
        "why": "SecurityManager is deprecated/disabled path in newer JDKs.",
        "action": "Replace with process/container isolation and explicit permission boundaries.",
        "pattern": re.compile(r"\bSecurityManager\b"),
        "kinds": {"java", "pasted"},
    },
    {
        "severity": "medium",
        "title": "finalize() usage",
        "why": "finalize() is deprecated and unreliable for resource cleanup.",
        "action": "Use AutoCloseable + try-with-resources or Cleaner.",
        "pattern": re.compile(r"\bvoid\s+finalize\s*\("),
        "kinds": {"java", "pasted"},
    },
    {
        "severity": "medium",
        "title": "Temporary JVM access flags found",
        "why": "--add-opens/--illegal-access usually indicates compatibility debt.",
        "action": "Track and remove these flags after library/application updates.",
        "pattern": re.compile(r"\b--add-opens\b|\b--illegal-access\b"),
        "kinds": {"build", "config"},
    },
]

ANALYZABLE_FILE_NAMES = {
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "gradle.properties",
    "settings.gradle",
    "settings.gradle.kts",
    "application.properties",
    "application.yml",
    "application.yaml",
}
DEP_CHECK_PAGE_SIZE = 30


def parse_prefixes(raw):
    return [p.strip().lower() for p in str(raw or "").split(",") if p.strip()]


def normalize_path(path):
    return str(path).replace("\\", "/")


def file_kind_by_name(name):
    lower = normalize_path(name).lower()
    if lower.endswith(".java"):
        return "java"
    if lower.endswith("/application.properties") or lower.endswith("/application.yml") or lower.endswith("/application.yaml"):
        return "config"
    if Path(lower).name in ANALYZABLE_FILE_NAMES:
        return "build"
    return "other"


def collect_entries(root):
    root_path = Path(root).resolve()
    entries = []
    for dirpath, _, filenames in os.walk(root_path):
        for filename in filenames:
            full = Path(dirpath) / filename
            rel = normalize_path(full.relative_to(root_path))
            kind = file_kind_by_name(rel)
            if kind == "other":
                continue
            try:
                content = full.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            entries.append({"name": rel, "kind": kind, "content": content})
    return entries


def first_match_line(content, pattern):
    for i, line in enumerate(str(content).splitlines(), start=1):
        if pattern.search(line):
            return i
    return 1


def normalize_java_level(raw):
    if not raw:
        return None
    v = str(raw).strip()
    if v == "1.8":
        return 8
    try:
        return int(v.split(".")[0])
    except Exception:
        return None


def to_major(version):
    m = re.match(r"^(\d+)", str(version or ""))
    return int(m.group(1)) if m else None


def compare_versions(a, b):
    pa = [int(x) for x in re.split(r"[^0-9]+", str(a or "")) if x]
    pb = [int(x) for x in re.split(r"[^0-9]+", str(b or "")) if x]
    size = max(len(pa), len(pb))
    for i in range(size):
        av = pa[i] if i < len(pa) else 0
        bv = pb[i] if i < len(pb) else 0
        if av < bv:
            return -1
        if av > bv:
            return 1
    return 0


def detect_boot_major(entries):
    for entry in [e for e in entries if e["kind"] == "build"]:
        txt = entry["content"]
        parent_match = re.search(r"<parent>([\s\S]*?)</parent>", txt, re.IGNORECASE)
        if parent_match and re.search(r"spring-boot-starter-parent", parent_match.group(1), re.IGNORECASE):
            v = text_of(parent_match.group(1), "version")
            major = to_major(v)
            if major:
                return major
        prop = re.search(r"<spring-boot\.version>\s*([0-9]+)(?:\.[0-9]+)?", txt, re.IGNORECASE)
        if prop:
            return int(prop.group(1))
        gradle = re.search(r"org\.springframework\.boot[\"']?\s+version\s+[\"']([0-9]+)(?:\.[0-9]+)?", txt, re.IGNORECASE)
        if gradle:
            return int(gradle.group(1))
    return None


def detect_configured_java(entries):
    checks = [
        r"<java\.version>\s*([^<\s]+)\s*</java\.version>",
        r"<maven\.compiler\.source>\s*([^<\s]+)\s*</maven\.compiler\.source>",
        r"<maven\.compiler\.target>\s*([^<\s]+)\s*</maven\.compiler\.target>",
        r"\bsourceCompatibility\s*=\s*[\"']([^\"']+)[\"']",
        r"\btargetCompatibility\s*=\s*[\"']([^\"']+)[\"']",
    ]
    for entry in [e for e in entries if e["kind"] == "build"]:
        txt = entry["content"]
        for pat in checks:
            m = re.search(pat, txt, re.IGNORECASE)
            level = normalize_java_level(m.group(1) if m else None)
            if level:
                return level
    return None


def text_of(text, tag_name):
    m = re.search(fr"<{tag_name}>\s*([\s\S]*?)\s*</{tag_name}>", str(text or ""), re.IGNORECASE)
    return m.group(1).strip() if m else None


def extract_declared_dependencies(entries):
    out = []
    for e in [x for x in entries if x["kind"] == "build" and x["name"].lower().endswith("pom.xml")]:
        model = parse_pom_xml(e["name"], e["content"])
        if not model:
            continue
        for d in model.get("dependencies") or []:
            g = d.get("groupId")
            a = d.get("artifactId")
            if g and a:
                out.append({"groupId": g, "artifactId": a, "version": d.get("version"), "files": [e["name"]]})
    out.extend(parse_gradle_dependencies([e for e in entries if e["kind"] == "build" and not e["name"].lower().endswith("pom.xml")]))
    merged = {}
    for d in out:
        ga = f"{d['groupId']}:{d['artifactId']}"
        if ga not in merged:
            merged[ga] = {"groupId": d["groupId"], "artifactId": d["artifactId"], "version": d.get("version"), "files": set(d.get("files") or [])}
        else:
            merged[ga]["files"].update(d.get("files") or [])
            if d.get("version") and not merged[ga]["version"]:
                merged[ga]["version"] = d.get("version")
    return [{"groupId": v["groupId"], "artifactId": v["artifactId"], "version": v["version"], "files": sorted(v["files"])} for v in merged.values()]


def detect_framework(entries, deps):
    build_text = "\n".join([e["content"] for e in entries if e["kind"] == "build"])
    all_text = "\n".join([e["content"] for e in entries])

    def dep_major(predicate):
        for d in deps:
            if predicate(d):
                m = to_major(d.get("version"))
                if m:
                    return m
        return None

    def has_dep(predicate):
        return any(predicate(d) for d in deps)

    explicit_boot = detect_boot_major(entries)
    inferred_boot = dep_major(lambda d: d["groupId"] == "org.springframework.boot" or d["artifactId"].startswith("spring-boot"))
    boot_major = explicit_boot or inferred_boot
    boot_source = "explicit" if explicit_boot else ("inferred" if inferred_boot else "none")
    spring_major = first_major_from(build_text, r"<spring-framework\.version>\s*([0-9]+)(?:\.[0-9]+)?") or dep_major(
        lambda d: d["groupId"] == "org.springframework" and d["artifactId"].startswith("spring-") and not d["artifactId"].startswith("spring-boot")
    )
    hibernate_major = first_major_from(build_text, r"<hibernate(?:\.core)?\.version>\s*([0-9]+)(?:\.[0-9]+)?") or dep_major(
        lambda d: d["groupId"] in {"org.hibernate", "org.hibernate.orm"} and d["artifactId"] == "hibernate-core"
    )
    has_jakarta = bool(re.search(r"\bimport\s+jakarta\.", all_text)) or has_dep(lambda d: str(d["groupId"]).startswith("jakarta."))
    has_spring = bool(re.search(r"org\.springframework|spring-boot|@SpringBootApplication|SpringApplication", all_text)) or has_dep(
        lambda d: d["groupId"] in {"org.springframework", "org.springframework.boot"} or d["artifactId"].startswith("spring-")
    )
    return {
        "hasSpring": has_spring,
        "bootMajor": boot_major,
        "bootSource": boot_source,
        "springMajor": spring_major,
        "hibernateMajor": hibernate_major,
        "hasJakarta": has_jakarta,
    }


def first_major_from(text, pattern):
    m = re.search(pattern, str(text or ""), re.IGNORECASE)
    return int(m.group(1)) if m else None


def framework_matrix_findings(target, framework, build_file_names):
    findings = []
    evidence = [f"{f}:1" for f in build_file_names[:3]]
    if framework["hasSpring"] and target >= 17 and framework["bootMajor"] and framework["bootMajor"] < 3:
        findings.append(new_finding("high", f"Spring Boot {framework['bootMajor']}.x detected on Java {target} path",
            "Java 17+ migrations typically need Spring Boot 3.x + Spring 6 + Jakarta namespace.",
            "Plan Boot 2.7.latest stabilization first, then move to Boot 3.x on Java 17 with jakarta.* migration.",
            build_file_names, evidence))
    if framework["hasSpring"] and target >= 17 and not framework["bootMajor"]:
        findings.append(new_finding("medium", "Spring usage detected but Boot version not found",
            "Compatibility depends on exact Spring/Spring Boot version.",
            "Confirm current framework versions in build files and map them to Java target support matrix.",
            build_file_names, evidence))
    if framework["bootMajor"] and framework["springMajor"] and framework["bootMajor"] >= 3 and framework["springMajor"] < 6:
        explicit = framework["bootSource"] == "explicit"
        findings.append(new_finding("high" if explicit else "medium",
            f"Matrix mismatch: Boot {framework['bootMajor']}.x with Spring {framework['springMajor']}.x" + ("" if explicit else " (inferred Boot)"),
            "Boot 3.x+ is aligned with Spring 6.x+ and Jakarta APIs." if explicit else "Inferred Boot major (from dependencies) conflicts with detected Spring major. Effective BOM may differ.",
            "Check effective BOM/parent versions and align to a supported combination." if explicit else "Resolve effective Boot version from parent/dependencyManagement and confirm matrix alignment.",
            build_file_names, evidence))
    if framework["bootMajor"] and framework["bootMajor"] >= 3 and target < 17:
        findings.append(new_finding("high", f"Spring Boot {framework['bootMajor']}.x with Java {target} target",
            "Boot 3.x+ requires Java 17+.", "Raise target to Java 17+ or use Boot 2.7.x for lower targets.", build_file_names, evidence))
    if framework["springMajor"] and framework["springMajor"] >= 6 and target < 17:
        findings.append(new_finding("high", f"Spring Framework {framework['springMajor']}.x with Java {target} target",
            "Spring 6.x line requires Java 17+.", "Use Spring 5.3.x for Java 11, or raise target to Java 17+ with Spring 6.x.", build_file_names, evidence))
    if framework["hasSpring"] and target >= 17 and framework["springMajor"] and framework["springMajor"] < 6:
        findings.append(new_finding("medium", f"Spring Framework {framework['springMajor']}.x on Java {target} path",
            "Java 17 migrations usually pair with Spring 6.x and Jakarta alignment.",
            "Evaluate Spring 6.x upgrade timing and dependency compatibility before cutover.", build_file_names, evidence))
    if framework["hasSpring"] and framework["bootMajor"] and framework["bootMajor"] >= 3 and not framework["hasJakarta"]:
        findings.append(new_finding("medium", "Boot 3.x+ detected but Jakarta APIs not detected",
            "Boot 3 migrations typically include jakarta.* API usage.",
            "Verify javax->jakarta migration scope and dependencies.", build_file_names, evidence))
    if framework["hibernateMajor"] and framework["hibernateMajor"] >= 6 and target < 11:
        findings.append(new_finding("medium", f"Hibernate {framework['hibernateMajor']}.x with Java {target} target",
            "Hibernate 6.x needs newer Java baselines than Java 8.", "Use Hibernate 5.x for Java 8, or raise target Java to 11+.",
            build_file_names, evidence))
    if framework["hasSpring"] or framework["hibernateMajor"] or framework["hasJakarta"]:
        detected = ", ".join([
            f"boot={framework['bootMajor'] or 'unknown'}({framework['bootSource']})",
            f"spring={framework['springMajor'] or 'unknown'}",
            f"hibernate={framework['hibernateMajor'] or 'unknown'}",
            f"jakarta={'yes' if framework['hasJakarta'] else 'no'}",
        ])
        findings.append(new_finding("low", "Framework compatibility matrix check", f"Detected {detected}.",
            "Matrix: Java 11 -> Boot 2.7.x + Spring 5.3.x. Java 17+ -> Boot 3.x + Spring 6.x + Jakarta APIs.", build_file_names, evidence))
    return findings


def new_finding(severity, title, why, action, files, evidence):
    return {
        "severity": severity,
        "title": title,
        "why": why,
        "action": action,
        "files": list(dict.fromkeys(files or [])),
        "evidence": evidence or [],
    }


def analyze_entries(entries, target):
    findings = []
    all_text = "\n\n".join([e["content"] for e in entries])
    build_files = [e["name"] for e in entries if e["kind"] == "build"]
    declared_deps = extract_declared_dependencies(entries)
    framework = detect_framework(entries, declared_deps)
    configured_java = detect_configured_java(entries)

    for rule in RULES:
        evidence = []
        files = []
        for entry in entries:
            if entry["kind"] not in rule["kinds"]:
                continue
            if not rule["pattern"].search(entry["content"]):
                continue
            files.append(entry["name"])
            evidence.append(f"{entry['name']}:{first_match_line(entry['content'], rule['pattern'])}")
        if evidence:
            findings.append(new_finding(rule["severity"], rule["title"], rule["why"], rule["action"], files, evidence))

    if re.search(r"\bStream\b", all_text) and not re.search(r"\bparallelStream\b", all_text):
        stream_files = [e["name"] for e in entries if re.search(r"\bStream\b", e["content"])]
        findings.append(new_finding("low", "Streams detected (performance review candidate)",
            "Not a migration blocker, but hotspot tuning can differ on newer JDKs.",
            "Profile first, then optimize only bottlenecks.", stream_files, []))

    findings.extend(framework_matrix_findings(target, framework, build_files))
    if configured_java and configured_java < target:
        findings.append(new_finding("medium", f"Build is pinned to Java {configured_java}",
            "Current compiler/runtime level is below selected migration target.",
            f"Update build Java level in Maven/Gradle to {target} during migration.",
            build_files, [f"{f}:1" for f in build_files[:3]]))

    context = {
        "hasSpring": framework["hasSpring"],
        "bootMajor": framework["bootMajor"],
        "configuredJava": configured_java,
        "framework": framework,
    }
    return {"findings": findings, "context": context}


def plan_from_analysis(findings, target, context):
    path = [v for v in (11, 17, 21, 25) if v <= target]
    plan = ["Build + test baseline on Java 8 and freeze dependency versions."]
    if context.get("hasSpring") and target >= 17 and context.get("bootMajor") and context["bootMajor"] < 3:
        plan.append("For Spring Boot: upgrade to 2.7.latest first, then migrate to Boot 3.x/Spring 6 on Java 17.")
    elif context.get("hasSpring"):
        plan.append("Verify Spring/Spring Boot compatibility for each Java jump before upgrading production.")
    if any("Legacy javax imports" in f.get("title", "") for f in findings):
        plan.append("Migrate javax.* APIs to jakarta.* where required by your framework stack.")
    if any(str(f.get("severity")).lower() in {"high", "medium"} for f in findings):
        plan.append("Fix high/medium findings before final cutover.")
    for v in path:
        plan.append(f"Upgrade to Java {v}{' LTS' if v in {17, 21} else ''}, then compile/test and fix blockers.")
    plan.append("Remove temporary JVM compatibility flags and run performance regression checks.")
    return [f"{i + 1}) {step}" for i, step in enumerate(plan)]


def summarize_entries(entries):
    java_files = [e["name"] for e in entries if e["kind"] == "java"]
    build_files = [e["name"] for e in entries if e["kind"] == "build"]
    config_files = [e["name"] for e in entries if e["kind"] == "config"]
    return {
        "javaCount": len(java_files),
        "buildCount": len(build_files),
        "configCount": len(config_files),
        "buildFiles": sorted(set(build_files)),
    }


def resolve_dependencies(entries):
    repos, internal_repos = current_repos()
    poms = [{"name": e["name"], "content": e["content"]} for e in entries if e["kind"] == "build" and e["name"].lower().endswith("pom.xml")]
    gradles = [{"name": e["name"], "content": e["content"]} for e in entries if e["kind"] == "build" and not e["name"].lower().endswith("pom.xml")]
    pom_deps = resolve_maven_dependencies(poms, repos, internal_repos)
    gradle_deps = parse_gradle_dependencies(gradles)
    merged = {}
    for dep in pom_deps + gradle_deps:
        ga = f"{dep['groupId']}:{dep['artifactId']}"
        if ga not in merged:
            merged[ga] = {"groupId": dep["groupId"], "artifactId": dep["artifactId"], "version": dep.get("version"), "files": set(dep.get("files") or [])}
        else:
            merged[ga]["files"].update(dep.get("files") or [])
            if dep.get("version") and (not merged[ga]["version"] or "${" in str(merged[ga]["version"])):
                merged[ga]["version"] = dep["version"]
    return [{"groupId": v["groupId"], "artifactId": v["artifactId"], "version": v["version"], "files": sorted(v["files"])} for v in merged.values()]


def current_repos():
    extra = [r.strip() for r in os.getenv(REPO_ENV, "").split(",") if r.strip()]
    return extra + DEFAULT_REPOS, extra


def is_static_version(v):
    s = str(v or "")
    return bool(s) and "${" not in s and "$" not in s and "snapshot" not in s.lower()


def dependency_java_hint(dep, target, latest):
    ga = f"{dep['groupId']}:{dep['artifactId']}"
    current_major = to_major(dep.get("version"))
    latest_major = to_major(latest)
    if dep["groupId"] == "org.springframework.boot" or dep["artifactId"].startswith("spring-boot"):
        if target >= 17 and not current_major:
            return {"severity": "medium", "note": f"Spring Boot version is managed (not explicit). Verify effective version is Boot 3.x+ for Java {target} target."}
        if target >= 17 and current_major and current_major < 3:
            return {"severity": "high", "note": "Boot 2.x on Java 17+ path. Plan move to Boot 3.x (Jakarta migration)."}
        if target < 17 and current_major and current_major >= 3:
            return {"severity": "high", "note": "Boot 3.x requires Java 17+; this target is too low."}
    if dep["groupId"] == "org.springframework" or dep["artifactId"].startswith("spring-"):
        if target >= 17 and current_major and current_major < 6:
            return {"severity": "medium", "note": "Spring 5.x on Java 17 path; verify upgrade plan to Spring 6.x with Boot 3/Jakarta."}
        if target < 17 and latest_major and latest_major >= 6:
            return {"severity": "medium", "note": "Spring 6.x line usually needs Java 17+; keep compatible major for lower target."}
    if ga == "jakarta.servlet:jakarta.servlet-api" and target < 11:
        return {"severity": "medium", "note": "Jakarta ecosystem is usually aligned with newer Java and Boot 3+ stacks."}
    if dep["groupId"] in {"org.hibernate.orm", "org.hibernate"} and dep["artifactId"] == "hibernate-core":
        if target < 11 and ((current_major and current_major >= 6) or (latest_major and latest_major >= 6)):
            return {"severity": "medium", "note": "Hibernate 6.x generally requires Java 11+; keep Hibernate 5.x for Java 8 targets."}
    return {"severity": "low", "note": "No special Java-version rule detected for this dependency."}


def run_dependency_checks(entries, target, include_transitive):
    deps = resolve_dependencies(entries)
    repos, internal_repos = current_repos()
    limited = deps[:LOOKUP_LIMIT]
    by_ga = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [
            pool.submit(lookup_one, dep, repos, internal_repos, include_transitive, TRANSITIVE_PER_DEP_LIMIT if include_transitive else 0)
            for dep in limited
        ]
        for fut in concurrent.futures.as_completed(futures):
            item = fut.result()
            if item:
                by_ga[item["ga"]] = item

    results = []
    for dep in limited:
        ga = f"{dep['groupId']}:{dep['artifactId']}"
        api = by_ga.get(ga)
        if not api:
            hint = dependency_java_hint(dep, target, None)
            results.append({
                "ga": ga,
                "currentVersion": dep.get("version") or "(not explicit)",
                "latestVersion": "(not found)",
                "upgradeAvailable": None,
                "compatibility": hint["note"],
                "severity": "medium",
                "source": "none",
                "note": "Lookup failed for this dependency.",
                "files": dep.get("files") or [],
                "transitiveCount": 0,
                "transitive": [],
                "checked": False,
            })
            continue
        hint = dependency_java_hint(dep, target, api.get("latestVersion"))
        current_known = is_static_version(dep.get("version"))
        latest = api.get("latestVersion")
        results.append({
            "ga": ga,
            "currentVersion": dep.get("version") or "(not explicit)",
            "latestVersion": latest or "(not found)",
            "upgradeAvailable": compare_versions(dep.get("version"), latest) < 0 if (current_known and latest) else None,
            "compatibility": hint["note"],
            "severity": hint["severity"] if api.get("status") == "ok" else "medium",
            "source": api.get("source") or "none",
            "note": api.get("note") or "",
            "files": dep.get("files") or [],
            "transitiveCount": int(api.get("transitiveCount") or 0),
            "transitive": api.get("transitive") or [],
            "checked": api.get("status") == "ok",
        })

    return {
        "totalDetected": len(deps),
        "checkedCount": len(results),
        "pageSize": DEP_CHECK_PAGE_SIZE,
        "pageCount": max(1, (len(results) + DEP_CHECK_PAGE_SIZE - 1) // DEP_CHECK_PAGE_SIZE),
        "transitiveFallbackUsed": False,
        "results": sorted(results, key=lambda r: r["ga"]),
    }


def parse_ga(ga):
    parts = str(ga or "").split(":")
    return {"groupId": parts[0] if len(parts) > 0 else "", "artifactId": parts[1] if len(parts) > 1 else ""}


def is_internal_candidate(dep, prefixes):
    ga = parse_ga(dep.get("ga"))
    configured = any(ga["groupId"].lower().startswith(p) or ga["artifactId"].lower().startswith(p) for p in prefixes)
    if configured:
        return True
    group_looks_internal = ga["groupId"] and "." not in ga["groupId"]
    version_looks_internal = "SNAPSHOT" in str(dep.get("currentVersion") or "").upper()
    unresolved = not (dep.get("checked") and dep.get("source") and dep.get("source") != "none")
    return group_looks_internal or version_looks_internal or unresolved


def compute_repo_coverage(dep_report, prefixes):
    all_deps = (dep_report or {}).get("results") or []
    internal = [d for d in all_deps if is_internal_candidate(d, prefixes)]
    resolved = [d for d in internal if d.get("checked") and d.get("source") and d.get("source") != "none"]
    unresolved = [d for d in internal if d not in resolved]
    pct = round((len(resolved) * 100) / len(internal)) if internal else 100
    return {
        "totalInternal": len(internal),
        "resolvedInternal": len(resolved),
        "unresolvedInternal": len(unresolved),
        "coveragePercent": pct,
        "unresolvedList": unresolved,
    }


def project_roots_from_summary(summary):
    roots = sorted(set(parent_dir(p) for p in (summary.get("buildFiles") or []) if parent_dir(p)), key=len, reverse=True)
    return roots


def parent_dir(path):
    n = normalize_path(path)
    i = n.rfind("/")
    return n[:i] if i >= 0 else ""


def project_from_file(path, roots):
    normalized = normalize_path(path)
    if not normalized:
        return "(unknown)"
    if normalized == "pasted-input":
        return "(pasted-input)"
    for root in roots:
        if normalized == root or normalized.startswith(root + "/"):
            return root
    parts = normalized.split("/")
    if len(parts) >= 2:
        return f"{parts[0]}/{parts[1]}"
    return parts[0]


def projects_for_files(files, summary):
    roots = project_roots_from_summary(summary)
    return sorted(set(project_from_file(f, roots) for f in (files or [])))


def group_items_by_project(items, summary):
    roots = project_roots_from_summary(summary)
    groups = {}
    for item in items or []:
        for project in sorted(set(project_from_file(f, roots) for f in (item.get("files") or []))):
            groups.setdefault(project, []).append(item)
    return sorted(groups.items(), key=lambda pair: pair[0])


def build_rewrite_suggestions(findings, dep_report):
    out = {}
    def add(recipe, why):
        out.setdefault(recipe, why)
    for f in findings or []:
        title = str(f.get("title") or "")
        if "Legacy javax imports" in title:
            add("org.openrewrite.java.migrate.jakarta.JavaxMigrationToJakarta", "javax -> jakarta migration")
        if "Spring Boot 2.x" in title:
            add("org.openrewrite.java.spring.boot3.UpgradeSpringBoot_3_2", "Boot 2.x -> 3.x migration path")
        if "finalize()" in title:
            add("org.openrewrite.java.migrate.lang.MigrateClassNewInstanceToGetDeclaredConstructorNewInstance", "cleanup deprecated Java patterns")
        if "Internal JDK API" in title:
            add("org.openrewrite.java.migrate.UpgradeToJava17", "replace internal JDK usage and modernize code")
    for d in (dep_report or {}).get("results") or []:
        if str(d.get("ga") or "").startswith("org.springframework.boot:") and str(d.get("currentVersion") or "").startswith("2."):
            add("org.openrewrite.java.spring.boot3.UpgradeSpringBoot_3_2", "dependency report indicates Boot 2.x")
    return [{"recipe": k, "why": v} for k, v in out.items()]


def with_project_tags(report):
    def decorate(items):
        return [{**i, "projects": projects_for_files(i.get("files") or [], report["summary"])} for i in (items or [])]

    dep_results = decorate(((report.get("dependencyChecks") or {}).get("results")) or [])
    findings = decorate(report.get("findings") or [])
    unresolved = decorate(((report.get("repoCoverage") or {}).get("unresolvedList")) or [])
    finding_by_project = [{"project": project, "count": len(items)} for project, items in group_items_by_project(findings, report["summary"])]
    dep_by_project = [{"project": project, "count": len(items), "dependencies": [i.get("ga") for i in items]} for project, items in group_items_by_project(dep_results, report["summary"])]
    unresolved_by_project = [{"project": project, "count": len(items)} for project, items in group_items_by_project(unresolved, report["summary"])]
    out = dict(report)
    out["findings"] = findings
    if out.get("dependencyChecks"):
        out["dependencyChecks"] = dict(out["dependencyChecks"])
        out["dependencyChecks"]["results"] = dep_results
    if out.get("repoCoverage"):
        out["repoCoverage"] = dict(out["repoCoverage"])
        out["repoCoverage"]["unresolvedList"] = unresolved
    out["projectBreakdown"] = {
        "projectRoots": project_roots_from_summary(report["summary"]),
        "findingCountByProject": finding_by_project,
        "dependencyCountByProject": dep_by_project,
        "unresolvedInternalByProject": unresolved_by_project,
    }
    return out


def to_text_report(report):
    lines = [
        "Java Upgrade Report",
        f"Target Java: {report['target']}",
        f"Scanned files/input blocks: {report['scannedFileCount']}",
        f"Scanned Java files: {report['summary']['javaCount']}",
        f"Scanned build files: {report['summary']['buildCount']}",
        f"Scanned config files: {report['summary']['configCount']}",
        f"Build files: {', '.join(report['summary'].get('buildFiles') or []) or 'none detected'}",
        f"Internal prefixes: {', '.join(report.get('internalPrefixes') or []) or '(heuristic only)'}",
        f"Generated at: {datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')}",
        "",
        "Findings:",
    ]
    if not report.get("findings"):
        lines.append("- No obvious migration blockers found.")
    else:
        for f in report["findings"]:
            lines.append(f"- [{str(f.get('severity', '')).upper()}] {f.get('title', '')}")
            lines.append(f"  Why: {f.get('why', '')}")
            lines.append(f"  Action: {f.get('action', '')}")
            if f.get("files"):
                lines.append(f"  Files: {', '.join(f['files'])}")
            if f.get("evidence"):
                lines.append(f"  Evidence: {', '.join(f['evidence'])}")
    lines.append("")
    lines.append("Upgrade plan:")
    for step in report.get("plan") or []:
        lines.append(f"- {step}")
    return "\n".join(lines) + "\n"


def to_csv_report(report):
    rows = [[
        "kind", "severity", "title_or_ga", "currentVersion", "latestVersion",
        "upgradeAvailable", "compatibility_or_why", "action_or_note", "source",
        "transitive_1hop", "files", "evidence", "projects",
    ]]
    for f in report.get("findings") or []:
        rows.append([
            "finding", f.get("severity", ""), f.get("title", ""), "", "", "", f.get("why", ""), f.get("action", ""),
            "", "", "; ".join(f.get("files") or []), "; ".join(f.get("evidence") or []), "; ".join(f.get("projects") or []),
        ])
    for p in report.get("plan") or []:
        rows.append(["plan", "", p, "", "", "", "", "", "", "", "", "", ""])
    for d in ((report.get("dependencyChecks") or {}).get("results") or []):
        rows.append([
            "dependency", d.get("severity", ""), d.get("ga", ""), d.get("currentVersion", ""), d.get("latestVersion", ""),
            "" if d.get("upgradeAvailable") is None else str(d.get("upgradeAvailable")),
            d.get("compatibility", ""), d.get("note", ""), d.get("source", ""), d.get("transitiveCount", ""),
            "; ".join(d.get("files") or []), "", "; ".join(d.get("projects") or []),
        ])
    from io import StringIO
    buff = StringIO()
    writer = csv.writer(buff)
    writer.writerows(rows)
    return buff.getvalue()


def to_html_report(report):
    findings_html = "".join(
        f"<li><strong>{html.escape(str(f.get('severity', '')).upper())}</strong> {html.escape(str(f.get('title', '')))}"
        f"<div>{html.escape(str(f.get('why', '')))}</div><div>{html.escape(str(f.get('action', '')))}</div></li>"
        for f in report.get("findings") or []
    ) or "<li>No findings</li>"
    return f"""<!doctype html>
<html><head><meta charset="utf-8"/><title>Java Upgrade Report</title></head>
<body>
<h1>Java Upgrade Report</h1>
<p>Target Java: <strong>{html.escape(str(report.get("target")))}</strong></p>
<p>Scanned: {report.get("scannedFileCount", 0)} blocks</p>
<h2>Findings</h2><ul>{findings_html}</ul>
<h2>Upgrade plan</h2><ol>{''.join(f"<li>{html.escape(str(p))}</li>" for p in (report.get("plan") or []))}</ol>
</body></html>
"""


def write_report(report, fmt, out_path):
    out = Path(out_path)
    stamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z").replace(":", "-")
    if fmt == "json":
        out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        return [out]
    if fmt == "txt":
        out.write_text(to_text_report(report), encoding="utf-8")
        return [out]
    if fmt == "csv":
        out.write_text(to_csv_report(report), encoding="utf-8")
        return [out]
    if fmt == "html":
        out.write_text(to_html_report(report), encoding="utf-8")
        return [out]
    base = out.with_suffix("")
    outputs = [
        base.with_name(f"{base.name}-{stamp}.json"),
        base.with_name(f"{base.name}-{stamp}.txt"),
        base.with_name(f"{base.name}-{stamp}.csv"),
        base.with_name(f"{base.name}-{stamp}.html"),
    ]
    outputs[0].write_text(json.dumps(report, indent=2), encoding="utf-8")
    outputs[1].write_text(to_text_report(report), encoding="utf-8")
    outputs[2].write_text(to_csv_report(report), encoding="utf-8")
    outputs[3].write_text(to_html_report(report), encoding="utf-8")
    return outputs


def build_report(path, target, include_transitive, skip_deps, internal_prefixes):
    entries = collect_entries(path)
    if not entries:
        raise ValueError("No analyzable files found (.java, pom.xml, build.gradle, application.*).")
    analysis = analyze_entries(entries, target)
    summary = summarize_entries(entries)
    if skip_deps:
        dep_checks = {"totalDetected": 0, "checkedCount": 0, "pageSize": DEP_CHECK_PAGE_SIZE, "pageCount": 1, "results": []}
    else:
        dep_checks = run_dependency_checks(entries, target, include_transitive)
    report = {
        "target": target,
        "scannedFileCount": len(entries),
        "summary": summary,
        "findings": analysis["findings"],
        "dependencyChecks": dep_checks,
        "repoCoverage": compute_repo_coverage(dep_checks, internal_prefixes),
        "plan": plan_from_analysis(analysis["findings"], target, analysis["context"]),
        "context": analysis["context"],
        "internalPrefixes": internal_prefixes,
        "rewriteSuggestions": build_rewrite_suggestions(analysis["findings"], dep_checks),
    }
    return with_project_tags(report)


def parse_args():
    p = argparse.ArgumentParser(description="Headless Java upgrade scan (no browser UI).")
    p.add_argument("--path", required=True, help="Project/repository path to scan")
    p.add_argument("--target", type=int, default=17, choices=[11, 17, 21, 25], help="Target Java version")
    p.add_argument("--format", default="json", choices=["json", "txt", "csv", "html", "all"], help="Output format")
    p.add_argument("--out", default="java-upgrade-report.json", help="Output file path (or prefix when --format all)")
    p.add_argument("--include-transitive", action="store_true", help="Enable 1-hop transitive dependency checks")
    p.add_argument("--skip-deps", action="store_true", help="Skip dependency checks (faster, offline-friendly)")
    p.add_argument("--internal-prefixes", default="", help="Comma-separated internal artifact prefixes (e.g. c2b,com.myco)")
    return p.parse_args()


def main():
    args = parse_args()
    report = build_report(
        args.path,
        args.target,
        args.include_transitive,
        args.skip_deps,
        parse_prefixes(args.internal_prefixes),
    )
    outputs = write_report(report, args.format, args.out)
    for path in outputs:
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())

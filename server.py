#!/usr/bin/env python3
import base64
import json
import logging
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree


DEFAULT_REPOS = ["https://repo1.maven.org/maven2"]
REPO_ENV = "INTERNAL_MAVEN_REPOS"
AUTH_HEADER_ENV = "INTERNAL_MAVEN_AUTH_HEADER"
AUTH_USER_ENV = "INTERNAL_MAVEN_USERNAME"
AUTH_PASS_ENV = "INTERNAL_MAVEN_PASSWORD"
LOOKUP_TIMEOUT_SECONDS = 2
LOOKUP_LIMIT = 200
MAX_WORKERS = 10
TRANSITIVE_PER_DEP_LIMIT = 40
LOG_LEVEL_ENV = "LOG_LEVEL"


LOGGER = logging.getLogger("java-upgrade-helper")


def non_snapshot(versions):
    return [v for v in versions if "SNAPSHOT" not in v.upper()]


def best_version(versions):
    stable = non_snapshot(versions)
    if stable:
        return stable[-1]
    return versions[-1] if versions else None


def parse_metadata(xml_bytes):
    root = ElementTree.fromstring(xml_bytes)
    versioning = root.find("versioning")
    if versioning is None:
        return None
    release = versioning.findtext("release")
    if release:
        return release.strip()
    versions_node = versioning.find("versions")
    if versions_node is None:
        return None
    versions = [v.text.strip() for v in versions_node.findall("version") if v.text and v.text.strip()]
    return best_version(versions)


def normalize_repo(url):
    return str(url).rstrip("/")


def normalize_path(path):
    parts = str(path or "").replace("\\", "/").split("/")
    out = []
    for p in parts:
        if not p or p == ".":
            continue
        if p == "..":
            if out:
                out.pop()
            continue
        out.append(p)
    return "/".join(out)


def dirname(path):
    n = normalize_path(path)
    idx = n.rfind("/")
    return n[:idx] if idx >= 0 else ""


def join_path(base, rel):
    if not base:
        return normalize_path(rel)
    return normalize_path(f"{base}/{rel}")


def build_metadata_url(repo, group_id, artifact_id):
    group_path = group_id.replace(".", "/")
    return f"{normalize_repo(repo)}/{group_path}/{artifact_id}/maven-metadata.xml"


def build_pom_url(repo, group_id, artifact_id, version):
    group_path = group_id.replace(".", "/")
    repo_n = normalize_repo(repo)
    return f"{repo_n}/{group_path}/{artifact_id}/{version}/{artifact_id}-{version}.pom"


def build_request_headers(repo, internal_repos):
    headers = {"User-Agent": "java-upgrade-helper/1.0"}
    normalized_repo = normalize_repo(repo)
    normalized_internal = {normalize_repo(r) for r in internal_repos}
    if normalized_repo not in normalized_internal:
        return headers

    # Explicit auth header has top priority (e.g., Bearer token). If absent, fallback to Basic auth.
    explicit_header = os.getenv(AUTH_HEADER_ENV, "").strip()
    if explicit_header:
        headers["Authorization"] = explicit_header
        return headers

    user = os.getenv(AUTH_USER_ENV, "").strip()
    pwd = os.getenv(AUTH_PASS_ENV, "").strip()
    if user and pwd:
        token = base64.b64encode(f"{user}:{pwd}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {token}"
    return headers


def fetch_text(url, headers):
    req = Request(url, headers=headers)
    with urlopen(req, timeout=LOOKUP_TIMEOUT_SECONDS) as resp:
        if resp.status != 200:
            return None
        return resp.read().decode("utf-8", errors="replace")


def resolve_latest(group_id, artifact_id, repos, internal_repos):
    for repo in repos:
        meta_url = build_metadata_url(repo, group_id, artifact_id)
        try:
            req = Request(meta_url, headers=build_request_headers(repo, internal_repos))
            with urlopen(req, timeout=LOOKUP_TIMEOUT_SECONDS) as resp:
                if resp.status != 200:
                    continue
                latest = parse_metadata(resp.read())
                if latest:
                    return {"status": "ok", "latestVersion": latest, "source": repo}
        except Exception:
            continue
    return {"status": "unresolved", "latestVersion": None, "source": None, "note": "Not found in configured repositories."}


def lookup_one(dep, repos, internal_repos, include_transitive, per_dep_limit):
    group_id = str(dep.get("groupId") or "").strip()
    artifact_id = str(dep.get("artifactId") or "").strip()
    current_version = str(dep.get("version") or "").strip() or None
    if not group_id or not artifact_id:
        return None
    ga = f"{group_id}:{artifact_id}"
    lookup = resolve_latest(group_id, artifact_id, repos, internal_repos)
    out = {"ga": ga, **lookup}
    if include_transitive:
        version_for_pom = current_version or lookup.get("latestVersion")
        transitive = resolve_one_hop_transitive(
            group_id,
            artifact_id,
            version_for_pom,
            repos,
            internal_repos,
            {},
            per_dep_limit,
        )
        out["transitive"] = transitive
        out["transitiveCount"] = len(transitive)
    return out


def strip_ns(tag):
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def child_text(node, tag):
    for c in list(node):
        if strip_ns(c.tag) == tag:
            return (c.text or "").strip() or None
    return None


def parse_pom_xml(name, xml_text):
    root = ElementTree.fromstring(xml_text)
    if strip_ns(root.tag) != "project":
        return None
    props = {}
    parent = None
    dependencies = []
    managed = []

    for ch in list(root):
        tag = strip_ns(ch.tag)
        if tag == "properties":
            for p in list(ch):
                props[strip_ns(p.tag)] = (p.text or "").strip()
        elif tag == "parent":
            parent = {
                "groupId": child_text(ch, "groupId"),
                "artifactId": child_text(ch, "artifactId"),
                "version": child_text(ch, "version"),
                "relativePath": child_text(ch, "relativePath") or "../pom.xml",
            }
        elif tag == "dependencies":
            for d in list(ch):
                if strip_ns(d.tag) != "dependency":
                    continue
                dependencies.append(
                    {
                        "groupId": child_text(d, "groupId"),
                        "artifactId": child_text(d, "artifactId"),
                        "version": child_text(d, "version"),
                        "scope": child_text(d, "scope"),
                        "type": child_text(d, "type"),
                    }
                )
        elif tag == "dependencyManagement":
            deps_node = None
            for dm_ch in list(ch):
                if strip_ns(dm_ch.tag) == "dependencies":
                    deps_node = dm_ch
                    break
            if deps_node is not None:
                for d in list(deps_node):
                    if strip_ns(d.tag) != "dependency":
                        continue
                    managed.append(
                        {
                            "groupId": child_text(d, "groupId"),
                            "artifactId": child_text(d, "artifactId"),
                            "version": child_text(d, "version"),
                            "scope": child_text(d, "scope"),
                            "type": child_text(d, "type"),
                        }
                    )

    model = {
        "name": normalize_path(name),
        "groupId": child_text(root, "groupId"),
        "artifactId": child_text(root, "artifactId"),
        "version": child_text(root, "version"),
        "parent": parent,
        "properties": props,
        "dependencies": dependencies,
        "managedDependencies": managed,
    }
    return model


def interpolate(value, props, depth=0):
    if value is None:
        return None
    s = str(value)
    if depth > 8:
        return s
    out = s
    start = out.find("${")
    while start >= 0:
        end = out.find("}", start + 2)
        if end < 0:
            break
        key = out[start + 2 : end]
        repl = props.get(key, f"${{{key}}}")
        out = out[:start] + str(repl) + out[end + 1 :]
        start = out.find("${")
    if out == s:
        return out.strip()
    return interpolate(out, props, depth + 1)


def parse_gradle_dependencies(entries):
    import re

    dep_re = re.compile(
        r"\b(?:implementation|api|compileOnly|runtimeOnly|testImplementation|annotationProcessor)\s*\(?\s*[\"']([^:\"']+):([^:\"']+):([^\"']+)[\"']\s*\)?"
    )
    result = []
    for e in entries:
        name = str(e.get("name") or "")
        content = str(e.get("content") or "")
        for m in dep_re.finditer(content):
            result.append(
                {
                    "groupId": m.group(1).strip(),
                    "artifactId": m.group(2).strip(),
                    "version": m.group(3).strip(),
                    "files": [normalize_path(name)],
                }
            )
    return result


def fetch_remote_pom(group_id, artifact_id, version, repos, internal_repos, remote_cache):
    key = f"{group_id}:{artifact_id}:{version}"
    if key in remote_cache:
        return remote_cache[key]
    for repo in repos:
        url = build_pom_url(repo, group_id, artifact_id, version)
        try:
            text = fetch_text(url, build_request_headers(repo, internal_repos))
            if not text:
                continue
            model = parse_pom_xml(f"remote:{key}", text)
            if model:
                model["remoteSource"] = repo
                remote_cache[key] = model
                return model
        except Exception:
            continue
    remote_cache[key] = None
    return None


def resolve_one_hop_transitive(group_id, artifact_id, version, repos, internal_repos, remote_cache, per_dep_limit):
    if not version:
        return []
    model = fetch_remote_pom(group_id, artifact_id, version, repos, internal_repos, remote_cache)
    if not model:
        return []
    props = dict(model.get("properties") or {})
    props["project.groupId"] = model.get("groupId") or ""
    props["project.artifactId"] = model.get("artifactId") or ""
    props["project.version"] = model.get("version") or ""
    managed = {}
    for d in model.get("managedDependencies") or []:
        g = interpolate(d.get("groupId"), props)
        a = interpolate(d.get("artifactId"), props)
        v = interpolate(d.get("version"), props)
        if g and a and v:
            managed[f"{g}:{a}"] = v

    out = []
    seen = set()
    for d in model.get("dependencies") or []:
        scope = (interpolate(d.get("scope") or "", props) or "").lower()
        if scope in {"test", "provided"}:
            continue
        g = interpolate(d.get("groupId"), props)
        a = interpolate(d.get("artifactId"), props)
        if not g or not a:
            continue
        v = interpolate(d.get("version"), props) or managed.get(f"{g}:{a}") or ""
        key = f"{g}:{a}:{v}"
        if key in seen:
            continue
        seen.add(key)
        out.append({"ga": f"{g}:{a}", "version": v})
        if len(out) >= per_dep_limit:
            break
    return out


def resolve_maven_dependencies(local_poms, repos, internal_repos):
    local_models = [parse_pom_xml(p["name"], p["content"]) for p in local_poms]
    local_models = [m for m in local_models if m is not None]
    by_path = {normalize_path(m["name"]): m for m in local_models}
    by_gav = {}
    by_artifact = {}
    for m in local_models:
        g = m.get("groupId") or (m.get("parent") or {}).get("groupId")
        v = m.get("version") or (m.get("parent") or {}).get("version")
        a = m.get("artifactId")
        if g and a and v:
            by_gav[f"{g}:{a}:{v}"] = m
        if a:
            by_artifact[a] = m

    remote_cache = {}
    memo = {}
    in_progress = set()

    def resolve_model(model):
        key = normalize_path(model["name"])
        if key in memo:
            return memo[key]
        if key in in_progress:
            return {"groupId": None, "version": None, "properties": {}, "managed": {}, "dependencies": []}
        in_progress.add(key)

        parent_eff = None
        parent = model.get("parent")
        if parent and parent.get("groupId") and parent.get("artifactId"):
            gav_key = f"{parent.get('groupId')}:{parent.get('artifactId')}:{parent.get('version') or ''}"
            if parent.get("version") and gav_key in by_gav:
                parent_eff = resolve_model(by_gav[gav_key])
            else:
                rel = parent.get("relativePath") or "../pom.xml"
                rel_path = join_path(dirname(model["name"]), rel)
                if rel_path in by_path:
                    parent_eff = resolve_model(by_path[rel_path])
                elif parent.get("version"):
                    # Last resort for parent resolution: fetch parent POM from configured repositories.
                    remote_parent = fetch_remote_pom(
                        parent["groupId"], parent["artifactId"], parent["version"], repos, internal_repos, remote_cache
                    )
                    if remote_parent:
                        parent_eff = resolve_model(remote_parent)

        props = {}
        if parent_eff:
            props.update(parent_eff["properties"])
        for k, v in (model.get("properties") or {}).items():
            props[k] = interpolate(v, props)

        eff_group = interpolate(
            model.get("groupId") or (parent_eff["groupId"] if parent_eff else None) or (parent or {}).get("groupId"),
            props,
        )
        eff_version = interpolate(
            model.get("version") or (parent_eff["version"] if parent_eff else None) or (parent or {}).get("version"),
            props,
        )
        props["project.groupId"] = eff_group or ""
        props["pom.groupId"] = eff_group or ""
        props["project.version"] = eff_version or ""
        props["pom.version"] = eff_version or ""
        props["project.artifactId"] = model.get("artifactId") or ""
        props["pom.artifactId"] = model.get("artifactId") or ""
        if parent_eff:
            props["project.parent.groupId"] = parent_eff.get("groupId") or ""
            props["project.parent.version"] = parent_eff.get("version") or ""

        managed = {}
        if parent_eff:
            managed.update(parent_eff["managed"])

        for d in model.get("managedDependencies") or []:
            g = interpolate(d.get("groupId"), props)
            a = interpolate(d.get("artifactId"), props)
            v = interpolate(d.get("version"), props)
            scope = interpolate(d.get("scope") or "", props)
            dep_type = interpolate(d.get("type") or "jar", props)
            if not g or not a:
                continue
            if scope == "import" and dep_type == "pom" and v:
                local_bom = by_gav.get(f"{g}:{a}:{v}")
                if local_bom:
                    bom_eff = resolve_model(local_bom)
                    managed.update(bom_eff["managed"])
                else:
                    # Support BOM imports that are only available in remote/internal repositories.
                    remote_bom = fetch_remote_pom(g, a, v, repos, internal_repos, remote_cache)
                    if remote_bom:
                        bom_eff = resolve_model(remote_bom)
                        managed.update(bom_eff["managed"])
            elif v:
                managed[f"{g}:{a}"] = v

        deps = []
        for d in model.get("dependencies") or []:
            g = interpolate(d.get("groupId"), props)
            a = interpolate(d.get("artifactId"), props)
            explicit = interpolate(d.get("version"), props)
            if not g or not a:
                continue
            version = explicit or managed.get(f"{g}:{a}")
            deps.append({"groupId": g, "artifactId": a, "version": version})

        eff = {"groupId": eff_group, "version": eff_version, "properties": props, "managed": managed, "dependencies": deps}
        memo[key] = eff
        in_progress.discard(key)
        return eff

    dep_map = {}
    for m in local_models:
        eff = resolve_model(m)
        for d in eff["dependencies"]:
            ga = f"{d['groupId']}:{d['artifactId']}"
            if ga not in dep_map:
                dep_map[ga] = {"groupId": d["groupId"], "artifactId": d["artifactId"], "version": d.get("version"), "files": set()}
            if d.get("version") and (not dep_map[ga]["version"] or "${" in str(dep_map[ga]["version"])):
                dep_map[ga]["version"] = d["version"]
            dep_map[ga]["files"].add(normalize_path(m["name"]))

    out = []
    for dep in dep_map.values():
        out.append(
            {
                "groupId": dep["groupId"],
                "artifactId": dep["artifactId"],
                "version": dep["version"],
                "files": sorted(dep["files"]),
            }
        )
    return out


class Handler(SimpleHTTPRequestHandler):
    def _rid(self):
        return uuid.uuid4().hex[:8]

    def log_message(self, format, *args):
        LOGGER.info("%s - %s", self.address_string(), format % args)

    def _send_json(self, payload, status=200):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self):
        content_len = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(content_len)
        return json.loads(raw.decode("utf-8"))

    def _repos(self):
        extra_repos = [r.strip() for r in os.getenv(REPO_ENV, "").split(",") if r.strip()]
        return extra_repos + DEFAULT_REPOS, extra_repos

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path not in {"/api/dependency-check", "/api/maven-resolve"}:
            self._send_json({"error": "Not found"}, status=404)
            return
        try:
            payload = self._read_json()
        except Exception:
            self._send_json({"error": "Invalid JSON"}, status=400)
            return

        if parsed.path == "/api/dependency-check":
            self.handle_dependency_check(payload)
            return
        self.handle_maven_resolve(payload)

    def handle_dependency_check(self, payload):
        rid = self._rid()
        started = time.perf_counter()
        deps = payload.get("dependencies") or []
        if not isinstance(deps, list):
            self._send_json({"error": "dependencies must be an array"}, status=400)
            LOGGER.warning("[rid=%s] dependency-check invalid payload", rid)
            return
        include_transitive = bool(payload.get("includeTransitive"))
        try:
            per_dep_limit = int(payload.get("transitivePerDepLimit") or TRANSITIVE_PER_DEP_LIMIT)
        except Exception:
            per_dep_limit = TRANSITIVE_PER_DEP_LIMIT
        per_dep_limit = max(0, min(per_dep_limit, 100))

        repos, extra_repos = self._repos()
        limited = deps[:LOOKUP_LIMIT]
        LOGGER.info(
            "[rid=%s] dependency-check start deps=%d limited=%d includeTransitive=%s transitivePerDepLimit=%d repos=%d internalRepos=%d",
            rid,
            len(deps),
            len(limited),
            include_transitive,
            per_dep_limit,
            len(repos),
            len(extra_repos),
        )
        results = []
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = [pool.submit(lookup_one, dep, repos, extra_repos, include_transitive, per_dep_limit) for dep in limited]
            for fut in as_completed(futures):
                try:
                    item = fut.result()
                    if item is not None:
                        results.append(item)
                except Exception as err:
                    LOGGER.warning("[rid=%s] dependency lookup worker failed: %s", rid, err)

        ok_count = sum(1 for r in results if r.get("status") == "ok")
        unresolved_count = sum(1 for r in results if r.get("status") != "ok")
        transitive_total = sum(int(r.get("transitiveCount") or 0) for r in results)
        duration_ms = int((time.perf_counter() - started) * 1000)
        LOGGER.info(
            "[rid=%s] dependency-check done checked=%d ok=%d unresolved=%d transitiveEdges=%d durationMs=%d",
            rid,
            len(results),
            ok_count,
            unresolved_count,
            transitive_total,
            duration_ms,
        )

        self._send_json(
            {
                "checked": len(results),
                "totalRequested": len(deps),
                "limited": len(deps) > LOOKUP_LIMIT,
                "repos": repos,
                "results": results,
            }
        )

    def handle_maven_resolve(self, payload):
        rid = self._rid()
        started = time.perf_counter()
        poms = payload.get("poms") or []
        gradles = payload.get("gradles") or []
        if not isinstance(poms, list) or not isinstance(gradles, list):
            self._send_json({"error": "poms and gradles must be arrays"}, status=400)
            LOGGER.warning("[rid=%s] maven-resolve invalid payload", rid)
            return

        repos, extra_repos = self._repos()
        LOGGER.info(
            "[rid=%s] maven-resolve start poms=%d gradles=%d repos=%d internalRepos=%d",
            rid,
            len(poms),
            len(gradles),
            len(repos),
            len(extra_repos),
        )
        try:
            pom_deps = resolve_maven_dependencies(poms, repos, extra_repos)
            gradle_deps = parse_gradle_dependencies(gradles)
        except Exception as err:
            LOGGER.exception("[rid=%s] maven-resolve failed: %s", rid, err)
            self._send_json({"error": f"maven resolve failed: {err}"}, status=500)
            return

        dep_map = {}
        for dep in pom_deps + gradle_deps:
            ga = f"{dep['groupId']}:{dep['artifactId']}"
            if ga not in dep_map:
                dep_map[ga] = {
                    "groupId": dep["groupId"],
                    "artifactId": dep["artifactId"],
                    "version": dep.get("version"),
                    "files": set(dep.get("files") or []),
                }
            else:
                dep_map[ga]["files"].update(dep.get("files") or [])
                if dep.get("version") and (not dep_map[ga]["version"] or "${" in str(dep_map[ga]["version"])):
                    dep_map[ga]["version"] = dep["version"]

        out = []
        for d in dep_map.values():
            out.append(
                {
                    "groupId": d["groupId"],
                    "artifactId": d["artifactId"],
                    "version": d["version"],
                    "files": sorted(d["files"]),
                }
            )

        duration_ms = int((time.perf_counter() - started) * 1000)
        LOGGER.info(
            "[rid=%s] maven-resolve done dependencies=%d durationMs=%d",
            rid,
            len(out),
            duration_ms,
        )
        self._send_json({"dependencies": out, "repos": repos, "count": len(out)})


def main():
    level_name = os.getenv(LOG_LEVEL_ENV, "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    port = int(os.getenv("PORT", "8765"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    LOGGER.info("Java Upgrade Helper server running on http://127.0.0.1:%d", port)
    LOGGER.info("Set INTERNAL_MAVEN_REPOS to comma-separated Maven repository base URLs for internal artifacts.")
    LOGGER.info("Optional auth: INTERNAL_MAVEN_AUTH_HEADER or INTERNAL_MAVEN_USERNAME + INTERNAL_MAVEN_PASSWORD")
    LOGGER.info("Logging level: %s (set %s=DEBUG for verbose request logs)", level_name, LOG_LEVEL_ENV)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOGGER.info("Shutdown requested (Ctrl+C). Stopping server...")
    finally:
        server.shutdown()
        server.server_close()
        LOGGER.info("Server stopped.")


if __name__ == "__main__":
    main()

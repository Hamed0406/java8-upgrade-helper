# Java 8 Upgrade Helper

Small browser tool to help developers migrate code from **Java 8** to newer Java versions (default guidance targets **Java 17 LTS**).

## Most important: how to use the tool

1. Open this folder in terminal.
2. Start a local web server:

```bash
cd /opt/java-upgreaer
python3 server.py
```

Or run in a container (works with Docker and Podman):

```bash
cd /opt/java-upgreaer
docker build -t java-upgrade-helper .
docker run --rm -p 8765:8765 java-upgrade-helper
```

```bash
cd /opt/java-upgreaer
podman build -t java-upgrade-helper .
podman run --rm -p 8765:8765 java-upgrade-helper
```

3. Open in browser: `http://127.0.0.1:8765/index.html`
4. Add code in any way:
   - paste Java code in the text box, or
   - upload one or more `.java` files, or
   - select a source folder (the app scans `.java` files inside it)
5. Choose target Java version (11/17/21/25).
6. Click **Analyze**.
7. Read:
   - **Findings**: migration risks/patterns found
   - **Upgrade plan**: ordered migration steps
   - **Dependency checks**: current vs latest versions from Maven Central + Java-target hints
   - **Repository coverage (internal artifacts)**: resolved vs unresolved internal dependencies
8. Optional:
   - **Expand all / Collapse all** to show full file and evidence lists
   - Switch findings **View** to **By project** to group findings by module/project path
   - Choose export **Profile**: `All`, `By project`, `High only`, `Unresolved deps only`, `Internal only`
   - **Export report** with selectable format: `JSON`, `TXT`, `CSV`, `HTML`, or `ALL`
   - **Prev deps / Next deps** to page through dependency results
   - Switch dependency **View** to **By project** to group results by module/project path
   - Toggle **1-hop transitive** dependency lookup
   - Set **Internal prefixes** (comma-separated) to improve internal artifact coverage detection
9. Review **OpenRewrite suggestions** section for ready-to-apply recipe IDs.
10. Check the scan summary line under Findings to confirm build files like `pom.xml` were included.

## Headless CLI scan (no browser)

Run scan directly from terminal:

```bash
cd /opt/java-upgreaer
python3 scan.py --path ./my-java-repo --target 17 --format json --out report.json
```

Useful flags:
- `--include-transitive` -> enable 1-hop transitive dependency lookup
- `--skip-deps` -> skip dependency checks (faster/offline)
- `--dep-resolve-timeout 45` -> cap dependency model resolution time; auto-fallback if exceeded
- `--internal-prefixes c2b,com.myco` -> improve internal artifact coverage
- `--format all` -> write JSON/TXT/CSV/HTML files

## What it currently checks

- Internal JDK API usage (`sun.*`, `com.sun.*`)
- Legacy `javax` imports often needing migration/dependencies
- `SecurityManager` usage
- `finalize()` usage
- Deep reflection (`setAccessible(true)`)
- Build/config migration signals (`pom.xml`, `build.gradle`, `application.*`)
- Spring/Spring Boot migration recommendations (including Boot 2.x -> 3.x path for Java 17+)
- Framework compatibility matrix finding (Java target vs Spring Boot/Spring/Hibernate/Jakarta)
- Java level pinned in build files (for example Java 8 still configured while targeting 17+)
- File names per finding so you know exactly what to upgrade
- Dependency checks from `pom.xml` / `build.gradle` (latest version lookup + compatibility hint)
- Optional 1-hop transitive dependency lookup (from dependency POM metadata)
- Colored severity tags (HIGH/MEDIUM/LOW) for findings and dependency checks
- HTML export includes visual severity tags and formatted sections (findings, plan, dependencies, coverage)
- CSV export includes findings, full dependency rows, plan rows, and unresolved coverage rows
- Maven model resolution via backend (parent chain, properties, dependencyManagement, local+remote BOM import)
- OpenRewrite recipe suggestions generated from detected findings/dependency patterns

## Notes

- Core analysis runs in browser; optional lightweight backend (`server.py`) is used for reliable dependency lookup.
- Uses regex heuristics for speed; not full AST parsing.
- Internal artifact coverage uses heuristics (group/artifact/version patterns) and may need tuning per company naming rules.
- Browser security does not allow typing any local absolute path directly; use the folder picker.
- For smart dependency lookup, run `python3 server.py` (it proxies lookups and avoids browser CORS issues).
- Optional internal repository support:
  - `INTERNAL_MAVEN_REPOS=https://nexus.company/repository/maven-public,https://artifactory.company/maven`
  - then run `python3 server.py`
- Optional internal repo auth:
  - `INTERNAL_MAVEN_AUTH_HEADER="Bearer <token>"` (recommended when available), or
  - `INTERNAL_MAVEN_USERNAME=<user>` and `INTERNAL_MAVEN_PASSWORD=<pass>`
- Dependency checks now run through all dependencies and show results in pages of 30 entries.

## Project docs

- `README.md` -> usage and run instructions
- `ARCHITECTURE.md` -> architecture diagram and component/data-flow overview
- `PROCESS.md` -> workflow/runbook for planning and executing upgrades
- `REPO_UPGRADE_GUIDE.md` -> how repo analysis works and what upgrade info it gives
- `Dockerfile` -> container image for Docker/Podman
- `FEATURES.md` -> feature status and backlog
- `PLAN.md` -> implementation roadmap

## Internal repo usage (copy/paste)

Use Maven Central + your internal repos:

```bash
cd /opt/java-upgreaer
INTERNAL_MAVEN_REPOS="https://nexus.company/repository/maven-public,https://artifactory.company/maven" \
python3 server.py
```

Use token auth for internal repos:

```bash
cd /opt/java-upgreaer
INTERNAL_MAVEN_REPOS="https://nexus.company/repository/maven-public" \
INTERNAL_MAVEN_AUTH_HEADER="Bearer <token>" \
python3 server.py
```

Enable verbose server logs (request timing/counts):

```bash
cd /opt/java-upgreaer
LOG_LEVEL=DEBUG python3 server.py
```

Use username/password auth for internal repos:

```bash
cd /opt/java-upgreaer
INTERNAL_MAVEN_REPOS="https://nexus.company/repository/maven-public" \
INTERNAL_MAVEN_USERNAME="<user>" \
INTERNAL_MAVEN_PASSWORD="<pass>" \
python3 server.py
```

## Test dependency lookup

```bash
cd /opt/java-upgreaer
python3 -m unittest -v test_dependency_api.py
python3 -m unittest -v test_server_auth.py
python3 -m unittest -v test_maven_resolve_api.py
python3 -m unittest -v test_scan_cli.py
```

## CI / CLI mode (pipeline gate)

Use the report JSON in CI and fail builds on thresholds:

```bash
python3 ci_gate.py java-upgrade-report-all-2026-07-14T15-00-00.000Z.json --max-high 0 --max-medium 5 --max-unresolved-deps 20
```

# Repository Upgrade Intelligence Guide

This guide explains how this tool reads a Java repository and what upgrade information it provides for Java migration planning.

## Why repository input is best

A full repository (not just pasted code) gives:

- Module/project boundaries (multi-module Maven/Gradle)
- Real build configuration (`pom.xml`, `build.gradle`, `application.*`)
- Better framework/version detection
- Better dependency + internal artifact coverage
- Stronger upgrade sequencing (what to do first)

## Analysis logic (high level)

The scanner combines static code checks and build-model checks:

1. **Collect analyzable files**
   - Java sources (`*.java`)
   - Build files (`pom.xml`, `build.gradle`, `build.gradle.kts`, `settings.gradle*`, `gradle.properties`)
   - Config files (`application.properties|yml|yaml`)

2. **Run migration rules on code/build text**
   - Internal JDK API usage (`sun.*`, `com.sun.*`)
   - `javax.*` usage likely needing Jakarta migration
   - `setAccessible(true)`, `SecurityManager`, `finalize()`
   - Temporary JVM flags (`--add-opens`, `--illegal-access`)

3. **Detect framework matrix**
   - Spring Boot/Spring/Hibernate/Jakarta signals
   - Matrix mismatch checks vs selected target Java
   - Version alignment guidance (for example Boot 2.x -> 3.x on Java 17+)

4. **Resolve dependencies**
   - Parses Maven/Gradle dependencies
   - Maven model resolution supports parent chain, properties, dependencyManagement, BOM import
   - Checks latest versions from configured repositories
   - Optional 1-hop transitive dependency lookup

5. **Compute internal artifact coverage**
   - Classifies internal candidates via prefixes + heuristics
   - Reports resolved/unresolved internal dependencies
   - Groups unresolved items by project

6. **Generate outputs**
   - Findings (severity-tagged)
   - Ordered upgrade plan
   - Dependency report + compatibility hints
   - OpenRewrite recipe suggestions
   - Export formats: JSON, TXT, CSV, HTML

## Information you get for upgrade planning

### 1) Risk map (what can break)

- High/Medium/Low findings with:
  - Why it matters
  - Action recommendation
  - Exact file list and evidence lines

### 2) Framework target alignment

- Framework compatibility matrix finding:
  - Boot/Spring/Hibernate/Jakarta detection summary
  - Mismatch warnings against your selected target Java

### 3) Dependency reality

- For each dependency:
  - Current version
  - Latest available version
  - Compatibility hint for selected target Java
  - Source repository used for resolution
  - Optional 1-hop transitive count/details

### 4) Internal repository readiness

- Coverage summary:
  - Internal resolved/total (%)
  - Unresolved internal dependencies
  - By-project breakdown for unresolved items

### 5) Execution plan draft

- Upgrade steps ordered by dependency/risk logic:
  - Baseline and freeze
  - Framework path (for example Boot staging path)
  - High/Medium remediation first
  - Java target progression and final cleanup

## Inputs that improve plan quality

For highest-quality planning, include:

- Root + all module build files
- Representative Java code per module
- Internal repo configuration (`INTERNAL_MAVEN_REPOS`, auth if needed)
- Correct target Java selection (11/17/21/25)
- Internal prefixes (example: `c2b,com.myco`)

## Limits (important)

- Uses fast heuristics/regex, not full AST semantic analysis
- Accuracy is highest when full repo and build files are present
- Runtime behavior, infra constraints, and performance still need environment-specific validation

## Quick run options

Browser mode:

```bash
python3 server.py
# open http://127.0.0.1:8765/index.html
```

Headless CLI mode:

```bash
python3 scan.py --path ./my-java-repo --target 17 --format json --out report.json
```


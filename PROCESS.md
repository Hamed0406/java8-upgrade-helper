# Java Upgrade Helper Process Docs

## Standard upgrade workflow

1. **Prepare input**
   - Prefer full repository (all modules/build files).
   - Set target Java (`11/17/21/25`).
   - Configure internal repos if needed (`INTERNAL_MAVEN_REPOS` + auth).

2. **Run analysis**
   - UI mode: `python3 server.py` then open `index.html`.
   - CLI mode: `python3 scan.py --path <repo> --target 17 --format json --out report.json`.

3. **Triage findings**
   - Fix **HIGH** first, then **MEDIUM**.
   - Review matrix mismatches (Boot/Spring/Hibernate/Jakarta vs target Java).
   - Group by project to assign owners.

4. **Dependency pass**
   - Review outdated dependencies and compatibility hints.
   - Use transitive mode when needed for deeper risk visibility.
   - Resolve internal artifact coverage gaps.

5. **Plan execution**
   - Follow generated upgrade plan ordering.
   - For Spring stacks on Java 17+: stage toward Boot 3/Spring 6/Jakarta.
   - Apply OpenRewrite suggestions where useful.

6. **Gate and repeat**
   - Export report and run CI gate:
     - `python3 ci_gate.py report.json --max-high 0 --max-medium 5 --max-unresolved-deps 20`
   - Re-run scans after each milestone until thresholds pass.

## Recommended operating model

- **Cadence**: run scan at least per PR for migration branches.
- **Ownership**: triage by project/module grouping.
- **Exit criteria**:
  - No blocking high findings.
  - Dependency and internal coverage acceptable for release.
  - Target Java and framework matrix aligned.

## Troubleshooting flow

1. Dependency API unavailable -> start `server.py`.
2. Many unresolved internal deps -> set internal repo URLs/auth + internal prefixes.
3. Large-repo slowdown -> use CLI + `--skip-deps` first, then full dependency pass.

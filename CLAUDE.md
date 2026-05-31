# Spring Boot Modernizer — MCP Agent

This project is an MCP server that equips a Claude agent with tools to modernise
Spring Boot services by updating Maven dependency versions and verifying compatibility.

## Setup

```powershell
# 1. Install dependencies
pip install -r requirements.txt

# 2. Register with Claude Desktop — copy mcp_config_example.json entries
#    into %APPDATA%\Claude\claude_desktop_config.json

# 3. Restart Claude Desktop
```

## MCP Tools (6 tools exposed)

| Tool | Purpose |
|------|---------|
| `read_pom` | Parse pom.xml → parent, properties, all deps with resolved versions |
| `get_latest_version` | Fetch latest stable release from Maven Central |
| `get_spring_boot_bom_versions` | Download Spring Boot BOM → managed version map |
| `check_compatibility` | Run compatibility checks on a proposed update set |
| `generate_update_plan` | Full analysis: latest versions + BOM + compat report + update plan |
| `apply_pom_updates` | Write approved version changes to pom.xml in-place |

## Typical Agent Workflow

1. **Discover** — call `read_pom` on the target pom.xml
2. **Research** — call `get_spring_boot_bom_versions` for the project's Spring Boot version
3. **Fetch versions** — call `get_latest_version` for each dependency with an explicit version
4. **Plan** — or skip steps 2-3 and call `generate_update_plan` directly (does all of the above)
5. **Check** — call `check_compatibility` on the proposed changes
6. **Apply** — call `apply_pom_updates` with the approved list

## Example Agent Prompt

```
Using the spring-boot-modernizer MCP server, modernise the Spring Boot service at
C:\projects\my-service\pom.xml.

Steps:
1. Read and summarise the current pom.xml (dependencies, versions, Spring Boot version).
2. Generate a full update plan — identify latest stable versions for all explicitly
   versioned dependencies.
3. Present the plan grouped as: safe updates / needs manual review / already up-to-date.
4. Run a compatibility report and highlight any breaking changes or BOM conflicts.
5. Ask me to confirm before applying. When I confirm, apply the safe updates only.
```

## Project Structure

```
modernization/
├── mcp_server/
│   ├── __init__.py
│   ├── server.py           ← MCP server & tool implementations
│   ├── maven_client.py     ← Maven Central API client
│   ├── pom_handler.py      ← POM parser & in-place updater
│   └── compatibility.py    ← Compatibility rules & analysis
├── requirements.txt
├── pyproject.toml
├── mcp_config_example.json ← Paste into claude_desktop_config.json
└── CLAUDE.md               ← This file
```

## Compatibility Checks Included

- Spring Boot ↔ Java version requirements (e.g. Spring Boot 3.x requires Java 17+)
- End-of-life Spring Boot version warnings (2.x, 3.0, 3.1)
- **Spring Boot 3.x migration** — `javax.*` → `jakarta.*` namespace change
- **Spring Security 6.x** — `WebSecurityConfigurerAdapter` removed, `antMatchers()` → `requestMatchers()`
- **Hibernate ORM 6.x** — Jakarta Persistence, SQL generation changes
- **Flyway 10.x** — database modules split out
- **QueryDSL 5.x** — jakarta-only
- BOM override risk (explicit version conflicts with Spring Boot BOM)
- Major version upgrade flagging (any X.y.z → (X+1).y.z)

## Notes

- The agent never applies changes without explicit confirmation.
- BOM-managed dependencies (no `<version>` tag) are skipped by default to avoid
  conflicting with Spring Boot's curated version set.
- Use `include_bom_managed: true` in `generate_update_plan` to see their current
  BOM-assigned versions for informational purposes.

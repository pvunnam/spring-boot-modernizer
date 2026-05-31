"""
Spring Boot Modernizer — MCP Server

Exposes tools that a Claude agent uses to:
  1. Parse a pom.xml and understand its dependency graph
  2. Fetch latest stable versions from Maven Central
  3. Load Spring Boot BOM-managed versions
  4. Run compatibility checks before applying changes
  5. Generate a full update plan
  6. Write approved version updates back to the pom.xml
"""

import asyncio
import json
import logging
import sys
from typing import Any

import mcp.server.stdio
import mcp.types as types
from mcp.server import Server, NotificationOptions
from mcp.server.models import InitializationOptions

from .maven_client import (
    fetch_spring_boot_bom,
    fetch_bom_managed_versions,
    get_latest_version,
    get_artifact_metadata,
)
from .pom_handler import apply_updates, parse_pom, pom_to_dict
from .compatibility import full_compatibility_report

logging.basicConfig(stream=sys.stderr, level=logging.INFO)
logger = logging.getLogger("spring-modernizer")

server = Server("spring-boot-modernizer")

# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="read_pom",
            description=(
                "Parse a Maven pom.xml file and return its full structure: "
                "parent info, Spring Boot version, properties, all dependencies "
                "(with resolved versions and whether they are BOM-managed), "
                "dependencyManagement entries, and build plugins."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Absolute or relative path to the pom.xml file.",
                    }
                },
                "required": ["file_path"],
            },
        ),
        types.Tool(
            name="get_latest_version",
            description=(
                "Query Maven Central for the latest stable release version of a Maven artifact. "
                "Returns latest_stable, latest_any (including pre-releases), and the top-10 recent versions. "
                "Use this to find upgrade candidates for each dependency."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "group_id": {
                        "type": "string",
                        "description": "Maven groupId, e.g. org.projectlombok",
                    },
                    "artifact_id": {
                        "type": "string",
                        "description": "Maven artifactId, e.g. lombok",
                    },
                    "use_metadata_xml": {
                        "type": "boolean",
                        "description": (
                            "If true, fetch maven-metadata.xml directly from repo1.maven.org "
                            "instead of the search API. More reliable for some artifacts."
                        ),
                        "default": False,
                    },
                },
                "required": ["group_id", "artifact_id"],
            },
        ),
        types.Tool(
            name="get_spring_boot_bom_versions",
            description=(
                "Download and parse the Spring Boot dependencies BOM POM for a given Spring Boot version. "
                "Returns the complete map of artifact coordinates to managed versions. "
                "Use this to determine which library versions Spring Boot already manages "
                "(so you can decide whether to override or remove explicit version tags)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "spring_boot_version": {
                        "type": "string",
                        "description": "The Spring Boot version to fetch the BOM for, e.g. 3.2.5",
                    },
                },
                "required": ["spring_boot_version"],
            },
        ),
        types.Tool(
            name="check_compatibility",
            description=(
                "Analyse a proposed set of version updates for compatibility issues. "
                "Checks: Spring Boot ↔ Java version requirements, known breaking changes "
                "(Spring Boot 3.x jakarta migration, Security 6, Hibernate 6, Flyway 10, etc.), "
                "library minimum Spring Boot version requirements, and BOM override risks. "
                "Returns errors (blockers) and warnings (review recommended) grouped by severity."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "proposed_updates": {
                        "type": "array",
                        "description": "List of proposed version changes.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "group_id": {"type": "string"},
                                "artifact_id": {"type": "string"},
                                "current_version": {"type": "string"},
                                "new_version": {"type": "string"},
                            },
                            "required": ["group_id", "artifact_id", "new_version"],
                        },
                    },
                    "spring_boot_version": {
                        "type": "string",
                        "description": "Current (or target) Spring Boot version of the project.",
                    },
                    "java_version": {
                        "type": "string",
                        "description": "Java version used by the project (e.g. '17').",
                    },
                    "bom_managed_versions": {
                        "type": "object",
                        "description": (
                            "Map of 'groupId:artifactId' → version from the Spring Boot BOM. "
                            "Obtained via get_spring_boot_bom_versions. Used to flag BOM overrides."
                        ),
                    },
                },
                "required": ["proposed_updates"],
            },
        ),
        types.Tool(
            name="generate_update_plan",
            description=(
                "Generate a comprehensive dependency update plan for a Spring Boot project. "
                "Reads the pom.xml, fetches latest versions for all non-BOM-managed dependencies, "
                "loads the Spring Boot BOM, runs compatibility checks, and produces a prioritised "
                "list of recommended updates with compatibility notes. "
                "Returns a ready-to-review plan — use apply_pom_updates to write changes."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path to the pom.xml to analyse.",
                    },
                    "include_bom_managed": {
                        "type": "boolean",
                        "description": (
                            "If true, also check versions for BOM-managed dependencies "
                            "and include them in the plan (they won't be updated, just reported). "
                            "Default: false."
                        ),
                        "default": False,
                    },
                    "skip_major_upgrades": {
                        "type": "boolean",
                        "description": "If true, exclude major version upgrades from the safe-to-apply list.",
                        "default": False,
                    },
                },
                "required": ["file_path"],
            },
        ),
        types.Tool(
            name="apply_pom_updates",
            description=(
                "Write approved version updates to a pom.xml file in-place, "
                "preserving original formatting, comments, and namespace declarations. "
                "Each update can target either a direct <version> tag or a <properties> entry "
                "(when the dependency uses a ${property.name} version reference). "
                "Returns a per-update success/failure report."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path to the pom.xml to update.",
                    },
                    "updates": {
                        "type": "array",
                        "description": "List of version updates to apply.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "group_id": {
                                    "type": "string",
                                    "description": "groupId of the dependency.",
                                },
                                "artifact_id": {
                                    "type": "string",
                                    "description": "artifactId of the dependency.",
                                },
                                "new_version": {
                                    "type": "string",
                                    "description": "New version to set.",
                                },
                                "property_key": {
                                    "type": "string",
                                    "description": (
                                        "If the dependency version is defined via a property, "
                                        "provide the property key (e.g. 'lombok.version'). "
                                        "The property value in <properties> will be updated instead."
                                    ),
                                },
                            },
                            "required": ["group_id", "artifact_id", "new_version"],
                        },
                    },
                },
                "required": ["file_path", "updates"],
            },
        ),
    ]


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

@server.call_tool()
async def handle_call_tool(
    name: str, arguments: dict[str, Any] | None
) -> list[types.TextContent]:
    args = arguments or {}
    try:
        result = await _dispatch(name, args)
    except Exception as exc:
        logger.exception("Tool %s raised an error", name)
        result = {"error": str(exc), "tool": name}

    return [types.TextContent(type="text", text=json.dumps(result, indent=2))]


async def _dispatch(name: str, args: dict) -> Any:
    loop = asyncio.get_event_loop()

    if name == "read_pom":
        return await loop.run_in_executor(None, _read_pom, args)

    if name == "get_latest_version":
        return await loop.run_in_executor(None, _get_latest_version, args)

    if name == "get_spring_boot_bom_versions":
        return await loop.run_in_executor(None, _get_spring_boot_bom_versions, args)

    if name == "check_compatibility":
        return await loop.run_in_executor(None, _check_compatibility, args)

    if name == "generate_update_plan":
        return await loop.run_in_executor(None, _generate_update_plan, args)

    if name == "apply_pom_updates":
        return await loop.run_in_executor(None, _apply_pom_updates, args)

    return {"error": f"Unknown tool: {name}"}


# ---------------------------------------------------------------------------
# Sync implementations (run in executor to keep the event loop free)
# ---------------------------------------------------------------------------

def _read_pom(args: dict) -> dict:
    file_path = args["file_path"]
    pom = parse_pom(file_path)
    return pom_to_dict(pom)


def _get_latest_version(args: dict) -> dict:
    group_id = args["group_id"]
    artifact_id = args["artifact_id"]
    use_metadata = args.get("use_metadata_xml", False)

    if use_metadata:
        return get_artifact_metadata(group_id, artifact_id)
    return get_latest_version(group_id, artifact_id)


def _get_spring_boot_bom_versions(args: dict) -> dict:
    return fetch_spring_boot_bom(args["spring_boot_version"])


def _check_compatibility(args: dict) -> dict:
    proposed = args["proposed_updates"]
    spring_boot_version = args.get("spring_boot_version")
    java_version = args.get("java_version")
    bom_managed = args.get("bom_managed_versions")
    return full_compatibility_report(proposed, spring_boot_version, java_version, bom_managed)


def _generate_update_plan(args: dict) -> dict:
    file_path = args["file_path"]
    include_bom_managed = args.get("include_bom_managed", False)
    skip_major = args.get("skip_major_upgrades", False)

    # 1. Parse POM
    try:
        pom = parse_pom(file_path)
    except (ValueError, FileNotFoundError) as e:
        return {"error": str(e)}

    pom_data = pom_to_dict(pom)

    # 2. Load Spring Boot BOM if applicable
    bom_managed: dict[str, str] = {}
    bom_result: dict = {}
    if pom.is_spring_boot_child and pom.spring_boot_version:
        bom_result = fetch_spring_boot_bom(pom.spring_boot_version)
        bom_managed = bom_result.get("managed_dependencies", {})

    # 3. Collect candidates — deps + plugins with explicit versions
    candidates = []
    all_deps = pom.dependencies + pom.plugins + pom.managed_dependencies

    for dep in all_deps:
        skip_bom = dep.managed_by_bom and not include_bom_managed
        if skip_bom:
            continue
        if dep.resolved_version is None:
            continue

        result = get_latest_version(dep.group_id, dep.artifact_id)
        latest_stable = result.get("latest_stable")

        entry = {
            "group_id": dep.group_id,
            "artifact_id": dep.artifact_id,
            "coordinates": dep.coordinates,
            "current_version": dep.resolved_version,
            "latest_stable": latest_stable,
            "property_key": dep.property_key,
            "is_plugin": dep.is_plugin,
            "managed_by_bom": dep.managed_by_bom,
            "in_dependency_management": dep.in_dependency_management,
            "scope": dep.scope,
        }

        if latest_stable and latest_stable != dep.resolved_version:
            entry["update_available"] = True
            entry["new_version"] = latest_stable

            try:
                from packaging.version import Version
                cur_major = Version(dep.resolved_version).major
                new_major = Version(latest_stable).major
                entry["is_major_upgrade"] = new_major > cur_major
            except Exception:
                entry["is_major_upgrade"] = False
        else:
            entry["update_available"] = False
            entry["is_major_upgrade"] = False

        candidates.append(entry)

    # 4. Build proposed list for compatibility check
    proposed_updates = [
        {
            "group_id": c["group_id"],
            "artifact_id": c["artifact_id"],
            "current_version": c["current_version"],
            "new_version": c.get("new_version", c["current_version"]),
        }
        for c in candidates
        if c.get("update_available")
    ]

    # 5. Run compatibility check
    compat = full_compatibility_report(
        proposed=proposed_updates,
        spring_boot_version=pom.spring_boot_version,
        java_version=pom.java_version,
        bom_managed=bom_managed if bom_managed else None,
    )

    # 6. Classify updates
    safe_updates = []
    review_updates = []
    skip_updates = []

    flagged_coords = {
        i["artifact"].split(":")[0] + ":" + (i["artifact"].split(":")[1] if ":" in i["artifact"] else "")
        for i in compat["errors"] + compat["warnings"]
    }

    for c in candidates:
        if not c.get("update_available"):
            continue
        if skip_major and c.get("is_major_upgrade"):
            skip_updates.append({**c, "skip_reason": "Major upgrade excluded by skip_major_upgrades flag"})
            continue

        coords = c["coordinates"]
        is_flagged = any(coords.startswith(f) or f.startswith(coords.split(":")[0]) for f in flagged_coords if f)

        if is_flagged or c.get("is_major_upgrade"):
            review_updates.append(c)
        else:
            safe_updates.append(c)

    # Build apply_pom_updates input for safe updates (convenience)
    ready_to_apply = [
        {
            "group_id": u["group_id"],
            "artifact_id": u["artifact_id"],
            "new_version": u["new_version"],
            **({"property_key": u["property_key"]} if u.get("property_key") else {}),
        }
        for u in safe_updates
        if not u.get("managed_by_bom")
    ]

    return {
        "file_path": file_path,
        "project_summary": {
            "spring_boot_version": pom.spring_boot_version,
            "java_version": pom.java_version,
            "is_spring_boot_child": pom.is_spring_boot_child,
            "total_dependencies": pom_data["summary"]["total_dependencies"],
            "bom_managed_count": pom_data["summary"]["bom_managed"],
            "explicit_version_count": pom_data["summary"]["explicit_version"],
        },
        "bom_info": {
            "bom_version": bom_result.get("bom_version"),
            "bom_managed_count": bom_result.get("managed_count", 0),
        },
        "update_summary": {
            "safe_to_apply": len(safe_updates),
            "needs_review": len(review_updates),
            "skipped": len(skip_updates),
            "already_up_to_date": sum(1 for c in candidates if not c.get("update_available")),
        },
        "compatibility": compat,
        "safe_updates": safe_updates,
        "review_required_updates": review_updates,
        "skipped_updates": skip_updates,
        "ready_to_apply_input": ready_to_apply,
        "note": (
            "Pass 'ready_to_apply_input' directly to apply_pom_updates to write safe updates. "
            "Review 'review_required_updates' manually before applying."
        ),
    }


def _apply_pom_updates(args: dict) -> dict:
    return apply_updates(args["file_path"], args["updates"])


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _main():
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="spring-boot-modernizer",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


def main():
    asyncio.run(_main())


if __name__ == "__main__":
    main()

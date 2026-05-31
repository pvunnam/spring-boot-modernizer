"""
POM XML parser and in-place updater.

Preserves original formatting, comments, and namespace declarations.
Handles:
  - Direct <version> tags on dependencies
  - Property-based versions via ${property.name}
  - Spring Boot parent / BOM inheritance (no-version deps)
  - Plugin versions inside <build><plugins>
  - dependencyManagement section
"""

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

POM_NS = "http://maven.apache.org/POM/4.0.0"
PROP_REF = re.compile(r"\$\{([^}]+)\}")


@dataclass
class Dependency:
    group_id: str
    artifact_id: str
    version: Optional[str]              # Raw value (may be a ${prop} reference)
    resolved_version: Optional[str]     # Version after resolving properties
    property_key: Optional[str]         # If version is ${key}, the key name
    scope: Optional[str]
    is_plugin: bool = False
    managed_by_bom: bool = False        # True when no <version> tag at all
    in_dependency_management: bool = False

    @property
    def coordinates(self) -> str:
        return f"{self.group_id}:{self.artifact_id}"


@dataclass
class ParsedPom:
    file_path: str
    raw_content: str
    parent_group_id: Optional[str]
    parent_artifact_id: Optional[str]
    parent_version: Optional[str]
    is_spring_boot_child: bool
    spring_boot_version: Optional[str]
    properties: dict[str, str]
    dependencies: list[Dependency]
    plugins: list[Dependency]
    managed_dependencies: list[Dependency]
    java_version: Optional[str]


def _strip_ns(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


def _find(elem: ET.Element, *path_parts: str) -> Optional[ET.Element]:
    """Namespace-aware find through chained child lookups."""
    cur = elem
    for part in path_parts:
        cur = cur.find(f"{{{POM_NS}}}{part}")
        if cur is None:
            return None
    return cur


def _findtext(elem: ET.Element, *path_parts: str) -> Optional[str]:
    found = _find(elem, *path_parts)
    return found.text.strip() if found is not None and found.text else None


def _resolve_properties(value: str, properties: dict[str, str]) -> str:
    def _replace(m: re.Match) -> str:
        key = m.group(1)
        return properties.get(key, m.group(0))
    return PROP_REF.sub(_replace, value)


def _parse_dependency_elem(
    elem: ET.Element,
    properties: dict[str, str],
    is_plugin: bool = False,
    in_dep_mgmt: bool = False,
) -> Dependency:
    group_id = _findtext(elem, "groupId") or ""
    artifact_id = _findtext(elem, "artifactId") or ""
    raw_version = _findtext(elem, "version")
    scope = _findtext(elem, "scope")

    property_key: Optional[str] = None
    resolved: Optional[str] = None

    if raw_version:
        m = PROP_REF.fullmatch(raw_version.strip())
        if m:
            property_key = m.group(1)
            resolved = properties.get(property_key)
        else:
            resolved = _resolve_properties(raw_version, properties)

    return Dependency(
        group_id=group_id,
        artifact_id=artifact_id,
        version=raw_version,
        resolved_version=resolved,
        property_key=property_key,
        scope=scope,
        is_plugin=is_plugin,
        managed_by_bom=(raw_version is None and not in_dep_mgmt),
        in_dependency_management=in_dep_mgmt,
    )


def parse_pom(file_path: str) -> ParsedPom:
    """Parse a pom.xml and return a structured representation."""
    path = Path(file_path)
    raw = path.read_text(encoding="utf-8")

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        raise ValueError(f"Invalid XML in {file_path}: {e}") from e

    # Parent block
    parent_group_id = _findtext(root, "parent", "groupId")
    parent_artifact_id = _findtext(root, "parent", "artifactId")
    parent_version = _findtext(root, "parent", "version")

    is_spring_boot_child = parent_artifact_id in (
        "spring-boot-starter-parent",
        "spring-boot-dependencies",
    )
    spring_boot_version = parent_version if is_spring_boot_child else None

    # Properties
    properties: dict[str, str] = {}
    props_elem = _find(root, "properties")
    if props_elem is not None:
        for prop in props_elem:
            tag = _strip_ns(prop.tag)
            if prop.text:
                properties[tag] = prop.text.strip()

    # Add parent version as a resolvable property
    if parent_version:
        properties["project.parent.version"] = parent_version

    java_version = properties.get("java.version") or properties.get("maven.compiler.source")

    # Regular dependencies
    deps: list[Dependency] = []
    deps_elem = _find(root, "dependencies")
    if deps_elem is not None:
        for dep in deps_elem.findall(f"{{{POM_NS}}}dependency"):
            deps.append(_parse_dependency_elem(dep, properties))

    # dependencyManagement
    managed_deps: list[Dependency] = []
    dep_mgmt = _find(root, "dependencyManagement", "dependencies")
    if dep_mgmt is not None:
        for dep in dep_mgmt.findall(f"{{{POM_NS}}}dependency"):
            managed_deps.append(_parse_dependency_elem(dep, properties, in_dep_mgmt=True))

    # Build plugins
    plugins: list[Dependency] = []
    plugins_elem = _find(root, "build", "plugins")
    if plugins_elem is not None:
        for plugin in plugins_elem.findall(f"{{{POM_NS}}}plugin"):
            plugins.append(_parse_dependency_elem(plugin, properties, is_plugin=True))

    return ParsedPom(
        file_path=str(path),
        raw_content=raw,
        parent_group_id=parent_group_id,
        parent_artifact_id=parent_artifact_id,
        parent_version=parent_version,
        is_spring_boot_child=is_spring_boot_child,
        spring_boot_version=spring_boot_version,
        properties=properties,
        dependencies=deps,
        plugins=plugins,
        managed_dependencies=managed_deps,
        java_version=java_version,
    )


def _replace_xml_version(content: str, group_id: str, artifact_id: str, new_version: str) -> str:
    """
    Replace the <version> tag for a specific dependency/plugin block in the raw XML.
    Uses a targeted regex so the rest of the file format is preserved.
    """
    # Build a pattern that matches the groupId+artifactId block and captures <version>
    ga_pattern = re.compile(
        r"(<groupId>\s*"
        + re.escape(group_id)
        + r"\s*</groupId>\s*<artifactId>\s*"
        + re.escape(artifact_id)
        + r"\s*</artifactId>(?:(?!<groupId>).)*?<version>\s*)"
        + r"([^<]+)"
        + r"(\s*</version>)",
        re.DOTALL,
    )
    new_content, count = ga_pattern.subn(
        lambda m: m.group(1) + new_version + m.group(3),
        content,
        count=1,
    )
    if count == 0:
        # Try reversed order (artifactId before groupId, as seen in some POMs)
        ga_pattern2 = re.compile(
            r"(<artifactId>\s*"
            + re.escape(artifact_id)
            + r"\s*</artifactId>\s*<groupId>\s*"
            + re.escape(group_id)
            + r"\s*</groupId>(?:(?!<artifactId>).)*?<version>\s*)"
            + r"([^<]+)"
            + r"(\s*</version>)",
            re.DOTALL,
        )
        new_content, count = ga_pattern2.subn(
            lambda m: m.group(1) + new_version + m.group(3),
            content,
            count=1,
        )
    return new_content


def _replace_property_value(content: str, property_key: str, new_value: str) -> str:
    """Replace a property value inside <properties> block."""
    tag = property_key.split(".")[-1] if "." not in property_key else property_key
    # Try exact tag match first
    pattern = re.compile(
        r"(<" + re.escape(property_key) + r"\s*>)" + r"([^<]+)" + r"(</" + re.escape(property_key) + r">)"
    )
    new_content, count = pattern.subn(
        lambda m: m.group(1) + new_value + m.group(3),
        content,
        count=1,
    )
    return new_content


def apply_updates(file_path: str, updates: list[dict]) -> dict:
    """
    Apply a list of version updates to a pom.xml file.

    Each update in `updates` is a dict:
      {
        "group_id": "org.projectlombok",
        "artifact_id": "lombok",
        "new_version": "1.18.32",
        # optional — if the dep uses a property for its version:
        "property_key": "lombok.version"
      }

    Returns a result dict with success/failure per update and the final pom path.
    """
    path = Path(file_path)
    content = path.read_text(encoding="utf-8")
    results = []

    for upd in updates:
        group_id = upd["group_id"]
        artifact_id = upd["artifact_id"]
        new_version = upd["new_version"]
        property_key = upd.get("property_key")

        original_content = content

        if property_key:
            content = _replace_property_value(content, property_key, new_version)
        else:
            content = _replace_xml_version(content, group_id, artifact_id, new_version)

        changed = content != original_content
        results.append({
            "group_id": group_id,
            "artifact_id": artifact_id,
            "new_version": new_version,
            "property_key": property_key,
            "applied": changed,
            "message": "Updated successfully" if changed else "Version tag not found — manual update may be required",
        })

    path.write_text(content, encoding="utf-8")

    return {
        "file_path": str(path),
        "updates_applied": sum(1 for r in results if r["applied"]),
        "updates_failed": sum(1 for r in results if not r["applied"]),
        "details": results,
    }


def pom_to_dict(pom: ParsedPom) -> dict:
    """Serialise a ParsedPom to a plain dict (JSON-safe)."""

    def dep_to_dict(d: Dependency) -> dict:
        return {
            "group_id": d.group_id,
            "artifact_id": d.artifact_id,
            "coordinates": d.coordinates,
            "version": d.version,
            "resolved_version": d.resolved_version,
            "property_key": d.property_key,
            "scope": d.scope,
            "is_plugin": d.is_plugin,
            "managed_by_bom": d.managed_by_bom,
            "in_dependency_management": d.in_dependency_management,
        }

    return {
        "file_path": pom.file_path,
        "parent": {
            "group_id": pom.parent_group_id,
            "artifact_id": pom.parent_artifact_id,
            "version": pom.parent_version,
        },
        "is_spring_boot_child": pom.is_spring_boot_child,
        "spring_boot_version": pom.spring_boot_version,
        "java_version": pom.java_version,
        "properties": pom.properties,
        "dependencies": [dep_to_dict(d) for d in pom.dependencies],
        "managed_dependencies": [dep_to_dict(d) for d in pom.managed_dependencies],
        "plugins": [dep_to_dict(d) for d in pom.plugins],
        "summary": {
            "total_dependencies": len(pom.dependencies),
            "bom_managed": sum(1 for d in pom.dependencies if d.managed_by_bom),
            "explicit_version": sum(1 for d in pom.dependencies if d.version and not d.managed_by_bom),
            "total_plugins": len(pom.plugins),
        },
    }

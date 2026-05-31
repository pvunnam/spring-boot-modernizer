"""
Maven Central API client for fetching artifact versions and BOM data.
"""

import re
import xml.etree.ElementTree as ET
from typing import Optional
import requests

MAVEN_SEARCH_URL = "https://search.maven.org/solrsearch/select"
MAVEN_REPO_URL = "https://repo1.maven.org/maven2"
STABLE_VERSION_PATTERN = re.compile(
    r"^\d+\.\d+.*$"
)
UNSTABLE_SUFFIXES = re.compile(
    r"(-SNAPSHOT|-RC\d*|-M\d+|-alpha\d*|-beta\d*|-milestone\d*|-preview\d*)$",
    re.IGNORECASE,
)
POM_NS = "http://maven.apache.org/POM/4.0.0"


def _is_stable(version: str) -> bool:
    return bool(STABLE_VERSION_PATTERN.match(version)) and not UNSTABLE_SUFFIXES.search(version)


def get_latest_version(group_id: str, artifact_id: str, include_prerelease: bool = False) -> dict:
    """
    Fetch the latest stable (or latest overall) version of a Maven artifact.
    Returns dict with 'latest_stable', 'latest_any', and 'all_versions' (top 10).
    """
    params = {
        "q": f'g:"{group_id}" AND a:"{artifact_id}"',
        "core": "gav",
        "rows": 20,
        "wt": "json",
    }
    try:
        resp = requests.get(MAVEN_SEARCH_URL, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        return {"error": str(e), "group_id": group_id, "artifact_id": artifact_id}

    docs = data.get("response", {}).get("docs", [])
    if not docs:
        return {
            "group_id": group_id,
            "artifact_id": artifact_id,
            "latest_stable": None,
            "latest_any": None,
            "all_versions": [],
        }

    all_versions = [d["v"] for d in docs]
    stable_versions = [v for v in all_versions if _is_stable(v)]

    return {
        "group_id": group_id,
        "artifact_id": artifact_id,
        "latest_stable": stable_versions[0] if stable_versions else None,
        "latest_any": all_versions[0] if all_versions else None,
        "all_versions": all_versions[:10],
    }


def fetch_bom_managed_versions(group_id: str, artifact_id: str, version: str) -> dict:
    """
    Download a BOM POM from Maven Central and extract its dependencyManagement entries.
    Used to determine what versions a Spring Boot BOM manages.
    """
    g_path = group_id.replace(".", "/")
    url = f"{MAVEN_REPO_URL}/{g_path}/{artifact_id}/{version}/{artifact_id}-{version}.pom"

    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        pom_text = resp.text
    except requests.RequestException as e:
        return {"error": f"Could not fetch BOM from {url}: {e}"}

    return _parse_bom_pom(pom_text, version)


def _parse_bom_pom(pom_text: str, version: str) -> dict:
    """Parse a BOM POM XML and extract managed dependency versions."""
    try:
        # Strip default namespace so ElementTree finds tags easily
        pom_text_clean = pom_text.replace(f' xmlns="{POM_NS}"', "")
        root = ET.fromstring(pom_text_clean)
    except ET.ParseError as e:
        return {"error": f"Failed to parse BOM POM: {e}"}

    # Collect <properties> — BOM POMs use property placeholders
    properties: dict[str, str] = {}
    for prop in root.findall(".//properties/*"):
        properties[prop.tag] = prop.text or ""

    # Replace property placeholders with their values
    def resolve(value: str | None) -> str:
        if not value:
            return ""
        for k, v in properties.items():
            value = value.replace(f"${{{k}}}", v)
        return value

    managed: dict[str, str] = {}
    for dep in root.findall(".//dependencyManagement/dependencies/dependency"):
        g = resolve(dep.findtext("groupId", ""))
        a = resolve(dep.findtext("artifactId", ""))
        v = resolve(dep.findtext("version", ""))
        if g and a and v:
            managed[f"{g}:{a}"] = v

    return {
        "bom_version": version,
        "managed_count": len(managed),
        "managed_dependencies": managed,
        "properties": properties,
    }


def fetch_spring_boot_bom(spring_boot_version: str) -> dict:
    """
    Convenience wrapper: fetch the Spring Boot dependencies BOM for a given version.
    """
    return fetch_bom_managed_versions(
        "org.springframework.boot",
        "spring-boot-dependencies",
        spring_boot_version,
    )


def get_artifact_metadata(group_id: str, artifact_id: str) -> dict:
    """
    Get maven-metadata.xml for release listing (alternative to search API).
    Returns all known release versions.
    """
    g_path = group_id.replace(".", "/")
    url = f"{MAVEN_REPO_URL}/{g_path}/{artifact_id}/maven-metadata.xml"

    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
    except (requests.RequestException, ET.ParseError) as e:
        return {"error": str(e)}

    latest = root.findtext(".//release") or root.findtext(".//latest") or ""
    versions = [v.text for v in root.findall(".//versions/version") if v.text]
    stable = [v for v in reversed(versions) if _is_stable(v)]

    return {
        "group_id": group_id,
        "artifact_id": artifact_id,
        "latest_release": latest,
        "latest_stable": stable[-1] if stable else None,
        "all_versions": list(reversed(versions))[:15],
    }

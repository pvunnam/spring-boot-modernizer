"""
Compatibility rules and analysis for Spring Boot dependency upgrades.

Covers:
  - Spring Boot ↔ Java version requirements
  - Spring Boot 2.x → 3.x migration breaking changes
  - Known library-specific constraints
  - Major version upgrade warnings
"""

from dataclasses import dataclass
from packaging.version import Version, InvalidVersion
from typing import Optional

# ---------------------------------------------------------------------------
# Spring Boot ↔ Java compatibility matrix
# ---------------------------------------------------------------------------

SPRING_BOOT_JAVA_REQUIREMENTS: dict[str, dict] = {
    "3.4": {"min_java": 17, "recommended_java": 21, "eol": False},
    "3.3": {"min_java": 17, "recommended_java": 21, "eol": False},
    "3.2": {"min_java": 17, "recommended_java": 21, "eol": False},
    "3.1": {"min_java": 17, "recommended_java": 17, "eol": True},
    "3.0": {"min_java": 17, "recommended_java": 17, "eol": True},
    "2.7": {"min_java": 8, "recommended_java": 17, "eol": True},
    "2.6": {"min_java": 8, "recommended_java": 11, "eol": True},
}

# ---------------------------------------------------------------------------
# Known breaking changes by artifact and version threshold
# ---------------------------------------------------------------------------

@dataclass
class BreakingChange:
    artifact: str          # "groupId:artifactId" or just groupId prefix
    from_major: int        # major version where break starts
    title: str
    description: str
    migration_guide: Optional[str] = None


BREAKING_CHANGES: list[BreakingChange] = [
    BreakingChange(
        artifact="org.springframework.boot:spring-boot-starter-parent",
        from_major=3,
        title="Spring Boot 3.x — Jakarta EE 10 (javax → jakarta namespace)",
        description=(
            "All javax.* imports must be migrated to jakarta.* "
            "(e.g. javax.persistence → jakarta.persistence, javax.servlet → jakarta.servlet). "
            "Requires Java 17+."
        ),
        migration_guide="https://spring.io/blog/2022/05/24/preparing-for-spring-boot-3-0",
    ),
    BreakingChange(
        artifact="org.springframework.security:spring-security-core",
        from_major=6,
        title="Spring Security 6.x — WebSecurityConfigurerAdapter removed",
        description=(
            "WebSecurityConfigurerAdapter is removed. Migrate to component-based security "
            "config using SecurityFilterChain beans. Also, antMatchers() is replaced by requestMatchers()."
        ),
        migration_guide="https://docs.spring.io/spring-security/reference/migration/index.html",
    ),
    BreakingChange(
        artifact="org.hibernate.orm:hibernate-core",
        from_major=6,
        title="Hibernate ORM 6.x — breaking API and SQL generation changes",
        description=(
            "Hibernate 6 generates different SQL, has new criteria API behavior, "
            "removed deprecated APIs, and uses Jakarta Persistence 3.x (jakarta.persistence)."
        ),
        migration_guide="https://github.com/hibernate/hibernate-orm/blob/main/migration-guide.adoc",
    ),
    BreakingChange(
        artifact="org.springframework:spring-framework",
        from_major=6,
        title="Spring Framework 6.x — baseline Java 17, Jakarta EE 9+",
        description=(
            "Spring Framework 6 requires Java 17+ and Jakarta EE 9+ APIs. "
            "HttpMethod is now an open-ended value type, not an enum."
        ),
    ),
    BreakingChange(
        artifact="io.micrometer:micrometer-core",
        from_major=2,
        title="Micrometer 2.x — API restructuring",
        description=(
            "Micrometer 2 changes the SPI and Timer/Counter creation APIs. "
            "Custom MeterBinder and MeterRegistry implementations may need updates."
        ),
    ),
    BreakingChange(
        artifact="org.flywaydb:flyway-core",
        from_major=10,
        title="Flyway 10.x — database-specific modules split out",
        description=(
            "Database-specific support (MySQL, PostgreSQL, etc.) moved to separate modules. "
            "Add flyway-mysql, flyway-database-postgresql, etc. as needed."
        ),
        migration_guide="https://documentation.red-gate.com/fd/release-notes-flyway-flywaydb-262405780.html",
    ),
    BreakingChange(
        artifact="com.querydsl:querydsl-apt",
        from_major=5,
        title="QueryDSL 5.x — uses Jakarta EE",
        description="QueryDSL 5.x uses jakarta.* annotations; not compatible with javax.* (Spring Boot 2.x).",
    ),
    BreakingChange(
        artifact="org.mapstruct:mapstruct",
        from_major=2,
        title="MapStruct 2.x — annotation processor changes",
        description="MapStruct 2.x may require updates to custom processors and Spring integration.",
    ),
]

# ---------------------------------------------------------------------------
# Minimum Spring Boot version required by popular libraries
# ---------------------------------------------------------------------------

LIBRARY_SPRING_BOOT_MIN: dict[str, dict] = {
    # "groupId:artifactId": {"min_sb": "3.0", "note": "..."}
    "org.springdoc:springdoc-openapi-starter-webmvc-ui": {
        "min_sb": "3.0",
        "note": "Use springdoc-openapi-ui (without -starter-) for Spring Boot 2.x",
    },
    "io.micrometer:micrometer-tracing": {
        "min_sb": "3.0",
        "note": "Micrometer Tracing replaces Spring Cloud Sleuth in Spring Boot 3.x",
    },
}

# ---------------------------------------------------------------------------
# Analysis functions
# ---------------------------------------------------------------------------

def _major(version_str: str) -> Optional[int]:
    try:
        return Version(version_str).major
    except InvalidVersion:
        return None


def _minor_str(version_str: str) -> str:
    """Return 'major.minor' string, e.g. '3.2' from '3.2.5'."""
    try:
        v = Version(version_str)
        return f"{v.major}.{v.minor}"
    except InvalidVersion:
        return version_str


def check_spring_boot_java_compatibility(spring_boot_version: str, java_version: str) -> list[dict]:
    """
    Check that the Java version satisfies Spring Boot's requirements.
    Returns a list of issue dicts (empty = all good).
    """
    issues = []
    sb_minor = _minor_str(spring_boot_version)
    reqs = SPRING_BOOT_JAVA_REQUIREMENTS.get(sb_minor)

    if reqs is None:
        issues.append({
            "severity": "warning",
            "artifact": f"org.springframework.boot:{spring_boot_version}",
            "message": f"Spring Boot {spring_boot_version} compatibility data not available for this tool.",
        })
        return issues

    try:
        jv = int(java_version.split(".")[0])
    except (ValueError, AttributeError):
        return issues

    if jv < reqs["min_java"]:
        issues.append({
            "severity": "error",
            "artifact": "spring-boot / java",
            "message": (
                f"Spring Boot {spring_boot_version} requires Java {reqs['min_java']}+, "
                f"but java.version is set to {java_version}."
            ),
        })

    if reqs.get("eol"):
        issues.append({
            "severity": "warning",
            "artifact": f"org.springframework.boot:spring-boot-starter-parent:{spring_boot_version}",
            "message": f"Spring Boot {spring_boot_version} has reached End-of-Life. Upgrade to 3.3+ is strongly recommended.",
        })

    return issues


def check_breaking_changes(proposed: list[dict]) -> list[dict]:
    """
    Detect breaking changes in proposed dependency upgrades.

    `proposed` is a list of dicts:
      {"group_id": ..., "artifact_id": ..., "current_version": ..., "new_version": ...}

    Returns a list of warning/error dicts.
    """
    issues = []

    for dep in proposed:
        coords = f"{dep['group_id']}:{dep['artifact_id']}"
        cur_ver = dep.get("current_version")
        new_ver = dep.get("new_version")

        if not cur_ver or not new_ver:
            continue

        cur_major = _major(cur_ver)
        new_major = _major(new_ver)

        if cur_major is None or new_major is None or new_major <= cur_major:
            continue

        # Flag any major upgrade
        issues.append({
            "severity": "warning",
            "artifact": coords,
            "message": (
                f"Major version upgrade: {cur_ver} → {new_ver}. "
                f"Review release notes and migration guide before applying."
            ),
        })

        # Check specific breaking-change rules
        for bc in BREAKING_CHANGES:
            if coords.startswith(bc.artifact) or dep["group_id"] in bc.artifact:
                if new_major >= bc.from_major > cur_major:
                    issue = {
                        "severity": "error",
                        "artifact": coords,
                        "title": bc.title,
                        "message": bc.description,
                    }
                    if bc.migration_guide:
                        issue["migration_guide"] = bc.migration_guide
                    issues.append(issue)

    return issues


def check_library_spring_boot_compatibility(
    proposed: list[dict],
    spring_boot_version: str,
) -> list[dict]:
    """
    Check that specific libraries are compatible with the target Spring Boot version.
    """
    issues = []
    try:
        sb_ver = Version(spring_boot_version)
    except InvalidVersion:
        return issues

    for dep in proposed:
        coords = f"{dep['group_id']}:{dep['artifact_id']}"
        rule = LIBRARY_SPRING_BOOT_MIN.get(coords)
        if not rule:
            continue
        try:
            min_sb = Version(rule["min_sb"])
        except InvalidVersion:
            continue
        if sb_ver < min_sb:
            issues.append({
                "severity": "error",
                "artifact": coords,
                "message": (
                    f"{coords} requires Spring Boot {rule['min_sb']}+, "
                    f"but project uses {spring_boot_version}. {rule.get('note', '')}"
                ),
            })

    return issues


def check_bom_override_risk(
    proposed: list[dict],
    bom_managed: dict[str, str],
) -> list[dict]:
    """
    Warn when a proposed version differs from what the Spring Boot BOM manages.
    Overriding BOM versions can lead to subtle incompatibilities.
    """
    issues = []
    for dep in proposed:
        coords = f"{dep['group_id']}:{dep['artifact_id']}"
        bom_version = bom_managed.get(coords)
        if bom_version and dep.get("new_version") and dep["new_version"] != bom_version:
            issues.append({
                "severity": "warning",
                "artifact": coords,
                "message": (
                    f"Proposed version {dep['new_version']} overrides the Spring Boot BOM-managed "
                    f"version {bom_version}. This may cause compatibility issues. "
                    "Consider removing the explicit version and letting the BOM manage it."
                ),
                "bom_version": bom_version,
                "proposed_version": dep["new_version"],
            })
    return issues


def full_compatibility_report(
    proposed: list[dict],
    spring_boot_version: Optional[str],
    java_version: Optional[str],
    bom_managed: Optional[dict[str, str]] = None,
) -> dict:
    """
    Run all compatibility checks and return a consolidated report.
    """
    all_issues: list[dict] = []

    if spring_boot_version and java_version:
        all_issues += check_spring_boot_java_compatibility(spring_boot_version, java_version)

    all_issues += check_breaking_changes(proposed)

    if spring_boot_version:
        all_issues += check_library_spring_boot_compatibility(proposed, spring_boot_version)

    if bom_managed:
        all_issues += check_bom_override_risk(proposed, bom_managed)

    errors = [i for i in all_issues if i.get("severity") == "error"]
    warnings = [i for i in all_issues if i.get("severity") == "warning"]

    return {
        "compatible": len(errors) == 0,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "errors": errors,
        "warnings": warnings,
        "all_issues": all_issues,
    }

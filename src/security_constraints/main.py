"""Main module."""
import argparse
import logging
import sys
from datetime import datetime

if sys.version_info >= (3, 8):
    from importlib.metadata import version
else:
    from importlib_metadata import version

from typing import IO, List, Optional, Sequence

import yaml

from security_constraints.common import (
    ArgumentNamespace,
    Configuration,
    PackageConstraints,
    SecurityConstraintsError,
    SecurityVulnerability,
    SecurityVulnerabilityDatabaseAPI,
)
from security_constraints.github_security_advisory import GithubSecurityAdvisoryAPI

LOGGER = logging.getLogger(__name__)


def get_security_vulnerability_database_apis(
    severities: Optional[List[str]] = None,
) -> List[SecurityVulnerabilityDatabaseAPI]:
    """Return the APIs to use for fetching vulnerabilities."""
    return [GithubSecurityAdvisoryAPI(severities=severities)]


def fetch_vulnerabilities(
    apis: Sequence[SecurityVulnerabilityDatabaseAPI],
) -> List[SecurityVulnerability]:
    """Use apis to fetch and return vulnerabilities."""
    vulnerabilities: List[SecurityVulnerability] = []
    for api in apis:
        LOGGER.debug("Fetching vulnerabilities from %s...", api.get_database_name())
        vulnerabilities.extend(api.get_vulnerabilities())
    return vulnerabilities


def filter_vulnerabilities(
    config: Configuration, vulnerabilities: List[SecurityVulnerability]
) -> List[SecurityVulnerability]:
    """Filter out vulnerabilities that should be ignored and return the rest."""
    if config.ignore_ids:
        LOGGER.debug("Applying ignore-ids...")
        vulnerabilities = [
            v for v in vulnerabilities if v.identifier not in config.ignore_ids
        ]
    return vulnerabilities


def sort_vulnerabilities(
    vulnerabilities: List[SecurityVulnerability],
) -> List[SecurityVulnerability]:
    """Sort vulnerabilities into the order they should appear in the constraints."""
    return sorted(vulnerabilities, key=lambda v: v.package)


def get_safe_version_constraints(
    vulnerability: SecurityVulnerability,
) -> PackageConstraints:
    """Invert range of a vulnerability into constraints specifying unaffected versions.

    See SecurityVulnerability documentation for more information.

    """
    safe_specs: List[str] = []
    vulnerable_spec: str
    if "," in vulnerability.vulnerable_range:
        # If there is a known min and max affected version, make the constraints
        # just specify the minimum safe version, since min and max constraints cannot
        # be met at the same time.
        vulnerable_spec = [
            p.strip() for p in vulnerability.vulnerable_range.split(",")
        ][-1]
    else:
        vulnerable_spec = vulnerability.vulnerable_range.strip()

    if vulnerable_spec.startswith("= "):
        safe_specs.append(f"!={vulnerable_spec[2:]}")
    elif vulnerable_spec.startswith("<= "):
        safe_specs.append(f">{vulnerable_spec[3:]}")
    elif vulnerable_spec.startswith("< "):
        safe_specs.append(f">={vulnerable_spec[2:]}")
    elif vulnerable_spec.startswith(">= "):
        safe_specs.append(f"<{vulnerable_spec[3:]}")
    return PackageConstraints(
        package=vulnerability.package,
        specifiers=safe_specs,
    )


def are_constraints_pip_friendly(constraints: PackageConstraints) -> bool:
    """Return if the given PackageConstraints is understandable by pip.

    Pip does not understand versions like "2.5.0a05" when it is
    an inequality, e.g. "<= 2.5.0a05". That gets replaced by a strict
    equality. Then this function will return False, because pip
    cannot properly parse the constraint.

    """
    for part in constraints.specifiers:
        if part.startswith("="):
            continue
        version = part.strip("<>=! ")
        if not version.replace(".", "").isnumeric():
            LOGGER.debug(
                "Pip-unfriendly constraint '%s' (%s) -> ignore.",
                part,
                constraints.package,
            )
            return False
    return True


def create_header(
    apis: Sequence[SecurityVulnerabilityDatabaseAPI], config: Configuration
) -> str:
    """Create the comment header which goes at the top of the output."""
    timestamp: str = f"{datetime.utcnow().isoformat()}Z"
    sources: List[str] = [api.get_database_name() for api in apis]
    app_name: str = "security-constraints"
    lines: List[str] = [
        f"Generated by {app_name} {version(app_name)} on {timestamp}",
        f"Data sources: {', '.join(sources)}",
        f"Configuration: {config.to_dict()}",
    ]
    return "\n".join([f"# {line}" for line in lines])


def format_constraints_file_line(
    constraints: PackageConstraints, vulnerability: SecurityVulnerability
) -> str:
    """Format a line in the final pip constraints output.

    Args:
        constraints: The relevant package and the constraints to place upon it.
        vulnerability: The vulnerability tackled by the constraints.

    """
    if constraints.package != vulnerability.package:
        raise AssertionError(
            "Constraints and vulnerability are for different packages!"
            " This suggests a programming error!"
        )
    return f"{constraints}" f"  # {vulnerability.name} (ID: {vulnerability.identifier})"


def get_args() -> ArgumentNamespace:
    """Parse arguments from the command line and return them."""
    parser = argparse.ArgumentParser(
        description=(
            "Fetches security vulnerabilities from external sources "
            "and creates a list of pip-compatible version constraints "
            "that can be used to avoid versions affected by the "
            "vulnerabilities."
        )
    )
    parser.add_argument(
        "--dump-config",
        action="store_true",
        help=(
            "Print config file corresponding to the current settings to stdout "
            "and exit. Config file can be used as a template."
        ),
    )
    parser.add_argument(
        "--debug", action="store_true", default=False, help="Debugging output."
    )
    parser.add_argument(
        "-v", "--version", action="store_true", help="Print version and exit."
    )
    parser.add_argument(
        "--output",
        type=argparse.FileType(mode="w"),
        action="store",
        default="-",
        help="Output file name or '-' for stdout.",
    )
    parser.add_argument(
        "--ignore-ids",
        type=str,
        action="store",
        nargs="+",
        default=[],
        help=(
            "IDs of vulnerabilities to ignore."
            " Can also be given as 'ignore_ids' in config file."
        ),
    )
    parser.add_argument(
        "--config",
        type=str,
        action="store",
        help=(
            "Path to configuration file."
            f" Supported keys: {Configuration.supported_keys()}"
        ),
    )
    parser.add_argument(
        "--severities",
        type=str,
        action="store",
        nargs="+",
        default=["critical"],
        help=(
            "Vulnerability severities include."
            " Can also be given as 'severities' in config file."
        ),
    )
    return parser.parse_args(namespace=ArgumentNamespace())


def get_config(config_file: Optional[str]) -> Configuration:
    """Return configuration read from config_file.

    Default config will be returned if config_file is None.

    """
    if config_file is None:
        return Configuration()

    with open(config_file, mode="r") as fh:
        return Configuration.from_dict(yaml.safe_load(fh))


def setup_logging(debug: bool = False) -> None:
    logging.getLogger().setLevel(logging.DEBUG if debug else logging.INFO)


def main() -> int:
    """Main flow of the application.

    Returns:
        The program exit code as an integer.

    """
    output: Optional[IO] = None
    try:
        args = get_args()
        if args.version:
            print(version("security-constraints"))
            return 0
        setup_logging(debug=args.debug)
        output = args.output
        if output is None:
            raise AssertionError(
                "'output' is not a stream! This suggests a programming error"
            )
        config: Configuration = get_config(config_file=args.config)
        config.ignore_ids.extend(sorted(args.ignore_ids))
        config.severities.extend(sorted(args.severities))

        if args.dump_config:
            yaml.safe_dump(config.to_dict(), stream=sys.stdout)
            return 0

        apis: List[
            SecurityVulnerabilityDatabaseAPI
        ] = get_security_vulnerability_database_apis(severities=args.severities)

        vulnerabilities: List[SecurityVulnerability] = fetch_vulnerabilities(apis)
        vulnerabilities = filter_vulnerabilities(config, vulnerabilities)
        vulnerabilities = sort_vulnerabilities(vulnerabilities)

        LOGGER.debug("Writing constraints...")
        output.write(f"{create_header(apis, config)}\n")
        for vulnerability in vulnerabilities:
            constraints: PackageConstraints = get_safe_version_constraints(
                vulnerability
            )
            if are_constraints_pip_friendly(constraints):
                output.write(
                    f"{format_constraints_file_line(constraints, vulnerability)}\n"
                )
    except SecurityConstraintsError as error:
        LOGGER.error(error)
        return 1
    except Exception as error:
        LOGGER.critical(
            "Caught unhandled exception at top-level: %s", error, exc_info=True
        )
        return 2
    else:
        return 0
    finally:
        if output is not None and not output.isatty():
            output.close()

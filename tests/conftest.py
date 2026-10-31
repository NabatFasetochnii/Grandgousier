from __future__ import annotations


def pytest_addoption(parser) -> None:
    parser.addoption(
        "--grandgousier-test-config",
        action="store",
        default=None,
        metavar="PATH",
        help="Use an explicit config file for PostgreSQL integration tests",
    )

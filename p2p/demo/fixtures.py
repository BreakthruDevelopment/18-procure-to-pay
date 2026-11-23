"""Paths to committed recorded-reply files, including those owned by earlier slices."""

from __future__ import annotations

from importlib import resources
from pathlib import Path

from p2p.extract.ingest import fixture_path as extract_fixture


def demo_fixture(name: str) -> Path:
    """A recording this slice committed under `p2p/demo/fixtures/`."""
    return Path(str(resources.files("p2p.demo").joinpath("fixtures", name)))


def match_fixture(name: str) -> Path:
    return Path(str(resources.files("p2p.match").joinpath("fixtures", name)))


def extract_recording(name: str) -> Path:
    return extract_fixture(name)


def seeded_document(name: str) -> Path:
    return Path(str(resources.files("p2p.batch").joinpath("seeded", name)))


def extract_example(name: str) -> Path:
    return Path(str(resources.files("p2p.extract").joinpath("examples", name)))

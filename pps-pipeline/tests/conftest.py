"""Shared fixtures. Everything is fixture-driven: no real capture in CI."""

from __future__ import annotations

import os

os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("USE_FLAX", "0")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import pytest

from pps_pipeline import bundle as _bundle
from pps_pipeline import fixture as _fixture
from pps_pipeline.cli import build_package


@pytest.fixture(scope="session")
def fixture_dir(tmp_path_factory) -> str:
    """A freshly generated synthetic bundle (also proves the generator = a
    conforming 'capture front-end')."""
    d = tmp_path_factory.mktemp("session-demo")
    return _fixture.generate(str(d))


@pytest.fixture(scope="session")
def loaded_bundle(fixture_dir):
    return _bundle.load_bundle(fixture_dir)


@pytest.fixture(scope="session")
def built(fixture_dir):
    """The package build for the fixture (bundle -> chunk -> redact -> interleave)."""
    return build_package(fixture_dir, mode="event_boundary")


@pytest.fixture(scope="session")
def package(built):
    return built.package

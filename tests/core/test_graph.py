from pathlib import Path

import pytest

from mship.core.config import ConfigLoader
from mship.core.graph import DependencyGraph


def test_topo_sort_linear(workspace: Path):
    config = ConfigLoader.load(workspace / "mothership.yaml")
    graph = DependencyGraph(config)
    order = graph.topo_sort()
    assert order.index("shared") < order.index("auth-service")
    assert order.index("auth-service") < order.index("api-gateway")


def test_topo_sort_contains_all_repos(workspace: Path):
    config = ConfigLoader.load(workspace / "mothership.yaml")
    graph = DependencyGraph(config)
    order = graph.topo_sort()
    assert set(order) == {"shared", "auth-service", "api-gateway"}


def test_topo_sort_subset(workspace: Path):
    config = ConfigLoader.load(workspace / "mothership.yaml")
    graph = DependencyGraph(config)
    order = graph.topo_sort(repos=["auth-service", "shared"])
    assert order == ["shared", "auth-service"]


def test_dependents_of_shared(workspace: Path):
    config = ConfigLoader.load(workspace / "mothership.yaml")
    graph = DependencyGraph(config)
    deps = graph.dependents("shared")
    assert set(deps) == {"auth-service", "api-gateway"}


def test_dependents_of_leaf(workspace: Path):
    config = ConfigLoader.load(workspace / "mothership.yaml")
    graph = DependencyGraph(config)
    deps = graph.dependents("api-gateway")
    assert deps == []


def test_dependencies_of_api_gateway(workspace: Path):
    config = ConfigLoader.load(workspace / "mothership.yaml")
    graph = DependencyGraph(config)
    deps = graph.dependencies("api-gateway")
    assert set(deps) == {"shared", "auth-service"}


def test_dependencies_of_root(workspace: Path):
    config = ConfigLoader.load(workspace / "mothership.yaml")
    graph = DependencyGraph(config)
    deps = graph.dependencies("shared")
    assert deps == []

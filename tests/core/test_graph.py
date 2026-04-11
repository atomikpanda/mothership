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


def test_topo_tiers_linear(workspace: Path):
    config = ConfigLoader.load(workspace / "mothership.yaml")
    graph = DependencyGraph(config)
    tiers = graph.topo_tiers()
    assert len(tiers) == 3
    assert tiers[0] == ["shared"]
    assert tiers[1] == ["auth-service"]
    assert tiers[2] == ["api-gateway"]


def test_topo_tiers_parallel(workspace: Path):
    cfg = workspace / "mothership.yaml"
    cfg.write_text("""\
workspace: test
repos:
  shared:
    path: ./shared
    type: library
  auth-service:
    path: ./auth-service
    type: service
    depends_on: [shared]
  api-gateway:
    path: ./api-gateway
    type: service
    depends_on: [shared]
""")
    config = ConfigLoader.load(cfg)
    graph = DependencyGraph(config)
    tiers = graph.topo_tiers()
    assert len(tiers) == 2
    assert tiers[0] == ["shared"]
    assert set(tiers[1]) == {"auth-service", "api-gateway"}


def test_topo_tiers_subset(workspace: Path):
    config = ConfigLoader.load(workspace / "mothership.yaml")
    graph = DependencyGraph(config)
    tiers = graph.topo_tiers(repos=["shared", "auth-service"])
    assert len(tiers) == 2
    assert tiers[0] == ["shared"]
    assert tiers[1] == ["auth-service"]


def test_topo_tiers_no_deps(workspace: Path):
    cfg = workspace / "mothership.yaml"
    cfg.write_text("""\
workspace: test
repos:
  shared:
    path: ./shared
    type: library
  auth-service:
    path: ./auth-service
    type: service
  api-gateway:
    path: ./api-gateway
    type: service
""")
    config = ConfigLoader.load(cfg)
    graph = DependencyGraph(config)
    tiers = graph.topo_tiers()
    assert len(tiers) == 1
    assert set(tiers[0]) == {"shared", "auth-service", "api-gateway"}

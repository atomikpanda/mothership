from mship.core.config import WorkspaceConfig, Dependency


class DependencyGraph:
    """Repo dependency graph with topological sort and traversal."""

    def __init__(self, config: WorkspaceConfig) -> None:
        self._config = config
        self._forward: dict[str, list[str]] = {name: [] for name in config.repos}
        self._reverse: dict[str, list[str]] = {name: [] for name in config.repos}

        for name, repo in config.repos.items():
            for dep in repo.depends_on:
                dep_name = dep.repo if isinstance(dep, Dependency) else dep
                self._forward[dep_name].append(name)
                self._reverse[name].append(dep_name)

    def topo_sort(self, repos: list[str] | None = None) -> list[str]:
        """Return repos in dependency order (dependencies first)."""
        target_set = set(repos) if repos else set(self._config.repos.keys())

        in_degree: dict[str, int] = {}
        for name in target_set:
            in_degree[name] = sum(
                1 for dep in self._reverse[name] if dep in target_set
            )

        queue = sorted(n for n, d in in_degree.items() if d == 0)
        result: list[str] = []

        while queue:
            node = queue.pop(0)
            result.append(node)
            for neighbor in self._forward[node]:
                if neighbor in target_set:
                    in_degree[neighbor] -= 1
                    if in_degree[neighbor] == 0:
                        queue.append(neighbor)
            queue.sort()

        return result

    def topo_tiers(self, repos: list[str] | None = None) -> list[list[str]]:
        """Return repos grouped into dependency tiers.

        Each tier is a list of repos that can run concurrently.
        Tiers are ordered: tier N's deps are all in tiers 0..N-1.
        """
        target_set = set(repos) if repos else set(self._config.repos.keys())

        in_degree: dict[str, int] = {}
        for name in target_set:
            in_degree[name] = sum(
                1 for dep in self._reverse[name] if dep in target_set
            )

        tiers: list[list[str]] = []
        remaining = set(target_set)

        while remaining:
            tier = sorted(n for n in remaining if in_degree[n] == 0)
            if not tier:
                break
            tiers.append(tier)
            for node in tier:
                remaining.discard(node)
                for neighbor in self._forward[node]:
                    if neighbor in remaining:
                        in_degree[neighbor] -= 1

        return tiers

    def dependents(self, repo: str) -> list[str]:
        """Return all transitive downstream dependents of a repo."""
        visited: set[str] = set()
        stack = list(self._forward[repo])
        while stack:
            node = stack.pop()
            if node not in visited:
                visited.add(node)
                stack.extend(self._forward[node])
        return sorted(visited)

    def dependencies(self, repo: str) -> list[str]:
        """Return all transitive upstream dependencies of a repo."""
        visited: set[str] = set()
        stack = list(self._reverse[repo])
        while stack:
            node = stack.pop()
            if node not in visited:
                visited.add(node)
                stack.extend(self._reverse[node])
        return sorted(visited)

    def direct_deps(self, repo_name: str) -> list[str]:
        """Return the direct dependency names of `repo_name`."""
        return list(self._reverse[repo_name])

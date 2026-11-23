"""Guard the source module graph against cycles and upward layering."""

import ast
from functools import lru_cache
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "rss2discord"


def _module_name(path: Path) -> str:
    parts = path.relative_to(SOURCE_ROOT).parts
    if parts == ("__init__.py",):
        return "rss2discord"
    if parts[-1] == "__init__.py":
        return "rss2discord." + ".".join(parts[:-1])
    return "rss2discord." + ".".join((*parts[:-1], path.stem))


@lru_cache(maxsize=1)
def _import_graph() -> dict[str, set[str]]:
    modules = {_module_name(path): path for path in SOURCE_ROOT.rglob("*.py")}
    graph: dict[str, set[str]] = {name: set() for name in modules}
    for name, path in modules.items():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            targets: list[str] = []
            if isinstance(node, ast.ImportFrom):
                if node.level:
                    base = name.split(".")[:-1]
                    if node.level > 1:
                        base = base[: -(node.level - 1)]
                    module = ".".join((*base, *(node.module or "").split(".")))
                    targets.append(module.rstrip("."))
                elif node.module and node.module.startswith("rss2discord."):
                    targets.append(node.module)
            elif isinstance(node, ast.Import):
                targets.extend(
                    alias.name
                    for alias in node.names
                    if alias.name.startswith("rss2discord.")
                )
            graph[name].update(target for target in targets if target in modules)
    return graph


def test_source_modules_have_no_import_cycles() -> None:
    graph = _import_graph()
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(module: str) -> None:
        assert module not in visiting, f"import cycle reaches {module}"
        if module in visited:
            return
        visiting.add(module)
        for dependency in graph[module]:
            visit(dependency)
        visiting.remove(module)
        visited.add(module)

    for module in graph:
        visit(module)


def test_lower_layers_do_not_import_application_wiring() -> None:
    graph = _import_graph()
    forbidden = {
        "rss2discord.providers": (
            "rss2discord.app",
            "rss2discord.main",
            "rss2discord.runtime",
        ),
        "rss2discord.transports": ("rss2discord.providers",),
        "rss2discord.discord": ("rss2discord.providers",),
        "rss2discord.runtime": ("rss2discord.app", "rss2discord.main"),
    }

    violations = [
        f"{module} -> {dependency}"
        for module, dependencies in graph.items()
        for prefix, targets in forbidden.items()
        if module.startswith(prefix)
        for dependency in dependencies
        if dependency.startswith(targets)
    ]

    assert violations == []

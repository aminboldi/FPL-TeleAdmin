"""Load top-level definitions from the bot's modules without importing them.

Importing the runtime modules loads the configuration, initialises the
production databases and builds a Telegram client. Tests that exercise a
handler's control flow therefore compile just the definitions they need into
a namespace of mocks. The bot used to be one file; its definitions now live
across these modules, so look them up wherever they are.
"""
import ast
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1] / "teleadmin_project"
MODULES = (
    "bot", "app", "post_format", "delivery", "catalog_sync", "article_pipeline",
    "youtube_pipeline", "x_pipeline", "forwarded", "dashboard_content",
)


def _name(node):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return node.name
    if isinstance(node, ast.Assign):
        return next((t.id for t in node.targets if isinstance(t, ast.Name)), None)
    return None


def definitions(names) -> list[ast.stmt]:
    """Return the AST nodes defining ``names``, failing loudly on any missing."""
    wanted = set(names)
    found = []
    for module in MODULES:
        for node in ast.parse((PROJECT / f"{module}.py").read_text(encoding="utf-8")).body:
            if _name(node) in wanted:
                found.append(node)
                wanted.discard(_name(node))
    if wanted:
        raise LookupError(f"definitions not found: {sorted(wanted)}")
    return found


def load(names, namespace: dict) -> dict:
    """Compile ``names`` into ``namespace`` and return it."""
    module = ast.Module(body=definitions(names), type_ignores=[])
    exec(compile(module, str(PROJECT / "bot.py"), "exec"), namespace)
    return namespace

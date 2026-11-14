"""Names a file uses but never defines: what is left dangling after code is removed.

    python tools/undefined_names.py atticus.py

A rough check (every name stored anywhere in the file counts as defined), which
is enough to catch a call to a function that no longer exists.
"""
import ast
import builtins
import sys


def undefined(source: str) -> list[tuple[int, str]]:
    tree = ast.parse(source)
    known = set(dir(builtins)) | {"__file__", "__name__"}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            known.add(node.name)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            a = node.args
            known.update(x.arg for x in a.posonlyargs + a.args + a.kwonlyargs)
            known.update(x.arg for x in (a.vararg, a.kwarg) if x)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            known.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            known.update((n.asname or n.name).split(".")[0] for n in node.names)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            known.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            known.update(node.names)
        elif isinstance(node, ast.MatchAs) and node.name:
            known.add(node.name)
    return sorted({(n.lineno, n.id) for n in ast.walk(tree)
                   if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id not in known})


if __name__ == "__main__":
    found = undefined(open(sys.argv[1]).read())
    for line, name in found:
        print(f"{sys.argv[1]}:{line}: {name}")
    sys.exit(1 if found else 0)

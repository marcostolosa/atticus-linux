"""Delete top-level functions and classes that nothing in the file refers to.

    python tools/prune_unused.py atticus.py [keep_name ...]

Repeats until nothing changes. A reference is a real use of the name in
code; the same word inside a string or a comment does not count.
"""
import ast
import sys


def prune(source: str, keep=()) -> tuple[str, list[str]]:
    removed = []
    while True:
        tree = ast.parse(source)
        tops = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))]
        dead = None
        for node in tops:
            if node.name in keep or sum(t.name == node.name for t in tops) > 1:
                continue
            inside = {id(x) for x in ast.walk(node)}
            used = any(isinstance(x, ast.Name) and x.id == node.name and id(x) not in inside for x in ast.walk(tree)) \
                or any(isinstance(x, ast.Attribute) and x.attr == node.name and id(x) not in inside for x in ast.walk(tree))
            if not used:
                dead = node
                break
        if dead is None:
            return source, removed
        lines = source.split("\n")
        start = min([dead.lineno] + [d.lineno for d in dead.decorator_list]) - 1
        end = dead.end_lineno
        while end < len(lines) and not lines[end].strip():
            end += 1                      # the blank lines that followed it
        del lines[start:end]
        source = "\n".join(lines)
        removed.append(dead.name)


if __name__ == "__main__":
    path, keep = sys.argv[1], set(sys.argv[2:])
    text, gone = prune(open(path).read(), keep)
    open(path, "w").write(text)
    print(len(gone), "removed:", " ".join(gone))

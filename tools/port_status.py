"""What in the brain still relies on machinery this desktop does not have.

    python tools/port_status.py [atticus.py]

One line per function with the lines that still call it. This is the to-do
list of the port: a function leaves the list when it works here, not when it
is deleted.
"""
import re
import sys

KEY = re.compile(r"osascript|\bosa\(|System Events|AppleScript|\bAX[A-Z]\w+|defaults (?:read|write)|pbcopy|pbpaste|"
                 r"screencapture|/Applications|Library/|launchctl|sips |lsappinfo|NSWorkspace|plistlib\.load")


def status(source: str) -> dict[str, list[int]]:
    found, owner = {}, "<top>"
    for n, line in enumerate(source.split("\n"), 1):
        m = re.match(r"(?:def|class) (\w+)", line)
        if m:
            owner = m.group(1)
        elif line and not line[0].isspace() and not line.startswith(("#", ")", "]", "}")):
            owner = "<top>"
        if KEY.search(line):
            found.setdefault(owner, []).append(n)
    return found


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "atticus.py"
    rows = status(open(path).read())
    for name, lines in sorted(rows.items(), key=lambda kv: -len(kv[1])):
        print(f"{name:28} {len(lines):3}  lines {', '.join(map(str, lines[:8]))}")
    print(f"\n{sum(map(len, rows.values()))} lines in {len(rows)} places still to port")

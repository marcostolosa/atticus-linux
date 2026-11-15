"""The address book, read from vCard files.

  ~/.config/atticus/contacts/      drop .vcf files here (an export from a phone
                                   or a web address book is one file, many cards)
  ~/.contacts/<name>/              the folders khard and vdirsyncer use
  settings "contactsDirs"          more folders or .vcf files

Nothing is ever written: the address book belongs to whatever made the files.
Who the user is comes from the system account and, when there is one, from
their own card (settings "myCard" names it; otherwise the card with the
account's name).
"""
from __future__ import annotations

import os
import pwd
import quopri
import re
import subprocess
from pathlib import Path

from .agenda import split_line, unescape, unfold
from .settings import get as _setting


def _base() -> Path:
    return Path(os.environ.get("ATTICUS_DIR", Path.home() / ".config/atticus"))


def files() -> list[Path]:
    roots = [_base() / "contacts", Path.home() / ".contacts"]
    extra = _setting("contactsDirs", []) or []
    roots += [Path(os.path.expanduser(str(p))) for p in (extra if isinstance(extra, list) else [])]
    out = []
    for root in roots:
        if root.is_file():
            out.append(root)
        elif root.is_dir():
            out += sorted(p for p in root.rglob("*.vcf") if p.is_file())
    return list(dict.fromkeys(out))


def _value(params: dict, value: str) -> str:
    if params.get("ENCODING", "").upper() == "QUOTED-PRINTABLE":   # phones still export this old form
        value = quopri.decodestring(value).decode(params.get("CHARSET", "utf-8"), "replace")
    return unescape(value).strip()


def _label(text: str) -> str:
    """An address book's own label for a relation (`_$!<Mother>!$_`) as a word."""
    m = re.fullmatch(r"_\$!<(.+)>!\$_", text.strip())
    return (m[1] if m else text).strip().lower()


def cards() -> list[dict]:
    out = []
    for path in files():
        try:
            lines = unfold(path.read_text(errors="replace"))
        except OSError:
            continue
        card, groups = None, {}
        for line in lines:
            name, params, value = split_line(line)
            group, _, prop = name.rpartition(".")   # "item1.TEL": the group ties a value to its label
            if prop == "BEGIN" and value.strip().upper() == "VCARD":
                card, groups = {"name": "", "nickname": "", "organization": "", "emails": [], "phones": [], "related": []}, {}
            elif card is None:
                continue
            elif prop == "END" and value.strip().upper() == "VCARD":
                for g, who in groups.items():
                    if isinstance(who, dict) and who.get("value"):
                        card["related"].append({"label": _label(who.get("label", "")) or "related", "value": who["value"]})
                if not card["name"]:
                    card["name"] = card.pop("_n", "") or card["organization"]
                card.pop("_n", None)
                if card["name"]:
                    out.append(card)
                card = None
            elif prop == "FN": card["name"] = _value(params, value)
            elif prop == "N":
                last, first, *_ = (_value(params, value).split(";") + ["", ""])
                card["_n"] = " ".join(filter(None, [first.strip(), last.strip()]))
            elif prop == "NICKNAME": card["nickname"] = _value(params, value)
            elif prop == "ORG": card["organization"] = _value(params, value).replace(";", " ").strip()
            elif prop == "EMAIL": card["emails"].append(_value(params, value))
            elif prop == "TEL": card["phones"].append(re.sub(r"(?i)^tel:", "", _value(params, value)))
            elif prop == "RELATED":
                card["related"].append({"label": params.get("TYPE", "related").lower(), "value": re.sub(r"(?i)^urn:uuid:.*", "", _value(params, value))})
            elif prop == "X-ABRELATEDNAMES": groups.setdefault(group, {})["value"] = _value(params, value)
            elif prop == "X-ABLABEL" and group: groups.setdefault(group, {})["label"] = _value(params, value)
    for i, c in enumerate(out):
        c["index"] = i
        c["related"] = [r for r in c["related"] if r["value"]]
    return out


def account_name() -> str:
    try:
        return pwd.getpwuid(os.getuid()).pw_gecos.split(",")[0].strip()
    except (KeyError, OSError):
        return ""


def own_card(all_cards: list[dict] | None = None) -> dict | None:
    wanted = str(_setting("myCard", "") or account_name()).casefold().strip()
    if not wanted:
        return None
    return next((c for c in (all_cards if all_cards is not None else cards()) if c["name"].casefold() == wanted), None)


def _git_email() -> str:
    try:
        return subprocess.run(["git", "config", "--global", "user.email"], capture_output=True, text=True, timeout=2).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def mail_accounts() -> list[dict]:
    """Addresses the mail program is set up with (Thunderbird keeps them in prefs.js)."""
    found = []
    for prefs in sorted(Path.home().glob(".thunderbird/*/prefs.js")):
        try:
            text = prefs.read_text(errors="replace")
        except OSError:
            continue
        for address in re.findall(r'user_pref\("mail\.identity\.id\d+\.useremail", "([^"]+)"\)', text):
            if address not in [a for f in found for a in f["emails"]]:
                found.append({"name": address.split("@")[-1], "emails": [address]})
    return found


def profile() -> dict:
    """What can be known about the user without asking: the shape the brain's
    profile import reads ({contacts, mail, errors, errorReasons})."""
    card = own_card() or {}
    name = card.get("name") or account_name()
    emails = list(dict.fromkeys(card.get("emails", []) + ([_git_email()] if _git_email() else [])))
    contacts = {"name": name, "pronouns": "", "birthday": "",
                "emails": [{"label": "", "value": e} for e in emails],
                "phones": [{"label": "", "value": p} for p in card.get("phones", [])],
                "urls": [], "addresses": [], "organization": card.get("organization", ""), "department": "", "jobTitle": "",
                "social": [], "related": card.get("related", [])} if name or emails else None
    mail = mail_accounts()
    errors = [k for k, v in (("contacts", contacts), ("mail", mail)) if not v]
    return {"contacts": contacts, "mail": mail or None, "errors": errors, "errorReasons": {k: "read" for k in errors}}

"""Smart home through Home Assistant's REST API.

Alexa and Google Home talk to their own clouds; the open equivalent is a Home
Assistant server, which already speaks to nearly every light, plug, thermostat
and lock. Atticus needs two settings to use one:

    "haUrl":   "http://homeassistant.local:8123"
    "haToken": "<a long-lived access token from your HA profile page>"

Devices are addressed by the name the user gave them in Home Assistant ("Luz
da sala"), matched loosely so "a luz da sala" and "sala" both find it. Nothing
here guesses: an unknown or ambiguous name comes back as such.
"""
from __future__ import annotations

import json
import re
import unicodedata
import urllib.error
import urllib.request

from . import settings

# what can be switched, and the service that does it
SWITCHABLE = ("light", "switch", "fan", "input_boolean", "media_player", "climate", "humidifier", "vacuum", "scene", "script", "cover", "lock")
_ON = {"cover": "open_cover", "lock": "unlock", "scene": "turn_on", "script": "turn_on", "vacuum": "start"}
_OFF = {"cover": "close_cover", "lock": "lock", "vacuum": "return_to_base"}


class HomeError(Exception):
    pass


def configured() -> bool:
    return bool(settings.get("haUrl") and settings.get("haToken"))


def _call(method: str, path: str, body: dict | None = None, timeout: float = 8):
    url, token = str(settings.get("haUrl") or "").rstrip("/"), settings.get("haToken")
    if not url or not token:
        raise HomeError("Home Assistant is not set up: add haUrl and haToken to ~/.config/atticus/settings.json")
    req = urllib.request.Request(url + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        e.close()
        raise HomeError("Home Assistant refused the token" if e.code in (401, 403) else f"Home Assistant answered {e.code}")
    except (OSError, ValueError) as e:
        raise HomeError(f"could not reach Home Assistant: {getattr(e, 'reason', e)}")


def _fold(s: str) -> str:
    s = "".join(c for c in unicodedata.normalize("NFKD", str(s).lower()) if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9 ]", " ", s)


_FILLER = {"a", "o", "as", "os", "da", "do", "de", "das", "dos", "the", "of", "in", "no", "na", "my", "meu", "minha"}
_KIND = {"luz": "light", "luzes": "light", "lampada": "light", "lampadas": "light", "light": "light", "lights": "light",
         "lamp": "light", "tomada": "switch", "plug": "switch", "interruptor": "switch", "switch": "switch",
         "ventilador": "fan", "fan": "fan", "ar": "climate", "condicionado": "climate", "termostato": "climate",
         "thermostat": "climate", "tv": "media_player", "televisao": "media_player", "cortina": "cover",
         "persiana": "cover", "blinds": "cover", "curtain": "cover", "porta": "lock", "fechadura": "lock", "lock": "lock",
         "aspirador": "vacuum", "vacuum": "vacuum", "cena": "scene", "scene": "scene"}


def entities(domain: str = "") -> list[dict]:
    rows = _call("GET", "/api/states") or []
    out = []
    for r in rows:
        eid = str(r.get("entity_id", ""))
        dom = eid.split(".")[0]
        if dom not in SWITCHABLE and dom not in ("sensor", "binary_sensor", "weather") or domain and dom != domain:
            continue
        attrs = r.get("attributes") or {}
        out.append({"id": eid, "domain": dom, "name": str(attrs.get("friendly_name") or eid), "state": str(r.get("state", "")),
                    "unit": str(attrs.get("unit_of_measurement") or ""), "attrs": attrs})
    return out


def find(spoken: str, rows: list[dict] | None = None, switchable: bool = True) -> list[dict]:
    """Entities a spoken name could mean, best first. One result = found;
    several = the caller must ask which; none = unknown."""
    rows = entities() if rows is None else rows
    if switchable:
        rows = [r for r in rows if r["domain"] in SWITCHABLE]
    words = [w for w in _fold(spoken).split() if w not in _FILLER]
    if not words:
        return []
    kinds = {_KIND[w] for w in words if w in _KIND}
    names = [w for w in words if w not in _KIND] or words
    exact = [r for r in rows if _fold(r["name"]).split() == _fold(spoken).split() or r["id"] == spoken.strip()]
    if exact:
        return exact[:1]
    hits = []
    for r in rows:
        have = set(_fold(r["name"]).split()) | set(_fold(r["id"].split(".", 1)[-1].replace("_", " ")).split())
        if all(any(h == n or h.startswith(n) and len(n) >= 4 for h in have) for n in names):
            hits.append(r)
    # A kind word ("luz", "ar") narrows the candidates, but never to nothing: "iluminação do ar
    # condicionado" is a switch that belongs to the air conditioner, not a climate device.
    of_kind = [r for r in hits if r["domain"] in kinds]
    if kinds and of_kind:
        hits = of_kind
    elif kinds and len(names) == len(words):   # only kind words were said and nothing of that kind exists
        hits = []
    if len(hits) > 1:
        # Several candidates: the one whose own name carries every word said ("luz do quarto" is
        # "Luz do Quarto", not another light that merely sits in the quarto area) wins outright.
        named = [r for r in hits if all(w in _fold(r["name"]).split() for w in words)]
        if len(named) == 1:
            return named
    return hits


def switch(entity: dict, on: bool | None) -> None:
    """on=True/False, or None to toggle."""
    dom = entity["domain"]
    if entity.get("state") == "unavailable":
        # Home Assistant accepts the call and nothing happens: say so instead of claiming it worked
        raise HomeError(f"{entity['name']} is offline (Home Assistant shows it as unavailable)")
    if on is None:
        service = "toggle"
    else:
        service = (_ON if on else _OFF).get(dom, "turn_on" if on else "turn_off")
    if dom in ("scene", "script") and on is False:
        raise HomeError("a scene can only be turned on")
    _call("POST", f"/api/services/{dom}/{service}", {"entity_id": entity["id"]})


def state_of(entity_id: str) -> dict:
    r = _call("GET", f"/api/states/{entity_id}") or {}
    return {"state": str(r.get("state", "")), "attrs": r.get("attributes") or {}}


def confirm(entity: dict, on: bool | None = None, what: str = "", value: float = 0, wait: float = 3.0) -> bool:
    """Home Assistant answers 200 to a command whether or not the device obeyed
    (a bulb that just dropped off Wi-Fi accepts it and stays as it was). Read
    the state back until it shows the change, so the reply is what happened."""
    import time
    dom, before = entity["domain"], entity.get("state")
    if dom in ("scene", "script"):
        return True   # momentary: there is no state to read back
    end = time.monotonic() + wait
    while True:
        try:
            now = state_of(entity["id"])
        except HomeError:
            return False
        s, a = now["state"], now["attrs"]
        if s == "unavailable":
            return False
        if what == "brightness":
            got = a.get("brightness")
            if got is not None and abs(got / 255 * 100 - value) <= 3:
                return True
        elif what == "temperature":
            if a.get("temperature") is not None and abs(float(a["temperature"]) - value) < 0.6:
                return True
        elif what == "volume":
            if a.get("volume_level") is not None and abs(float(a["volume_level"]) * 100 - value) <= 3:
                return True
        elif what == "position":
            if a.get("current_position") is not None and abs(float(a["current_position"]) - value) <= 3:
                return True
        elif on is None:
            if s != before:
                return True
        else:
            off_states = {"off", "closed", "locked", "docked", "idle", "standby"} if dom != "media_player" else {"off", "standby"}
            if dom == "lock":
                is_on = s == "unlocked"
            elif dom == "cover":
                is_on = s in ("open", "opening")
            else:
                is_on = s not in off_states
            if is_on == on:
                return True
        if time.monotonic() >= end:
            return False
        time.sleep(0.15)   # answer as soon as the device reports the change


def set_value(entity: dict, what: str, value: float) -> None:
    dom = entity["domain"]
    if entity.get("state") == "unavailable":
        raise HomeError(f"{entity['name']} is offline (Home Assistant shows it as unavailable)")
    if what == "brightness" and dom == "light":
        _call("POST", "/api/services/light/turn_on", {"entity_id": entity["id"], "brightness_pct": max(1, min(100, int(value)))})
    elif what == "temperature" and dom == "climate":
        _call("POST", "/api/services/climate/set_temperature", {"entity_id": entity["id"], "temperature": float(value)})
    elif what == "volume" and dom == "media_player":
        _call("POST", "/api/services/media_player/volume_set", {"entity_id": entity["id"], "volume_level": max(0.0, min(1.0, value / 100))})
    elif what == "position" and dom == "cover":
        _call("POST", "/api/services/cover/set_cover_position", {"entity_id": entity["id"], "position": max(0, min(100, int(value)))})
    else:
        raise HomeError(f"{entity['name']} has no {what} to set")


def main(argv: list[str]) -> int:
    """--home list [domain] | state "<name>" | on|off|toggle "<name>" | set "<name>" brightness|temperature|volume|position <n>"""
    usage = 'usage: --home list [domain] | state "<name>" | on|off|toggle "<name>" | set "<name>" brightness|temperature|volume|position <number>'
    try:
        if not argv or argv[0] == "list":
            rows = entities(argv[1] if len(argv) > 1 else "")
            print("\n".join(f"{r['id']} · {r['name']} · {r['state']}{(' ' + r['unit']) if r['unit'] else ''}" for r in rows) or "no devices")
            return 0
        verb = argv[0]
        if verb in ("on", "off", "toggle", "state") and len(argv) >= 2:
            name = " ".join(argv[1:])
            hits = find(name, switchable=verb != "state")
        elif verb == "set" and len(argv) == 4:
            name = argv[1]
            hits = find(name)
        else:
            print(usage)
            return 1
        if not hits:
            print(f'no device named "{name}"; run --home list')
            return 1
        if len(hits) > 1:
            print(f'"{name}" matches several devices; ask which: ' + "; ".join(h["name"] for h in hits[:8]))
            return 1
        e = hits[0]
        if verb == "state":
            print(f"{e['name']}: {e['state']}{(' ' + e['unit']) if e['unit'] else ''}")
            return 0
        if verb == "set":
            try:
                value = float(argv[3].rstrip("%").replace(",", "."))
            except ValueError:
                print(usage)
                return 1
            set_value(e, argv[2], value)
            if not confirm(e, what=argv[2], value=value):
                print(f"{e['name']} accepted the command but did not change (it may be offline or slow); tell the user it did not respond")
                return 1
            print(f"{e['name']}: {argv[2]} is now {argv[3]} (confirmed)")
            return 0
        want = {"on": True, "off": False, "toggle": None}[verb]
        switch(e, want)
        if not confirm(e, on=want):
            print(f"{e['name']} accepted the command but did not change (it may be offline or slow); tell the user it did not respond")
            return 1
        print(f"{e['name']}: {verb} done (confirmed)")
        return 0
    except HomeError as err:
        print(str(err))
        return 1

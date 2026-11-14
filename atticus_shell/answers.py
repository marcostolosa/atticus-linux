"""Map a spoken reply to the answer of an on-screen card.

Permission cards take yes / no; choice cards take a number, an ordinal or
words from an option's label. Unclear replies return None — the caller asks a
permission again, and takes a choice reply as the user's own words — and for a permission, anything that sounds like a
refusal wins over anything that sounds like consent.
"""
from __future__ import annotations

import re


def plain(text) -> str:
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", str(text))
    s = re.sub(r"[*_`#>~]", "", s)
    return re.sub(r"\s+", " ", s).strip()


# Portuguese/English function words, excluded from the fuzzy choice
# match below so a reply and an option label never "match" on nothing
# but an article or preposition they both happen to contain.
_STOPWORDS = {
    "a", "o", "os", "as", "de", "da", "do", "das", "dos", "e", "é",
    "um", "uma", "uns", "umas", "no", "na", "nos", "nas", "por",
    "para", "com", "que", "se", "ao", "aos", "à", "às", "em", "ou",
    "mas", "the", "a", "of", "to", "is", "it", "and", "or", "in",
}

def match(p, text, lang=""):
    # Map a spoken reply to a card answer, or None when it is unclear.
    import re
    t = re.sub(r"[.!?,;:]+", " ", text.lower())
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        return None
    yes = {"sim", "pode", "pode sim", "claro", "ok", "okay", "isso",
           "permitir", "permito", "aprovar", "manda", "vai", "yes", "allow"}
    no = {"nao", "não", "cancelar", "cancela", "negar", "nega", "pare",
          "deny", "nunca", "negativo", "nope"}
    # "no" and "para" are also everyday Portuguese words ("abre no
    # Chrome", "para mim"), so they only mean no as the whole reply — or,
    # when the user is speaking English, as its first word ("no, don't").
    english = str(lang or "").lower().startswith("en")
    bare_no = t in ("no", "para", "no thanks") or (english and t.split()[0] == "no")
    none = {"nenhum", "nenhuma", "nenhum deles", "nenhuma delas",
            "outra", "outra coisa", "none", "nada disso"}
    words = set(t.split())
    if p["kind"] == "perm":
        # Check "no" first: a negative reply ("não pode", "claro que
        # não", "ok, não") contains a yes-word too, and must not be
        # read as approval just because "pode"/"claro"/"ok" also
        # matched.
        if words & no or t in no or bare_no:
            return "n"
        if words & yes or t in yes:
            return "r" if p.get("retry") else "y"
        return None
    # choice card
    if t in none or words & none:
        return ""
    ordinals = {"primeiro": 0, "primeira": 0, "um": 0, "uma": 0,
                "segundo": 1, "segunda": 1, "dois": 1, "duas": 1,
                "terceiro": 2, "terceira": 2, "tres": 2, "três": 2,
                "quarto": 3, "quarta": 3, "quatro": 3,
                "quinto": 4, "quinta": 4, "cinco": 4,
                "sexto": 5, "sexta": 5, "seis": 5}
    opts = p.get("options") or []
    m = re.search(r"\b(\d+)\b", t)
    if m:
        i = int(m.group(1)) - 1
        if 0 <= i < len(opts):
            return str(i)
    for w in words:
        if w in ordinals and ordinals[w] < len(opts):
            return str(ordinals[w])
    # Fuzzy: the option whose label shares the most content words
    # (stopwords excluded) with the reply.
    content = words - _STOPWORDS
    scores = []
    for o in opts:
        label = set(re.sub(r"[^\w\s]", " ", plain(o).lower()).split())
        scores.append(len((label - _STOPWORDS) & content))
    best = max(scores, default=0)
    # One option must stand out, and most of what was said must be in it:
    # "liga o ar" against "Verificar o ar" / "Desligar o ar" is neither of
    # them — it is the user's own answer, which the caller passes on as said.
    if best >= 1 and scores.count(best) == 1 and best * 2 >= len(content):
        return str(scores.index(best))
    return None

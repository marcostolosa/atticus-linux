"""Instant intents: everyday commands answered with no AI call.

A voice assistant is judged on "set a timer for ten minutes" and "volume up",
and those must not take a model round trip. This module recognizes a small set
of unambiguous commands in Brazilian Portuguese and English and runs them
locally in milliseconds. Anything it is not sure about returns None and goes to
the model, which has the same abilities as tools — so a miss here costs time,
never correctness.

parse(text) is pure (no side effects, takes `now`) so it can be unit-tested;
run(intent) performs the action and returns the line to show and speak.
"""
from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass, field


@dataclass
class Intent:
    kind: str
    lang: str = "pt"
    args: dict = field(default_factory=dict)


# --- text normalization ------------------------------------------------------
def fold(s: str) -> str:
    """Lowercase, accents removed. Same length as the input for Portuguese
    text, so a span found here can be cut from the accented original."""
    out = []
    for ch in s.lower():
        base = "".join(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c))
        out.append(base if len(base) == 1 else ch)
    return "".join(out)


def clean(s: str) -> str:
    """Punctuation a transcript adds ("Timer de 10 minutos.") out of the way;
    ':' '%' and '.' inside numbers/domains stay."""
    s = re.sub(r"[!?¿¡,;\"“”]+", " ", s)
    s = re.sub(r"\.(?=\s|$)", " ", s)
    return re.sub(r"\s+", " ", s).strip()


_PT_NUM = {"zero": 0, "um": 1, "uma": 1, "dois": 2, "duas": 2, "tres": 3, "quatro": 4, "cinco": 5, "seis": 6,
           "sete": 7, "oito": 8, "nove": 9, "dez": 10, "onze": 11, "doze": 12, "treze": 13, "catorze": 14,
           "quatorze": 14, "quinze": 15, "dezesseis": 16, "dezessete": 17, "dezoito": 18, "dezenove": 19,
           "vinte": 20, "trinta": 30, "quarenta": 40, "cinquenta": 50, "sessenta": 60, "setenta": 70,
           "oitenta": 80, "noventa": 90, "cem": 100}
_EN_NUM = {"zero": 0, "one": 1, "a": 1, "an": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
           "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
           "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
           "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
           "hundred": 100}
_NUM_WORD = "|".join(sorted(set(_PT_NUM) | set(_EN_NUM), key=len, reverse=True))
# "25", "vinte e cinco", "twenty five", "meia" is handled where it applies (half an hour)
NUM = rf"(?:\d+(?:[.,]\d+)?|(?:{_NUM_WORD})(?:(?: e | |-)(?:{_NUM_WORD}))?)"


def number(s: str) -> float | None:
    s = s.strip()
    if re.fullmatch(r"\d+(?:[.,]\d+)?", s):
        return float(s.replace(",", "."))
    total, seen = 0, False
    for w in re.split(r" e | |-", s):
        v = _PT_NUM.get(w, _EN_NUM.get(w))
        if v is None:
            return None
        total, seen = total + v, True
    return float(total) if seen else None


# --- durations and clock times -----------------------------------------------
_UNITS = {"s": 1, "seg": 1, "segs": 1, "segundo": 1, "segundos": 1, "second": 1, "seconds": 1, "sec": 1, "secs": 1,
          "m": 60, "min": 60, "mins": 60, "minuto": 60, "minutos": 60, "minute": 60, "minutes": 60,
          "h": 3600, "hr": 3600, "hrs": 3600, "hora": 3600, "horas": 3600, "hour": 3600, "hours": 3600}
_UNIT = "|".join(sorted(_UNITS, key=len, reverse=True))
DURATION = rf"(?:{NUM} ?(?:{_UNIT})\b(?:(?: e | and | )(?:meia|half|{NUM} ?(?:{_UNIT})\b))*|meia hora|half an hour)"


def duration(s: str) -> float | None:
    """ "1 hora e meia", "90 segundos", "2 minutos e 30 segundos" -> seconds."""
    s = s.strip()
    if s in ("meia hora", "half an hour"):
        return 1800.0
    total, last_unit = 0.0, None
    pos = 0
    for m in re.finditer(rf"({NUM}) ?({_UNIT})\b|\b(meia|half)\b", s):
        if m.group(3):
            if last_unit is None:
                return None
            total += last_unit / 2
        else:
            n = number(m.group(1))
            if n is None:
                return None
            last_unit = _UNITS[m.group(2)]
            total += n * last_unit
        pos = m.end()
    return total if total > 0 and pos else None


_WEEKDAYS = {"segunda": 0, "segunda-feira": 0, "terca": 1, "terca-feira": 1, "quarta": 2, "quarta-feira": 2,
             "quinta": 3, "quinta-feira": 3, "sexta": 4, "sexta-feira": 4, "sabado": 5, "domingo": 6,
             "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6}
_WD = "|".join(sorted(_WEEKDAYS, key=len, reverse=True))
_DAY = rf"(?:hoje|amanha|depois de amanha|today|tomorrow|(?:(?:na|no|on|next|proxima|proximo) )?(?:{_WD}))"
_PERIOD = r"(?:da manha|de manha|da tarde|a tarde|de tarde|da noite|a noite|de noite|da madrugada|am|pm|a\.m\.|p\.m\.)"
_CLOCK = (rf"(?:(?:as|a|at|para as|pras|para|pra|by) )?"
          rf"(?:(?P<noon>meio[- ]dia|noon|midday)|(?P<midnight>meia[- ]noite|midnight)|"
          rf"(?P<h>\d{{1,2}}|{_NUM_WORD})(?:(?::|h| ?horas? e | e | )(?P<mi>\d{{1,2}}|meia|half|{_NUM_WORD}(?: e (?:{_NUM_WORD}))?))?"
          rf"(?: ?(?:h|horas?|hrs?|o'?clock))?(?: e (?P<half>meia))?)"
          rf"(?: ?(?P<period>{_PERIOD}))?")
_REPEAT = r"(?:todo dia|todos os dias|diariamente|every day|daily|(?:em |nos |de |on )?(?:dias de semana|dias uteis|weekdays))"
_RELATIVE = rf"(?:(?:daqui a|daqui|em|in) (?P<rel>{DURATION}))"


@dataclass
class When:
    at: float
    repeat: str = ""
    explicit_day: bool = False


def _clock(m: re.Match, now: float, day_word: str = "", repeat: str = "", literal: bool = False) -> When | None:
    """Resolve a matched clock time to the next moment it happens."""
    period = m.group("period") or ""
    if m.group("noon"):
        h, mi = 12, 0
    elif m.group("midnight"):
        h, mi = 0, 0
    else:
        hv = number(m.group("h"))
        if hv is None or hv > 23:
            return None
        h, mi = int(hv), 0
        raw = m.group("mi")
        if raw in ("meia", "half") or m.group("half"):
            mi = 30
        elif raw:
            mv = number(raw)
            if mv is None or mv > 59:
                return None
            mi = int(mv)
    # "às 7" with no morning/evening word: for a reminder it is whichever 7 comes next; an alarm or a
    # repeating time is taken as the clock says it (a wake-up at 7 is 07:00).
    ambiguous = not (m.group("noon") or m.group("midnight")) and not period and 1 <= h <= 12 and not literal and not repeat
    if period:
        if re.match(r"da tarde|a tarde|de tarde|pm|p\.m\.", period) and h < 12:
            h += 12
        elif re.match(r"da noite|a noite|de noite", period) and 5 <= h < 12:
            h += 12
        elif re.match(r"da noite|a noite|de noite", period) and h == 12:
            h = 0
        elif re.match(r"am|a\.m\.|da manha|de manha|da madrugada", period) and h == 12:
            h = 0
    lt = time.localtime(now)

    def on(day_offset, hour):
        return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + day_offset, hour, mi, 0, 0, 0, -1))

    day_word = day_word.strip()
    explicit = bool(day_word)
    if day_word in ("hoje", "today"):
        offset = 0
    elif day_word in ("amanha", "tomorrow"):
        offset = 1
    elif day_word == "depois de amanha":
        offset = 2
    elif day_word:
        wd = _WEEKDAYS.get(day_word.split()[-1])
        if wd is None:
            return None
        offset = (wd - lt.tm_wday) % 7
        if offset == 0 and on(0, h) <= now:
            offset = 7
    else:
        offset = None
    if offset is not None:
        # a named day with a bare "às 7": people mean the morning unless it is today and already past
        at = on(offset, h)
        if ambiguous and at <= now and h < 12:
            at = on(offset, h + 12)
        if at <= now and not repeat:
            return None
        return When(_weekday(at, repeat), repeat, explicit)
    # no day: the next time the clock shows it. "às 7" said at 22h is 7 tomorrow; said at 10h it is 19h today.
    cands = [on(d, hh) for d in (0, 1) for hh in ([h, h + 12] if ambiguous and h < 12 else [h])]
    future = sorted(c for c in cands if c > now + 20)
    return When(_weekday(future[0], repeat), repeat, False) if future else None


def _weekday(at: float, repeat: str) -> float:
    """A weekdays-only time that landed on a weekend starts on Monday."""
    while repeat == "weekdays" and time.localtime(at).tm_wday >= 5:
        lt = time.localtime(at)
        at = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + 1, lt.tm_hour, lt.tm_min, 0, 0, 0, -1))
    return at


def find_when(folded: str, now: float, literal: bool = False, bare: bool = False) -> tuple[When, tuple[int, int]] | None:
    """The first time expression in the text and the span it covers:
    "daqui a 20 minutos", "amanhã às 9", "às 7 e meia da noite", "todo dia às 8"."""
    m = re.search(rf"\b{_RELATIVE}", folded)
    if m:
        secs = duration(m.group("rel"))
        if secs:
            return When(now + secs), m.span()
    pat = (rf"(?:(?P<rep1>{_REPEAT}) )?(?:(?P<d1>{_DAY}) )?(?<![\w:])(?P<clock>{_CLOCK})(?![\w:%])"
           rf"(?: (?P<d2>{_DAY}))?(?: (?P<rep2>{_REPEAT}))?")
    for m in re.finditer(pat, folded):
        # a bare number is only a time when something marks it as one ("às 9", "9h", "9 da manhã", "amanhã 9")
        text = m.group("clock")
        marked = bool(re.match(r"(as|a|at|para as|pras|by) ", text) or re.search(r"\d ?(h|horas?|hrs?|o'?clock)\b|:\d\d", text)
                      or m.group("period") or m.group("noon") or m.group("midnight") or m.group("d1") or m.group("d2")
                      or m.group("rep1") or m.group("rep2"))
        if not marked and not bare:   # bare: the caller knows a time is expected here ("me acorda 6 e meia")
            continue
        rep = m.group("rep1") or m.group("rep2") or ""
        repeat = "" if not rep else "weekdays" if re.search(r"semana|uteis|weekdays", rep) else "daily"
        day = m.group("d1") or m.group("d2") or ""
        day = re.sub(r"^(na|no|on|next|proxima|proximo) ", "", day)
        when = _clock(m, now, day, repeat, literal)
        if when:
            return when, m.span()
    return None


# --- the grammar -------------------------------------------------------------
_PLEASE = r"(?:(?:por favor|por gentileza|pfv|please|ai|aí|entao|então|agora|now|pra mim|para mim|for me) ?)*"
_SET = (r"(?:(?:coloca|coloque|colocar|poe|ponha|por|bota|bote|botar|cria|crie|criar|inicia|inicie|iniciar|comeca|comece|"
        r"marca|marque|marcar|define|defina|definir|ativa|ative|programa|programe|faz|faca|liga|ligue|"
        r"set|start|create|make|add|put|begin) )")
_ART = r"(?:(?:um|uma|o|a|the|an|a|me|my|meu|minha|novo|nova|new|another|outro|outra) )*"
_TIMER = r"(?:timer|temporizador|cronometro|contagem regressiva|countdown)"
_ALARM = r"(?:alarme|despertador|alarm)"


_HEARD_AS = {"coloco": "coloca", "ponho": "poe", "boto": "bota", "crio": "cria", "inicio": "inicia", "marco": "marca",
             "aumento": "aumenta", "abaixo": "abaixa", "baixo": "baixa", "diminuo": "diminui", "ligo": "liga",
             "desligo": "desliga", "apago": "apaga", "acendo": "acende", "pauso": "pausa", "adiciono": "adiciona",
             "abro": "abre", "cancelo": "cancela", "tiro": "tira", "limpo": "limpa", "muto": "muta", "pulo": "pula",
             "deixo": "deixa", "mudo": "muda", "ajusto": "ajusta", "bloqueio": "bloqueia", "anoto": "anota"}


def _lang(folded: str) -> str:
    en = len(re.findall(r"\b(the|set|timer for|what|turn|please|volume up|volume down|remind me|wake me|open|next|pause|play|"
                        r"stop|mute|unmute|brightness|minutes?|hours?|seconds?|at|tomorrow|every)\b", folded))
    pt = len(re.findall(r"\b(o|a|de|um|uma|que|para|pra|me|coloca|abre|abrir|aumenta|diminui|volume em|minutos?|horas?|"
                        r"segundos?|as|amanha|todo|lembra|lembre|acorda|proxima|pausa|toca|para|brilho|quais|qual)\b", folded))
    return "en" if en > pt else "pt"


def parse(text: str, now: float | None = None, home: bool | None = None) -> Intent | None:
    """`home`: whether a smart-home server is configured (None = look it up).
    Without one, "liga a luz" is not an instant command and goes to the model."""
    now = time.time() if now is None else now
    original = clean(text)
    t = fold(original)
    t = re.sub(rf"^(?:(?:ei|hey|oi|ok|okay|ola) )?(?:siri|atticus|aticus)\b ?", "", t)   # the wake word may be transcribed too
    original = original[len(original) - len(t):]
    t0 = t
    t = re.sub(rf"^{_PLEASE}", "", t)
    t = re.sub(rf" ?{_PLEASE}$", "", t).strip()
    if not t or len(t) > 160:
        return None
    # Speech recognition often hears the command's last vowel wrong ("coloco um timer" for "coloca um
    # timer"), most of all in a whisper. A request to the assistant is never "I do X", so a first word
    # in the first person is read as the command it sounds like.
    first, _, rest = t.partition(" ")
    if first in _HEARD_AS:
        t = _HEARD_AS[first] + (" " + rest if rest else "")
    lang = _lang(t)
    I = lambda kind, **args: Intent(kind, lang, args)

    # --- stop talking ---
    if re.fullmatch(r"(para|pare|parar|chega|silencio|cala a boca|fica quieto|stop|quiet|shut up|be quiet|para de falar|stop talking)", t):
        return I("hush")

    # --- time and date ---
    if re.fullmatch(r"(que horas sao|que hora e|que horas e|quantas horas|me diz as horas|me diga as horas|horas|"
                    r"what time is it|what's the time|whats the time|what is the time|time)( agora| now)?", t):
        return I("time")
    if re.fullmatch(r"(que dia e hoje|qual (e )?(o dia|a data) de hoje|qual e a data|que data e hoje|em que dia estamos|"
                    r"what day is it( today)?|what's the date( today)?|what is the date( today)?|what's today's date)", t):
        return I("date")

    # --- timers ---
    m = re.fullmatch(rf"{_SET}?{_ART}{_TIMER} (?:de|para|pra|por|for|of|em|in) (?P<d>{DURATION})"
                     rf"(?: (?:para|pra|de|do|da|for|called|chamado|named) (?:o |a |the )?(?P<label>.+))?", t)
    m = m or re.fullmatch(rf"{_SET}?{_ART}(?P<d>{DURATION}) (?:de |no |on (?:the )?|of )?{_TIMER}", t)
    m = m or re.fullmatch(rf"(?:conta|conte|contar|count(?: down)?) (?P<d>{DURATION})", t)
    m = m or re.fullmatch(rf"(?:me avisa|me avise|me chama|me chame|avisa|avise) (?:daqui a|daqui|em) (?P<d>{DURATION})", t)
    if m:
        secs = duration(m.group("d"))
        if secs and 1 <= secs <= 24 * 3600:
            label = (m.groupdict().get("label") or "").strip()
            return I("timer", seconds=secs, label=_slice(original, t0, t, m, "label") if label else "")
        return None

    # --- alarms ---
    m = re.fullmatch(rf"(?:me acorda|me acorde|acorda eu|me desperta|me desperte|wake me(?: up)?) (?P<rest>.+)", t)
    m = m or re.fullmatch(rf"{_SET}?{_ART}{_ALARM} (?P<rest>.+)", t)
    if m:
        found = find_when(m.group("rest"), now, literal=True, bare=True)
        if found:
            when, (a, b) = found
            left = (m.group("rest")[:a] + " " + m.group("rest")[b:]).strip()
            left = re.sub(r"^(para|pra|for|to|de|at|em|no|na) ?", "", left).strip()
            if not left or left in ("para", "pra", "for"):
                return I("alarm", at=when.at, repeat=when.repeat, label="")
        return None

    # --- reminders ---
    m = re.match(r"(?:me lembra|me lembre|lembra eu|lembre-me|lembra-me|me faz lembrar|cria um lembrete|criar um lembrete|"
                 r"crie um lembrete|coloca um lembrete|adiciona um lembrete|lembrete|remind me|set a reminder|"
                 r"add a reminder|create a reminder) ?(?P<rest>.+)", t)
    if m:
        rest = m.group("rest")
        offset = len(original) - len(t0) + (len(t0) - len(t0.lstrip())) + t0.find(t) + m.start("rest")
        rest_orig = original[offset:offset + len(rest)] if fold(original[offset:offset + len(rest)]) == rest else rest
        found = find_when(rest, now)
        if not found:
            return None   # "me lembra de comprar pão" with no time: the model asks when
        when, (a, b) = found
        what = (rest_orig[:a] + " " + rest_orig[b:]).strip()
        what = re.sub(r"^(?:de|do|da|que|para|pra|to|that|about|of|sobre)\s+", "", what, flags=re.I)
        what = re.sub(r"\s+(?:de|que|para|pra|to|that|e|and)$", "", what, flags=re.I)
        what = re.sub(r"\s+", " ", what).strip(" .:-")
        what = re.sub(r"^(?:de|que|para|pra|to|that)\s+", "", what, flags=re.I)
        if len(what) < 2:
            return None
        return I("reminder", at=when.at, repeat=when.repeat, label=what)

    # --- list / cancel ---
    things = rf"(?P<what>{_TIMER}s?|timers|temporizadores|{_ALARM}s?|alarmes|alarms|lembretes?|reminders?)"
    if re.fullmatch(rf"(?:quais|qual|que|lista|liste|listar|mostra|mostre|me mostra|tem|tenho|what|which|list|show(?: me)?|do i have)"
                    rf"(?: (?:sao|e|os|as|meus|minhas|algum|alguma|any|my|the|are|is|todos|todas|all))* {things}"
                    rf"(?: (?:ativos?|ativas?|pendentes?|eu tenho|tenho|i have|active|set|marcados?|programados?))?", t) or \
            re.fullmatch(rf"(?:quanto (?:tempo )?falta|quanto tempo resta|how (?:much time|long) (?:is )?left)"
                         rf"(?: (?:no|do|pro|para o|on (?:the|my)) (?:{_TIMER}))?", t):
        return I("alarms")
    m = re.fullmatch(rf"(?:cancela|cancele|cancelar|para|pare|parar|remove|remova|remover|apaga|apague|apagar|desliga|desligue|"
                     rf"desligar|desativa|desative|tira|tire|cancel|stop|remove|delete|clear|turn off)"
                     rf"(?: (?P<all>todos os|todas as|todos|todas|all(?: the| my)?))?(?: (?:o|a|os|as|meu|minha|meus|minhas|the|my))? {things}"
                     rf"(?: (?:de|do|da|for|called) (?:o |a |the )?(?P<label>.+))?", t)
    if m:
        what = m.group("what")
        kind = "timer" if re.match(_TIMER[3:-1].split("|")[0] + "|temporizador|cronometro|countdown|contagem", what) else \
            "alarm" if re.match(r"alarm|despertador", what) else "reminder"
        plural = bool(m.group("all")) or what.endswith("s") and not what.endswith("ss")
        return I("cancel", spec=(m.group("label") or "").strip() or (kind if plural else ""), what=kind)
    if re.fullmatch(r"(?:cancela|cancele|cancelar|apaga|remove|cancel|clear|delete) (?:tudo|todos|all|everything)", t):
        return I("cancel", spec="all", what="")

    # --- volume ---
    vol = r"(?:o |a |the |do |no )?(?:volume|som|audio|sound)"
    m = re.fullmatch(rf"(?:(?:coloca|coloque|poe|ponha|bota|deixa|deixe|muda|mude|ajusta|ajuste|define|set|put|change|turn) )?{vol}"
                     rf" (?:em|para|pra|a|no|at|to) (?P<n>{NUM}) ?(?:%|por cento|percent)?", t)
    m = m or re.fullmatch(rf"{vol} (?P<n>{NUM}) ?(?:%|por cento|percent)?", t)
    if m:
        n = number(m.group("n"))
        if n is not None and 0 <= n <= 100:
            return I("volume", percent=int(n))
        return None
    step = rf"(?: (?:em |by )?(?P<n>{NUM}) ?(?:%|por cento|percent)?| (?P<bit>um pouco|um pouquinho|mais|a bit|a little|more))?"
    m = re.fullmatch(rf"(?:aumenta|aumente|aumentar|sobe|suba|subir|levanta|levante|mais|raise|increase|turn up|louder)(?: mais)?"
                     rf"(?: {vol})?{step}", t) if re.search(r"volume|som|audio|sound|louder", t) or \
        re.fullmatch(r"(mais alto|louder)", t) else None
    m = m or re.fullmatch(rf"{vol} (?:up|para cima|mais alto){step}", t)
    if m or re.fullmatch(r"mais alto|louder", t):
        n = number(m.group("n")) if m and m.groupdict().get("n") else None
        return I("volume", delta=int(n) if n else 10)
    m = re.fullmatch(rf"(?:abaixa|abaixe|abaixar|baixa|baixe|baixar|diminui|diminua|diminuir|reduz|reduza|reduzir|menos|lower|"
                     rf"decrease|reduce|turn down|quieter)(?: {vol})?{step}", t) if re.search(r"volume|som|audio|sound|quieter", t) else None
    m = m or re.fullmatch(rf"{vol} (?:down|para baixo|mais baixo){step}", t)
    if m or re.fullmatch(r"mais baixo|quieter", t):
        n = number(m.group("n")) if m and m.groupdict().get("n") else None
        return I("volume", delta=-(int(n) if n else 10))
    if re.fullmatch(rf"(?:muta|mute|mutar|silencia|silencie|silenciar|tira o som|sem som|desliga o som|corta o som)(?: {vol}| o computador| tudo)?", t):
        return I("volume", mute=True)
    if re.fullmatch(rf"(?:desmuta|desmute|desmutar|unmute|volta o som|liga o som|ativa o som|com som)(?: {vol})?", t):
        return I("volume", mute=False)
    if re.fullmatch(rf"(?:qual (?:e |esta )?|como esta |what(?:'s| is) )?{vol}(?: atual| agora| current| now)?", t) and \
            re.match(r"qual|como|what", t):
        return I("volume")

    # --- brightness ---
    bri = r"(?:o |a |the |do |da )?(?:brilho|brightness|luminosidade)(?: da tela| do monitor| of the screen)?"
    m = re.fullmatch(rf"(?:(?:coloca|coloque|poe|ponha|bota|deixa|deixe|muda|mude|ajusta|ajuste|define|set|put|change|turn) )?{bri}"
                     rf" (?:em|para|pra|a|no|at|to) (?P<n>{NUM}) ?(?:%|por cento|percent)?", t)
    if m:
        n = number(m.group("n"))
        if n is not None and 1 <= n <= 100:
            return I("brightness", percent=int(n))
        return None
    if re.fullmatch(rf"(?:aumenta|aumente|aumentar|sobe|suba|mais|raise|increase|turn up)(?: mais)? {bri}(?: um pouco| a bit)?|{bri} (?:up|mais alto)", t):
        return I("brightness", delta=10)
    if re.fullmatch(rf"(?:abaixa|abaixe|baixa|baixe|diminui|diminua|reduz|reduza|menos|lower|decrease|reduce|turn down|dim) {bri}(?: um pouco| a bit)?"
                    rf"|{bri} (?:down|mais baixo)|dim the screen|escurece a tela", t):
        return I("brightness", delta=-10)

    # --- media ---
    music = r"(?: (?:a |o |the |essa |esse |esta |este |this |that |minha |my )?(?:musica|som|video|filme|podcast|player|music|song|track|faixa|playback|reproducao|spotify|youtube))"
    if re.fullmatch(rf"(?:pausa|pause|pausar|para|pare|parar|stop){music}|pausa|pause|pausar", t):
        return I("media", action="pause")
    if re.fullmatch(rf"(?:continua|continue|continuar|retoma|retome|retomar|volta|despausa|resume|unpause|play|toca|toque|tocar|solta|da play)"
                    rf"{music}?", t) and t not in ("volta",):
        return I("media", action="play")
    if re.fullmatch(rf"(?:proxima|proximo|pula|pule|pular|avanca|next|skip)(?:{music}| (?:essa|esta|this)(?: (?:musica|faixa|song|track))?)?"
                    rf"|(?:proxima|next) (?:musica|faixa|song|track)|pula essa", t):
        return I("media", action="next")
    if re.fullmatch(rf"(?:anterior|volta|voltar|previous|back)(?:{music})|(?:musica|faixa|song|track) anterior|"
                    rf"(?:previous|last) (?:song|track)|volta (?:uma|a) (?:musica|faixa)", t):
        return I("media", action="previous")
    if re.fullmatch(r"(?:que|qual) (?:musica|som|faixa) (?:e essa|esta tocando|ta tocando|e esse|e esta|toca agora)|"
                    r"(?:o que|oq) (?:esta|ta) tocando|what(?:'s| is) (?:playing|this song)(?: now)?|what song is this", t):
        return I("playing")

    # --- radios, lock ---
    m = re.fullmatch(r"(?P<verb>liga|ligue|ligar|ativa|ative|ativar|habilita|desliga|desligue|desligar|desativa|desative|desativar|"
                     r"turn on|turn off|enable|disable) (?:o |a |the )?(?P<what>wi-?fi|wifi|bluetooth)", t)
    m = m or re.fullmatch(r"(?:turn )?(?P<what>wi-?fi|wifi|bluetooth) (?P<verb>on|off)", t)
    if m:
        on = bool(re.match(r"lig|ativ|habilit|turn on|enable|on$", m.group("verb")))
        return I("radio", which="bluetooth" if "blue" in m.group("what") else "wifi", on=on)
    if re.fullmatch(r"(?:bloqueia|bloqueie|bloquear|tranca|tranque|trancar|lock)(?: (?:a |o |the |my )?(?:tela|computador|pc|screen|computer))?", t):
        return I("lock")

    # --- lists ---
    lname = r"(?:(?:a |the |my |minha )?(?:lista(?: (?:de|do|da) (?P<ln>[\w -]{2,24}?))?|(?P<ln2>[\w-]{2,20}) list|list))"
    m = re.fullmatch(rf"(?:adiciona|adicione|adicionar|coloca|coloque|colocar|poe|ponha|bota|bote|inclui|inclua|anota|anote|acrescenta|"
                     rf"add|put|include) (?P<items>.+?) (?:na|a|em|para a|pra|to|on|in|into) {lname}", t)
    if m:
        raw = _slice(original, t0, t, m, "items")
        items = [re.sub(r"^(?:o|a|os|as|um|uma|uns|umas|the|some) ", "", x.strip(), flags=re.I)
                 for x in re.split(r"\s+(?:e|and|mais|plus)\s+", raw) if x.strip()]
        if items and all(1 < len(x) <= 60 for x in items):
            return I("list_add", name=m.group("ln") or m.group("ln2") or "compras", items=items)
        return None
    m = re.fullmatch(rf"(?:o que (?:tem|ha|esta)|oq tem|quais (?:sao )?os itens|le|leia|ler|mostra|mostre|me mostra|me fala|fala|diz|"
                     rf"qual e|what(?:'s| is)|read|show(?: me)?|tell me)(?: (?:na|a|da|em|on|in|the|what's on|o que tem na))* {lname}", t)
    if m:
        return I("list_show", name=m.group("ln") or m.group("ln2") or "compras")
    m = re.fullmatch(rf"(?:tira|tire|tirar|remove|remova|remover|risca|risque|riscar|apaga|apague|apagar|delete|cross off|take) "
                     rf"(?P<item>.+?) (?:da|de|na|from|off|off of) {lname}", t)
    if m:
        item = re.sub(r"^(?:o|a|os|as|the) ", "", _slice(original, t0, t, m, "item").strip(), flags=re.I)
        return I("list_remove", name=m.group("ln") or m.group("ln2") or "compras", item=item)
    m = re.fullmatch(rf"(?:limpa|limpe|limpar|esvazia|esvazie|esvaziar|zera|zere|clear|empty) {lname}", t)
    if m:
        return I("list_clear", name=m.group("ln") or m.group("ln2") or "compras")

    # --- smart home ---
    if home is None:
        try:
            from . import home as _home
            home = _home.configured()
        except Exception:
            home = False
    if home:
        # "coloca o ar em 24 graus", "luz da sala em 50%"
        m = re.fullmatch(rf"(?:(?:coloca|coloque|poe|ponha|bota|deixa|deixe|ajusta|ajuste|muda|mude|define|set|put|change) )?"
                         rf"(?P<name>[\w -]{{2,48}}?) (?:em|e|para|pra|a|no|at|to) (?P<n>{NUM}) ?(?P<unit>graus|grau|degrees|°c?|%|por cento|percent)", t)
        if m and not re.search(r"\b(volume|som|brilho|brightness|timer|alarme|tela|screen)\b", m.group("name")):
            n = number(m.group("n"))
            if n is not None:
                temp = bool(re.match(r"grau|degree|°", m.group("unit")))
                if temp and 10 <= n <= 35 or not temp and 1 <= n <= 100:
                    return I("home_set", name=m.group("name").strip(), what="temperature" if temp else "brightness", value=n)
        m = re.fullmatch(r"(?P<verb>liga|ligue|ligar|acende|acenda|acender|ativa|ative|abre|abra|turn on|switch on|"
                         r"desliga|desligue|desligar|apaga|apague|apagar|desativa|desative|fecha|feche|turn off|switch off) "
                         r"(?P<name>[\w -]{2,48})", t)
        m2 = None if m else re.fullmatch(r"(?:turn |switch )?(?P<name>[\w -]{2,48}) (?P<verb>on|off)", t)
        m = m or m2
        if m and not re.search(r"\b(computador|pc|computer|tela|screen|pasta|arquivo|file|folder|alarme|timer|lembrete|som|volume)\b", m.group("name")):
            on = bool(re.match(r"lig|acend|ativ|abr|turn on|switch on|on$", m.group("verb")))
            return I("home", name=m.group("name").strip(), on=on)

    # --- open an app or a site ---
    m = re.fullmatch(r"(?:abre|abra|abrir|abri|inicia|inicie|iniciar|executa|execute|roda|lanca|open(?: up)?|launch|start|run)"
                     r" (?:(?:o|a|os|as|the|my|meu|minha|app|aplicativo|programa) )*(?P<name>[\w .+-]{2,40}?)(?: app| application)?", t)
    if m:
        name = m.group("name").strip()
        joiners = {"e", "and", "no", "na", "em", "on", "in", "com", "with", "para", "to", "do", "da", "de", "of", "que", "then", "depois"}
        if not joiners & set(name.split()):
            return I("open", name=name)
    return None


def _slice(original: str, t0: str, t: str, m: re.Match, group: str) -> str:
    """Group text with its accents, cut from the original by position."""
    start = len(original) - len(t0) + t0.find(t) + m.start(group)
    piece = original[start:start + (m.end(group) - m.start(group))]
    return piece if fold(piece) == m.group(group) else m.group(group)


# --- doing it ---------------------------------------------------------------
def _span(secs: float, lang: str) -> str:
    secs = int(round(secs))
    h, rem = divmod(secs, 3600)
    mi, s = divmod(rem, 60)
    if lang == "pt":
        parts = ([f"{h} hora{'s' if h != 1 else ''}"] if h else []) + ([f"{mi} minuto{'s' if mi != 1 else ''}"] if mi else []) + \
                ([f"{s} segundo{'s' if s != 1 else ''}"] if s and not h else [])
        return " e ".join(parts) or "0 segundos"
    parts = ([f"{h} hour{'s' if h != 1 else ''}"] if h else []) + ([f"{mi} minute{'s' if mi != 1 else ''}"] if mi else []) + \
            ([f"{s} second{'s' if s != 1 else ''}"] if s and not h else [])
    return " and ".join(parts) or "0 seconds"


_PT_DAYS = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]
_EN_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_PT_MONTHS = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro",
              "novembro", "dezembro"]
_EN_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
              "November", "December"]


def when_text(at: float, lang: str, now: float | None = None, repeat: str = "") -> str:
    """ "hoje às 15:00", "amanhã às 09:00", "sexta às 07:30", "todo dia às 08:00"."""
    now = time.time() if now is None else now
    lt, nt = time.localtime(at), time.localtime(now)
    clock = time.strftime("%H:%M", lt)
    days = (time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 12, 0, 0, 0, 0, -1)) -
            time.mktime((nt.tm_year, nt.tm_mon, nt.tm_mday, 12, 0, 0, 0, 0, -1))) / 86400
    days = int(round(days))
    if lang == "pt":
        if repeat:
            return ("todo dia" if repeat == "daily" else "nos dias de semana") + f" às {clock}"
        day = "hoje" if days == 0 else "amanhã" if days == 1 else _PT_DAYS[lt.tm_wday] if days < 7 else \
            f"dia {lt.tm_mday} de {_PT_MONTHS[lt.tm_mon - 1]}"
        return f"{day} às {clock}"
    if repeat:
        return ("every day" if repeat == "daily" else "on weekdays") + f" at {clock}"
    day = "today" if days == 0 else "tomorrow" if days == 1 else _EN_DAYS[lt.tm_wday] if days < 7 else \
        f"{_EN_MONTHS[lt.tm_mon - 1]} {lt.tm_mday}"
    return f"{day} at {clock}"


def run(i: Intent, hush=None) -> str | None:
    """Do it. Returns the reply, or None to hand the request to the model
    after all (the app was not found, a tool is missing in a way the model can
    work around, ...)."""
    from . import assistant as A
    pt = i.lang == "pt"
    a = i.args
    if i.kind == "hush":
        if hush:
            hush()
        return ""
    if i.kind == "time":
        return time.strftime("São %H:%M." if pt else "It's %H:%M.")
    if i.kind == "date":
        lt = time.localtime()
        return (f"Hoje é {_PT_DAYS[lt.tm_wday]}, {lt.tm_mday} de {_PT_MONTHS[lt.tm_mon - 1]} de {lt.tm_year}." if pt else
                f"Today is {_EN_DAYS[lt.tm_wday]}, {_EN_MONTHS[lt.tm_mon - 1]} {lt.tm_mday}, {lt.tm_year}.")
    if i.kind == "timer":
        row, err = A.add("timer", time.time() + a["seconds"], a.get("label") or _span(a["seconds"], i.lang), lang=i.lang,
                         seconds=a["seconds"])
        if not row:
            return (f"Não consegui criar o timer: {err}." if pt else f"I couldn't set the timer: {err}.")
        return (f"Timer de {_span(a['seconds'], 'pt')} iniciado." if pt else f"Timer set for {_span(a['seconds'], 'en')}.")
    if i.kind in ("alarm", "reminder"):
        row, err = A.add(i.kind, a["at"], a.get("label", ""), a.get("repeat", ""), lang=i.lang)
        if not row:
            return (f"Não consegui criar: {err}." if pt else f"I couldn't set it: {err}.")
        w = when_text(row["at"], i.lang, repeat=row["repeat"])
        if i.kind == "alarm":
            return (f"Alarme marcado para {w}." if pt else f"Alarm set for {w}.")
        return (f"Vou te lembrar {w}: {row['label']}." if pt else f"I'll remind you {w}: {row['label']}.")
    if i.kind == "alarms":
        rows = A.alarms()
        if not rows:
            return "Não há timers, alarmes nem lembretes ativos." if pt else "There are no timers, alarms or reminders set."
        now, out = time.time(), []
        for r in rows[:6]:
            if r["kind"] == "timer":
                out.append((f"timer {('de ' + r['label'] + ' ') if r['label'] else ''}com {_span(max(0, r['at'] - now), 'pt')} restantes" if pt else
                            f"{r['label'] + ' ' if r['label'] else ''}timer with {_span(max(0, r['at'] - now), 'en')} left"))
            else:
                name = {"alarm": "alarme" if pt else "alarm", "reminder": "lembrete" if pt else "reminder"}[r["kind"]]
                out.append(f"{name} {when_text(r['at'], i.lang, now, r.get('repeat', ''))}" + (f" ({r['label']})" if r["label"] else ""))
        more = len(rows) - len(out)
        text = "; ".join(out) + (f"; e mais {more}" if more > 0 and pt else f"; and {more} more" if more > 0 else "")
        return text[0].upper() + text[1:] + "."
    if i.kind == "cancel":
        spec = a.get("spec") or ""
        if not spec and a.get("what"):
            # "cancela o timer": the soonest of that kind
            soonest = next((r for r in A.alarms() if r["kind"] == a["what"]), None)
            gone = A.cancel(soonest["id"]) if soonest else []
        else:
            gone = A.cancel(spec)
        if not gone:
            return "Não havia nada para cancelar." if pt else "There was nothing to cancel."
        n = len(gone)
        if n == 1:
            name = {"timer": "Timer", "alarm": "Alarme" if pt else "Alarm", "reminder": "Lembrete" if pt else "Reminder"}[gone[0]["kind"]]
            return f"{name} cancelado." if pt else f"{name} cancelled."
        return f"{n} cancelados." if pt else f"{n} cancelled."
    if i.kind == "volume":
        if not a:
            v = A.volume()
        else:
            v = A.set_volume(a.get("percent"), a.get("delta"), a.get("mute"))
        if v is None:
            return None
        pct, muted = v
        if muted:
            return f"Som mudo (volume em {pct}%)." if pt else f"Muted (volume at {pct}%)."
        return f"Volume em {pct}%." if pt else f"Volume at {pct}%."
    if i.kind == "brightness":
        b = A.set_brightness(a.get("percent"), a.get("delta"))
        if b is None:
            return None
        return f"Brilho em {b}%." if pt else f"Brightness at {b}%."
    if i.kind == "media":
        ok, err = A.media(a["action"])
        if not ok:
            if a["action"] == "play":
                return None   # nothing to resume: maybe they meant "play <something>", which the model handles
            return ("Não há nada tocando." if pt else "Nothing is playing.") if "nothing" in err else None
        return {"pause": "Pausado." if pt else "Paused.", "play": "Tocando." if pt else "Playing.",
                "next": "Próxima." if pt else "Next.", "previous": "Anterior." if pt else "Previous."}[a["action"]]
    if i.kind == "playing":
        p = A.now_playing()
        if not p or not p["title"]:
            return "Não há nada tocando." if pt else "Nothing is playing."
        by = f" — {p['artist']}" if p["artist"] else ""
        return f"{p['title']}{by}."
    if i.kind == "radio":
        if not A.radio(a["which"], a["on"]):
            return None
        name = "Wi-Fi" if a["which"] == "wifi" else "Bluetooth"
        return (f"{name} {'ligado' if a['on'] else 'desligado'}." if pt else f"{name} {'on' if a['on'] else 'off'}.")
    if i.kind == "lock":
        return ("Tela bloqueada." if pt else "Screen locked.") if A.lock() else None
    if i.kind == "list_add":
        added = A.list_add(a["name"], a["items"])
        key = A.list_key(a["name"])
        if not added:
            return (f"Isso já estava na lista de {key}." if pt else f"That was already on the {key} list.")
        joined = (" e " if pt else " and ").join(added)
        return (f"Adicionei {joined} à lista de {key}." if pt else f"Added {joined} to the {key} list.")
    if i.kind == "list_show":
        rows, key = A.list_items(a["name"]), A.list_key(a["name"])
        if not rows:
            return (f"A lista de {key} está vazia." if pt else f"The {key} list is empty.")
        body = ", ".join(rows[:-1]) + ((" e " if pt else " and ") + rows[-1] if len(rows) > 1 else rows[0])
        return (f"Na lista de {key}: {body}." if pt else f"On the {key} list: {body}.")
    if i.kind == "list_remove":
        gone, key = A.list_remove(a["name"], a["item"]), A.list_key(a["name"])
        if not gone:
            return (f"Não achei {a['item']} na lista de {key}." if pt else f"I couldn't find {a['item']} on the {key} list.")
        return (f"Tirei {gone[0]} da lista de {key}." if pt else f"Removed {gone[0]} from the {key} list.")
    if i.kind == "list_clear":
        n, key = A.list_clear(a["name"]), A.list_key(a["name"])
        return (f"Lista de {key} limpa." if pt else f"Cleared the {key} list.") if n else \
            (f"A lista de {key} já estava vazia." if pt else f"The {key} list was already empty.")
    if i.kind == "home":
        from . import home as H
        try:
            hits = H.find(a["name"])
            if len(hits) != 1:
                return None   # unknown or ambiguous: the model lists the devices and asks which
            H.switch(hits[0], a["on"])
            if not H.confirm(hits[0], on=a["on"]):
                return (f"{hits[0]['name']} não respondeu." if pt else f"{hits[0]['name']} did not respond.")
        except H.HomeError as e:
            if "offline" in str(e):
                return (f"{hits[0]['name']} está fora do ar no Home Assistant." if pt else f"{hits[0]['name']} is offline in Home Assistant.")
            return (f"Não consegui falar com a casa: {e}." if pt else f"I couldn't reach the smart home: {e}.")
        state = {"cover": ("aberta", "fechada", "open", "closed"), "lock": ("destrancada", "trancada", "unlocked", "locked")}.get(
            hits[0]["domain"], ("ligado", "desligado", "on", "off"))
        word = state[(0 if a["on"] else 1) + (0 if pt else 2)]
        return f"{hits[0]['name']}: {word}."
    if i.kind == "home_set":
        from . import home as H
        try:
            hits = H.find(a["name"])
            if len(hits) != 1:
                return None
            if hits[0].get("state") == "unavailable":
                return (f"{hits[0]['name']} está fora do ar no Home Assistant." if pt else f"{hits[0]['name']} is offline in Home Assistant.")
            H.set_value(hits[0], a["what"], a["value"])
            if not H.confirm(hits[0], what=a["what"], value=a["value"]):
                return (f"{hits[0]['name']} não respondeu." if pt else f"{hits[0]['name']} did not respond.")
        except H.HomeError:
            return None   # this device has no such setting: the model works out what was meant
        v = int(a["value"]) if float(a["value"]).is_integer() else a["value"]
        if a["what"] == "temperature":
            return f"{hits[0]['name']} em {v} graus." if pt else f"{hits[0]['name']} set to {v} degrees."
        return f"{hits[0]['name']} em {v}%." if pt else f"{hits[0]['name']} at {v}%."
    if i.kind == "open":
        name = a["name"]
        app = A.find_app(name)
        if app and A.launch(app):
            return f"Abrindo {app['name']}." if pt else f"Opening {app['name']}."
        site = name.replace(" ponto ", ".").replace(" dot ", ".").replace(" ", "")
        if re.fullmatch(r"(?:https?://)?[\w-]+(?:\.[\w-]+)+(?:/\S*)?", site) and not re.search(r"\.(pdf|docx?|xlsx?|txt|csv|png|jpe?g|zip|mp[34]|md|py|sh)$", site):
            url = site if site.startswith("http") else "https://" + site
            if A.open_url(url):
                return f"Abrindo {site}." if pt else f"Opening {site}."
        return None
    return None

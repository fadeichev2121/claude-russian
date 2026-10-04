"""Compile known translations into exact fragments and bounded UI templates."""
import itertools
import re

TAGS = re.compile(r"</?([A-Za-z][A-Za-z0-9]*)>")
TOKEN = re.compile(r"\{([A-Za-z_][A-Za-z_0-9.-]*)\}")
LIMIT = 128
NUMERIC_SLOTS = {"count", "minutes", "hours", "days", "seconds", "day", "hour", "minute", "total", "done", "skipped", "failed", "recovered", "others"}
SHORT_UI_SOURCES = {
    "from {source}", "by {author}", "by {owner}", "added {when}", "{tier} plan",
    "Uploaded {date}.", "Saved {date}", "By {name}", "Edited {relativeTime}",
    "Created {relativeTime}", "Viewed {relativeTime}", "Remove {name}"
}


def parse(text):
    """Small ICU parser: arguments, number/date formats, plural and select."""
    nodes = []
    literal = []
    def flush():
        if literal:
            nodes.append(("text", "".join(literal)))
            literal.clear()
    index = 0
    while index < len(text):
        if text[index] != "{":
            literal.append(text[index]); index += 1; continue
        flush()
        start = index + 1
        depth = 1
        index += 1
        while index < len(text) and depth:
            depth += (text[index] == "{") - (text[index] == "}")
            index += 1
        if depth:
            raise ValueError("Unclosed ICU argument")
        content = text[start:index - 1]
        parts = content.split(",", 2)
        name = parts[0].strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9.-]*", name):
            raise ValueError("Unsupported ICU argument")
        kind = parts[1].strip() if len(parts) > 1 else ""
        if kind not in {"plural", "select", "selectordinal"}:
            nodes.append(("arg", name))
            continue
        if len(parts) != 3:
            raise ValueError("Missing ICU branches")
        rest = parts[2].strip()
        if rest.startswith("offset:"):
            raise ValueError("Offset not supported")
        choices = {}
        pos = 0
        while pos < len(rest):
            match = re.match(r"\s*([=A-Za-z0-9_-]+)\s*\{", rest[pos:])
            if not match:
                raise ValueError("Invalid ICU branch")
            key = match[1]
            body = pos + match.end()
            pos = body
            depth = 1
            while pos < len(rest) and depth:
                depth += (rest[pos] == "{") - (rest[pos] == "}")
                pos += 1
            if depth:
                raise ValueError("Unclosed ICU branch")
            choices[key] = parse(rest[body:pos - 1])
        nodes.append(("choice", name, kind, choices))
    flush()
    return nodes


def expand(nodes, selections=None, plural=None, traced=None):
    variants = [("", {} if traced is None else dict(traced))]
    for node in nodes:
        choices = []
        if node[0] == "text":
            value = node[1].replace("''", "'")
            if plural:
                value = value.replace("#", "{" + plural + "}")
            choices = [(value, {})]
        elif node[0] == "arg":
            choices = [("{" + node[1] + "}", {})]
        else:
            _, name, kind, branches = node
            keys = list(branches)
            if selections is not None:
                key = selections.get(name, "other")
                keys = [key if key in branches else "other"]
            for key in keys:
                if key not in branches:
                    continue
                for value, trace in expand(branches[key], selections, name if kind != "select" else plural):
                    trace[name] = key
                    choices.append((value, trace))
        variants = [(a + b, {**ta, **tb}) for (a, ta), (b, tb) in itertools.product(variants, choices)]
        if len(variants) > LIMIT:
            raise ValueError("Too many ICU branches")
    return variants


def fragments(source, target):
    src_tags = [m.group() for m in TAGS.finditer(source)]
    dst_tags = [m.group() for m in TAGS.finditer(target)]
    if src_tags != dst_tags:
        return [(source, target)]
    sources = TAGS.split(source)[::2]
    targets = TAGS.split(target)[::2]
    return list(zip(sources, targets))


def compile_catalog(dictionary):
    exact = {}
    patterns = {}
    pairs = []
    for source, target in dictionary.items():
        pairs.append((source, target))
        if "<" in source:
            pairs.extend(fragments(source, target))
    for source, target in pairs:
        source, target = source.strip(), target.strip()
        if not source or source == target:
            continue
        if "{" not in source and "<" not in source:
            exact.setdefault(source, target)
            continue
        try:
            for english, selections in expand(parse(source)):
                for russian, _ in expand(parse(target), selections):
                    # Rich-text links are translated independently without replacing DOM.
                    for english_piece, russian_piece in fragments(english, russian):
                        en, ru = english_piece.strip(), russian_piece.strip()
                        en = re.sub(r"\s+", " ", en).replace("’", "'").replace("‘", "'")
                        slots = TOKEN.findall(en)
                        if not slots:
                            if en and en != ru and "<" not in en:
                                exact.setdefault(en, ru)
                            continue
                        if "<" in en or not set(TOKEN.findall(ru)).issubset(slots):
                            continue
                        literal = TOKEN.sub("", en)
                        numeric = set(slots).issubset(NUMERIC_SLOTS)
                        safe_short = (numeric and len(literal) >= 3 and re.search(r"[A-Za-z]", literal)) or source in SHORT_UI_SOURCES
                        # Short templates are allowed only for known UI labels or numbers.
                        if not safe_short and (len(literal) < 12 or len(re.findall(r"[A-Za-z]+", literal)) < 2):
                            continue
                        source_parts = TOKEN.split(en)
                        capture_slots = source_parts[1::2]
                        regex = "^" + "".join(
                            re.escape(part).replace(r"\ ", r"\s+") if index % 2 == 0
                            else r"([0-9][0-9,.\s]{0,30})" if part in NUMERIC_SLOTS
                            else r"([^\n]{1,160}?)"
                            for index, part in enumerate(source_parts)
                        ) + "$"
                        prefix = re.match(r"[A-Za-z]+", en)
                        item = {"regex": regex, "slots": capture_slots, "target": ru,
                                "prefix": prefix[0].lower() if prefix else "*"}
                        patterns.setdefault(regex, item)
        except ValueError:
            # Unsupported formats remain unchanged instead of guessing.
            continue
    # Exact full phrases take priority over derived link fragments.
    exact.update({k: v for k, v in dictionary.items() if "{" not in k and "<" not in k})
    return exact, list(patterns.values())

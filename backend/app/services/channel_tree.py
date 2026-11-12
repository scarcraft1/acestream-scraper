"""Channel rules tree: decide a stream's channel, groups and guide from its name.

Pure functions, no database. The tree is a JSON-shaped dict (the same shape the
editor saves) and every stream is classified from its original name, so the
result never depends on earlier runs.

Walk: each node first runs its steps (prepare the name, regex replace, add a
group, set the Kodi name), then tests its children in order; the first child
whose conditions match is entered and the rest are skipped. A node with
children where none matches is where the stream stops (``rest_at``). A leaf
names the channel; ``#`` in a condition captures a loose number and fills the
``#`` of the leaf's channel and guide.

Regex dialect: the editor runs the same tree in the browser, so the patterns we
build ourselves use ASCII word characters (JavaScript's ``\\w``). Patterns the
user types are compiled with Python's ``re`` and case-insensitive; ``$1`` and
``$&`` in replacements are accepted as in JavaScript.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Iterable, Optional

Node = dict[str, Any]

_W = "A-Za-z0-9_"
_QUALITY = re.compile(
    rf"(^|[^{_W}+])(HD|FHD|UHD|SD|4K|8K|HDR|720P?|1080[PI]?|2160P?|(?:240|360|480|576)P)(?![{_W}+])|\*",
    re.IGNORECASE,
)
_CODES = re.compile(
    r"\s*\.\.\.\s*\S*|(^|\s)E-[0-9]+(?=\s|$)|\[[0-9a-f]{4,}\]|\([^)]*\)",
    re.IGNORECASE,
)
_SPACES = re.compile(r"\s+")
_LONE_HASH = re.compile(r"(^|\s)#(\s|$)")
_HASH_NUMBER = "0?([0-9]{1,2})"
# A clock or a date («21:00», «11/11») is not a channel number.
_OPTIONAL_NUMBER = rf"(?:(?:[^{_W}+]+)?0?([0-9]{{1,2}})(?![:./][0-9]))?"
# After a value that ends in a digit the number must be apart: «SPORT 1» is not «SPORT 10» as 1 + 0.
_SEPARATE_NUMBER = rf"(?:[^{_W}+]+0?([0-9]{{1,2}})(?![:./][0-9]))?"
_BAD = object()


def prep_op(text: str, op: str) -> str:
    if op == "upper":
        return text.upper()
    if op == "trim":
        return text.strip()
    if op == "collapse":
        return _SPACES.sub(" ", text)
    if op == "quality":
        text = _QUALITY.sub(r"\1", text)
        text = _CODES.sub(r"\1", text)
        return _SPACES.sub(" ", text).strip()
    return text


def resolution_of(name: str) -> str:
    if re.search(r"2160|4K|UHD", name, re.IGNORECASE):
        return "4K"
    if re.search(r"1080|FHD", name, re.IGNORECASE):
        return "1080p"
    if re.search(r"720|\bHD\b", name, re.IGNORECASE | re.ASCII):
        return "720p"
    return "—"


def js_replacement(repl: str) -> str:
    """Turn a JavaScript replacement (``$1``, ``$&``, ``$$``) into ``re.sub`` syntax."""
    out = []
    i = 0
    while i < len(repl):
        ch = repl[i]
        if ch == "\\":
            out.append("\\\\")
        elif ch == "$" and i + 1 < len(repl):
            nxt = repl[i + 1]
            if nxt == "$":
                out.append("$")
                i += 1
            elif nxt == "&":
                out.append(r"\g<0>")
                i += 1
            elif nxt.isdigit():
                j = i + 1
                while j < len(repl) and repl[j].isdigit() and j - i <= 2:
                    j += 1
                out.append(rf"\g<{repl[i + 1:j]}>")
                i = j - 1
            else:
                out.append(ch)
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _user_regex(pattern: str) -> Optional[re.Pattern[str]]:
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error:
        return None


@lru_cache(maxsize=4096)
def cond_regex(op: str, value: str):
    """Regex for one condition, ``None`` when it can never match, ``_BAD`` when invalid."""
    if op == "regex":
        if not value:
            return None
        return _user_regex(value) or _BAD
    tokens = [_HASH_NUMBER if w == "#" else re.escape(w) for w in value.split()]
    if not tokens:
        return None
    # Without an explicit #, «words» and «starts» also take a loose number after.
    optional = ""
    if op in ("words", "starts") and not _LONE_HASH.search(value):
        optional = _SEPARATE_NUMBER if value.strip()[-1].isdigit() else _OPTIONAL_NUMBER
    try:
        if op in ("words", "not"):
            body = f"[^{_W}+]+".join(tokens) + (optional if op == "words" else "")
            return re.compile(rf"(?<![{_W}+]){body}(?![{_W}+])", re.IGNORECASE)
        if op == "exact":
            return re.compile("^" + r"\s+".join(tokens) + "$", re.IGNORECASE)
        if op == "starts":
            return re.compile("^" + r"\s+".join(tokens) + optional + rf"(?![{_W}+])", re.IGNORECASE)
    except re.error:
        return _BAD
    return None


@dataclass
class TreeState:
    work: str
    kodi: str
    tvg: str = ""
    group: str = ""
    kind: str = "acestream"
    source_list: str = ""
    cid: str = ""
    res: str = "—"
    groups: list[str] = field(default_factory=list)
    num: Optional[str] = None
    path: list[str] = field(default_factory=list)
    seen: dict[str, str] = field(default_factory=dict)
    spans: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    tested: dict[str, str] = field(default_factory=dict)
    leaf: Optional[dict[str, Any]] = None
    rest_at: Optional[str] = None


def _field_value(name: str, st: TreeState) -> str:
    values = {
        "name": st.work,
        "tvg": st.tvg,
        "group": st.group,
        "kind": st.kind,
        "res": st.res,
        "list": st.source_list,
        "num": st.num or "",
        "cid": st.cid,
    }
    return values.get(name, "") or ""


@dataclass
class CondResult:
    ok: bool
    num: Optional[str] = None
    span: Optional[tuple[int, int]] = None


def eval_cond(cond: dict[str, Any], st: TreeState) -> CondResult:
    rx = cond_regex(cond.get("op", ""), cond.get("value", "") or "")
    if rx is None or rx is _BAD:
        return CondResult(False)
    match = rx.search(_field_value(cond.get("field", "name"), st))
    if cond.get("op") == "not":
        return CondResult(match is None)
    if match is None:
        return CondResult(False)
    num = None
    if rx.groups >= 1:
        first = match.group(1)
        if first is not None and first.isdigit() and first.isascii():
            num = str(int(first))
    span = (match.start(), match.end()) if cond.get("field", "name") == "name" else None
    return CondResult(True, num, span)


def _eval_node(node: Node, st: TreeState) -> Optional[tuple[Optional[str], list[tuple[int, int]]]]:
    """``None`` when the node does not match, else the captured number and the name spans."""
    conds = node.get("conds") or []
    if not conds:
        return None, []
    results = [eval_cond(c, st) for c in conds]
    ok = all(r.ok for r in results) if node.get("match") == "all" else any(r.ok for r in results)
    if not ok:
        return None
    hits = [r for r in results if r.ok]
    num = next((r.num for r in hits if r.num is not None), None)
    return num, [r.span for r in hits if r.span]


def fill(template: str, num: Optional[str]) -> str:
    """``X #`` with no number gives ``X``."""
    if num is None:
        return re.sub(r"\s*#", "", template).strip()
    return template.replace("#", num)


# «{nombre}» in a channel template: the stream name without what the publisher appends
# (« --> LIST», « · 1080p · LIST», «...d18» and the code before it), quality, codes or leading ornaments.
_NAME_TAIL = re.compile(r"\s*(-->|\s·\s).*$", re.DOTALL)
_PUBLISHER_ID = re.compile(r"\s+[0-9a-f]{3,4}$", re.IGNORECASE)
_TRAILING_SEPARATORS = re.compile(r"[\s\-|:·,;]+$")
# Promotion links and domains («example.top», «https://…») and quality with a trailing «p» («FHDp», «SDp»).
_PROMO = re.compile(r"(\s)(https?://\S+|\S+\.(top|online|blog|com|net|org|es|tv)(/\S*)?)(?=\s|$)", re.IGNORECASE)
_QUALITY_P = re.compile(r"(^|\s)(F?HD|SD|UHD)P(?=\s|$)", re.IGNORECASE)
NAME_PLACEHOLDER = "{nombre}"
GROUP_PLACEHOLDER = "{grupo}"


def _strip_quality(text: str) -> str:
    return _SPACES.sub(" ", _QUALITY.sub(r"\1", text)).strip()


def clean_name(name: str) -> str:
    text = name or ""
    marked = "-->" in text or "..." in text
    text = _PROMO.sub(r"\1", _NAME_TAIL.sub("", text))
    text = _SPACES.sub(" ", _QUALITY_P.sub(r"\1", _CODES.sub(r"\1", _QUALITY.sub(r"\1", text)))).strip()
    if marked:
        while True:
            shorter = _PUBLISHER_ID.sub("", text)
            if shorter == text or not shorter:
                break
            text = _strip_quality(shorter)
    start = 0
    while start < len(text) and not (text[start].isalnum() or text[start] == "+"):
        start += 1
    return _TRAILING_SEPARATORS.sub("", text[start:])


def fill_leaf(template: str, num: Optional[str], name: str) -> str:
    out = fill(template, num)
    if NAME_PLACEHOLDER not in out:
        return out
    return _SPACES.sub(" ", out.replace(NAME_PLACEHOLDER, clean_name(name))).strip()


def _add_groups(st: TreeState, groups: Optional[str]) -> None:
    # Several groups separated by «;» (GOL goes to Deportes, TDT and Generalistas).
    for g in (groups or "").split(";"):
        # «{grupo}»: the group the stream carries in its list.
        g = g.strip().replace(GROUP_PLACEHOLDER, st.group).strip()
        if g and g not in st.groups:
            st.groups.append(g)


def _apply_steps(node: Node, st: TreeState) -> None:
    for step in node.get("steps") or []:
        cond = step.get("cond")
        if cond and not eval_cond(cond, st).ok:
            continue
        kind = step.get("type")
        if kind == "prep":
            for op in step.get("ops") or []:
                st.work = prep_op(st.work, op)
        elif kind == "repl":
            rx = _user_regex(step.get("pattern", ""))
            if rx is not None:
                st.work = _sub(rx, step.get("repl", ""), st.work, count=1)
        elif kind == "grp":
            _add_groups(st, step.get("group"))
        elif kind == "ren":
            if step.get("mode") == "text" and step.get("value"):
                st.kodi = fill(step["value"], st.num)
            elif step.get("mode") == "regex" and step.get("pattern"):
                rx = _user_regex(step["pattern"])
                if rx is not None:
                    st.kodi = _sub(rx, step.get("repl", ""), st.kodi, count=0)
    _add_groups(st, node.get("group"))


def _sub(rx: re.Pattern[str], repl: str, text: str, count: int) -> str:
    try:
        return rx.sub(js_replacement(repl or ""), text, count=count)
    except (re.error, IndexError):
        return text


def run(root: Node, source: dict[str, Any]) -> TreeState:
    """Classify one stream. ``source`` has ``name`` and optionally ``tvg``, ``group``, ``kind``, ``list``, ``cid``."""
    name = source.get("name") or ""
    st = TreeState(
        work=name,
        kodi=name,
        tvg=source.get("tvg") or "",
        group=source.get("group") or "",
        kind=source.get("kind") or "acestream",
        source_list=source.get("list") or "",
        cid=source.get("cid") or "",
        res=resolution_of(name),
    )

    node = root
    while True:
        _apply_steps(node, st)
        st.path.append(node["id"])
        st.seen[node["id"]] = st.work
        children = node.get("children") or []
        leaf = node.get("leaf")
        if leaf and not children:
            if leaf.get("none"):
                st.leaf = {"node": node["id"], "none": True, "channel": None, "guide": ""}
            else:
                guide = leaf.get("guide") or ""
                st.leaf = {
                    "node": node["id"],
                    "channel": fill_leaf(leaf.get("channel") or "", st.num, st.kodi),
                    # None: no guide, not even the one the streams carry; "": automatic.
                    "guide": None if leaf.get("noGuide") else fill(guide, st.num) if guide else "",
                }
            # Groups set on the channel replace the ones inherited along the path.
            if leaf.get("groups") is not None:
                st.groups = []
                _add_groups(st, leaf["groups"])
            return st
        entered = None
        for child in children:
            st.tested[child["id"]] = st.work
            result = _eval_node(child, st)
            if result is not None:
                num, spans = result
                if num is not None:
                    st.num = num
                st.spans[child["id"]] = spans
                entered = child
                break
        if entered is None:
            if children:
                st.rest_at = node["id"]
            return st
        node = entered


FIELDS = frozenset({"name", "tvg", "group", "kind", "res", "list", "num", "cid"})
_TEXT_KEYS = ("label", "group", "match")
_STEP_TEXT = ("type", "pattern", "repl", "group", "mode", "value")


def tree_problems(root: Any, limit: int = 20) -> list[str]:
    """What keeps ``root`` from being walked; empty when it can be stored.

    Only the shape: a pattern that does not compile is not a problem, the
    condition simply never matches (the editor shows it in red).
    """
    problems: list[str] = []
    seen: set[str] = set()

    def text(value: Any) -> bool:
        return value is None or isinstance(value, str)

    def cond_problem(cond: Any, where: str) -> Optional[str]:
        if not isinstance(cond, dict):
            return f"{where}: a cond is an object"
        if cond.get("field", "name") not in FIELDS:
            return f"{where}: unknown field {cond.get('field')!r}"
        if not (text(cond.get("op")) and text(cond.get("value"))):
            return f"{where}: a cond's op and value are text"
        return None

    def walk(node: Any, where: str, depth: int) -> None:
        if len(problems) >= limit:
            return
        if depth > 64:
            problems.append(f"{where}: the tree is more than 64 levels deep")
            return
        if not isinstance(node, dict):
            problems.append(f"{where}: a node is an object")
            return
        nid = node.get("id")
        if not isinstance(nid, str) or not nid:
            problems.append(f"{where}: a node needs an id")
        elif nid in seen:
            problems.append(f"{where}: id {nid!r} is used twice")
        else:
            seen.add(nid)
            where = nid
        for key in _TEXT_KEYS:
            if not text(node.get(key)):
                problems.append(f"{where}: {key} is text")
        conds = node.get("conds")
        if conds is not None and not isinstance(conds, list):
            problems.append(f"{where}: conds is a list")
        for cond in conds if isinstance(conds, list) else []:
            problem = cond_problem(cond, where)
            if problem:
                problems.append(problem)
        steps = node.get("steps")
        if steps is not None and not isinstance(steps, list):
            problems.append(f"{where}: steps is a list")
        for step in steps if isinstance(steps, list) else []:
            if not isinstance(step, dict) or not all(text(step.get(k)) for k in _STEP_TEXT):
                problems.append(f"{where}: a step is an object with text fields")
                continue
            ops = step.get("ops")
            if ops is not None and not (isinstance(ops, list) and all(isinstance(o, str) for o in ops)):
                problems.append(f"{where}: a step's ops are a list of text")
            if step.get("cond"):
                problem = cond_problem(step["cond"], where)
                if problem:
                    problems.append(problem)
        leaf = node.get("leaf")
        if leaf is not None and not (isinstance(leaf, dict) and all(
                text(leaf.get(k)) for k in ("channel", "guide", "groups"))):
            problems.append(f"{where}: a leaf is an object with text fields")
        children = node.get("children")
        if children is not None and not isinstance(children, list):
            problems.append(f"{where}: children is a list")
            return
        for i, child in enumerate(children or []):
            walk(child, f"{where} > child {i + 1}", depth + 1)

    if not isinstance(root, dict) or not isinstance(root.get("children"), list):
        return ["a tree is a node with a list of children"]
    walk(root, "root", 0)
    return problems[:limit]


def norm(text: str) -> str:
    text = unicodedata.normalize("NFD", text)
    text = re.sub("[̀-ͯ]", "", text).lower()
    # Letters of any script: a Cyrillic name must not normalise to nothing.
    return "".join(ch for ch in text if ch.isalnum() or ch == "+")


def resolve(name: str, names: Iterable[str]) -> dict[str, Any]:
    """Match the leaf's channel against existing ones. «X 1» is «X» when X exists and «X 1» does not."""
    names = list(names)
    key = norm(name)
    hits = [n for n in names if norm(n) == key]
    if len(hits) == 1:
        return {"status": "ok", "name": hits[0]}
    if len(hits) > 1:
        return {"status": "amb", "name": name, "hits": hits}
    m = re.match(r"^(.*\S)\s+0?1$", name)
    if m:
        base_key = norm(m.group(1))
        base = [n for n in names if norm(n) == base_key]
        if len(base) == 1:
            return {"status": "ok", "name": base[0], "implicit_one": True}
        if len(base) > 1:
            return {"status": "amb", "name": name, "hits": base}
    return {"status": "new", "name": name}

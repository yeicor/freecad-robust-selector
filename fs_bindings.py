"""Explicit bindings between semantic selectors and native FreeCAD properties.

No document observer, timer, automatic capture, or selection interception is used.
A binding is created only by an explicit user action.  The persistent relationship
is represented by a normal ``App::PropertyLink`` on the native target object, so
FreeCAD's own dependency graph schedules the selector before its consumer.

The native property remains native.  The robust selector owns the intent; the
native ``FaceN``/``EdgeN`` strings are the current compatibility projection.
"""
from __future__ import annotations

import json
from typing import Any, Iterable, Optional

from fs_freecad import FeatureRef, shape_type_from_subname
from fs_selector import Selector

try:  # pragma: no cover - exercised in FreeCAD
    import FreeCAD as App
except ModuleNotFoundError:  # pragma: no cover - offline unit tests
    App = None


LINK_TYPES = {
    "App::PropertyLink", "App::PropertyLinkList", "PropertyLink", "PropertyLinkList",
    # Whole-object cross-part links share the verified same-document list-of-objects
    # assignment semantics; cross-document sources are refused at write time.
    "App::PropertyXLinkList",
}
LINK_SUB_TYPES = {
    "App::PropertyLinkSub",
    "App::PropertyLinkSubList",
    "App::PropertyLinkSubChild",
    "App::PropertyLinkSubGlobal",
    "App::PropertyLinkSubHidden",
    "App::PropertyLinkSubListChild",
    "App::PropertyLinkSubListGlobal",
    "App::PropertyLinkSubListHidden",
    "PropertyLinkSub",
    "PropertyLinkSubList",
    # Assembly joint references (Reference1/Reference2) and SubShapeBinder
    # supports share the verified same-document (object, subnames) assignment
    # semantics; cross-document sources and dotted subelement paths are refused
    # at write time instead of being silently corrupted.
    "App::PropertyXLinkSub",
    "App::PropertyXLinkSubList",
}
FILLET_EDGE_TYPES = {"Part::PropertyFilletEdges", "PropertyFilletEdges"}
BINDINGS_PROPERTY = "Bindings"
BINDING_STATUS_PROPERTY = "BindingStatus"
TARGET_LINK_PROPERTY = "FeatureSelectorSources"
TARGET_INFO_PROPERTY = "FeatureSelectorBindings"
BINDING_VERSION = 3


def _typename(value: Any) -> str:
    return type(value).__name__


def object_name(obj: Any) -> str:
    return str(getattr(obj, "Name", "") or "")


def object_label(obj: Any) -> str:
    return str(getattr(obj, "Label", "") or object_name(obj))


def document_name(obj: Any) -> str:
    return str(getattr(getattr(obj, "Document", None), "Name", "") or "")


def snapshot_source(source_obj: Any) -> Any:
    """Detach everything background evaluation needs from the live document.

    Returns a plain object exposing the source ``Shape`` (as a detached copy
    that no recompute or viewport render can mutate under the worker),
    ``Name``, and ``Placement`` — the complete touch surface of the planner
    and evaluator.  Evaluating against the live object from a worker thread
    would race the GUI thread; this snapshot makes that impossible by
    construction.  A sourceless or shapeless object raises immediately.
    """
    from types import SimpleNamespace

    name = object_name(source_obj)
    if not name:
        raise ValueError("Cannot snapshot a source object without a name")
    shape = getattr(source_obj, "Shape", None)
    if shape is None or shape.isNull():
        raise ValueError(f"Source '{name}' has no shape to evaluate")
    return SimpleNamespace(
        Shape=shape.copy(),
        Name=name,
        Placement=getattr(source_obj, "Placement", None),
    )


def same_document(first: Any, second: Any) -> bool:
    """True when both objects belong to the same, known document."""
    first_doc = getattr(getattr(first, "Document", None), "Name", None)
    second_doc = getattr(getattr(second, "Document", None), "Name", None)
    return bool(first_doc) and first_doc == second_doc


def _normalize_subnames(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (tuple, list)):
        return [str(x) for x in value if str(x)]
    return [str(value)] if str(value) else []


def _looks_like_link_sub_pair(value: Any) -> bool:
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        return False
    obj = value[0]
    return hasattr(obj, "Name") and (
        isinstance(value[1], (str, tuple, list)) or value[1] is None
    )


def normalize_linksub(value: Any) -> list[tuple[Any, list[str]]]:
    """Normalize the documented LinkSub/LinkSubList Python representations."""
    if _looks_like_link_sub_pair(value):
        obj, names = value
        return [(obj, _normalize_subnames(names))]
    if isinstance(value, (tuple, list)):
        result: list[tuple[Any, list[str]]] = []
        for item in value:
            if _looks_like_link_sub_pair(item):
                obj, names = item
                result.append((obj, _normalize_subnames(names)))
        return result
    return []


def _canonical_refs(refs: Iterable[Any]) -> list[tuple[str, str]]:
    return sorted(
        (
            str(getattr(r, "object_name", "") or object_name(r)),
            str(getattr(r, "subname", "") or ""),
        )
        for r in refs
    )


def _canonical_linksub(value: Any) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for obj, names in normalize_linksub(value):
        out.extend((object_name(obj), name) for name in names)
    return sorted(out)


def linksub_matches(value: Any, refs: Iterable[Any]) -> bool:
    """Exact reference-set match, deliberately order-insensitive."""
    wanted = _canonical_refs(refs)
    actual = _canonical_linksub(value)
    return bool(wanted) and wanted == actual


def linksub_source_matches(value: Any, refs: Iterable[Any]) -> bool:
    """Match only the references belonging to the selector's source object.

    This is used for LinkSubList consumers that may legitimately carry other
    source objects in the same native property.
    """
    wanted = _canonical_refs(refs)
    if not wanted:
        return False
    source_names = {name for name, _ in wanted}
    actual = [pair for pair in _canonical_linksub(value) if pair[0] in source_names]
    return actual == wanted


def _property_names(obj: Any) -> list[str]:
    return [str(n) for n in (getattr(obj, "PropertiesList", []) or [])]


def property_type_name(obj: Any, prop_name: str) -> str:
    """Return FreeCAD's declared property type."""
    getter = getattr(obj, "getTypeIdOfProperty", None)
    if callable(getter):
        try:
            declared = getter(prop_name)
            if declared:
                return str(declared)
        except AttributeError:
            pass
    try:
        value = getattr(obj, prop_name)
        declared = getattr(value, "TypeId", None) or getattr(value, "typeId", None)
        if declared:
            return str(declared() if callable(declared) else declared)
        return _typename(value)
    except AttributeError:
        return ""


def property_mode(obj: Any, prop_name: str) -> Optional[str]:
    declared = property_type_name(obj, prop_name)
    if declared in FILLET_EDGE_TYPES or declared.endswith("::PropertyFilletEdges"):
        return "FilletEdges"
    if declared in LINK_SUB_TYPES:
        return "LinkSub"
    if declared in LINK_TYPES:
        return "Link"
    # Anything else (single App::PropertyXLink, expressions, Extension props)
    # has no verified Python assignment semantics and is refused explicitly.
    return None


def is_property_writable(obj: Any, prop_name: str) -> bool:
    """Return whether a property is writable and not an internal hidden property."""
    getter = getattr(obj, "getEditorMode", None)
    if not callable(getter):
        return hasattr(obj, prop_name)
    try:
        mode = getter(prop_name)
    except (AttributeError, ValueError):
        return hasattr(obj, prop_name)
    if isinstance(mode, (list, tuple, set)):
        return "Hidden" not in mode and 2 not in mode
    if isinstance(mode, int):
        return (mode & 0b10) == 0
    return hasattr(obj, prop_name)


def read_property_value(obj: Any, prop_name: str) -> Any:
    return getattr(obj, prop_name, None)


def _edge_index(subname: str) -> Optional[int]:
    text = str(subname or "")
    if not text.startswith("Edge"):
        return None
    try:
        number = int(text[4:])
    except ValueError:
        return None
    return number if number > 0 else None


def _fillet_base_matches(obj: Any, refs: Iterable[Any]) -> bool:
    source_names = {str(getattr(r, "object_name", "") or object_name(r)) for r in refs}
    if len(source_names) != 1:
        return False
    base = getattr(obj, "Base", None)
    if base is None:
        return False
    if hasattr(base, "Name"):
        return object_name(base) in source_names
    pairs = normalize_linksub(base)
    return bool(pairs) and any(object_name(o) in source_names for o, _ in pairs)


def fillet_edges_match(obj: Any, refs: Iterable[Any], prop_name: str = "Edges") -> bool:
    refs = list(refs)
    if not refs or not _fillet_base_matches(obj, refs):
        return False
    value = read_property_value(obj, prop_name)
    if not isinstance(value, (tuple, list)) or len(value) != len(refs):
        return False
    wanted_ids = [_edge_index(getattr(r, "subname", "")) for r in refs]
    if any(number is None for number in wanted_ids):
        return False
    actual_ids: list[int] = []
    try:
        for item in value:
            if not isinstance(item, (tuple, list)) or not item:
                return False
            actual_ids.append(int(item[0]))
    except Exception:
        return False
    return sorted(actual_ids) == sorted(wanted_ids)


def source_object_from_property(obj: Any, prop_name: str, mode: Optional[str] = None) -> Any:
    mode = mode or property_mode(obj, prop_name)
    if mode == "Link":
        value = read_property_value(obj, prop_name)
        if hasattr(value, "Name"):
            return value
        values = list(value or []) if isinstance(value, (list, tuple)) else []
        sources = [item for item in values if hasattr(item, "Name")]
        return sources[0] if len(sources) == 1 else None
    if mode == "LinkSub":
        pairs = normalize_linksub(read_property_value(obj, prop_name))
        sources = [source for source, names in pairs if names]
        if len(sources) == 1 and all(names for _source, names in pairs):
            return sources[0]
        return None
    if mode == "FilletEdges":
        base = getattr(obj, "Base", None)
        if hasattr(base, "Name"):
            return base
        pairs = normalize_linksub(base)
        if len(pairs) == 1:
            return pairs[0][0]
    return None


def refs_from_property(obj: Any, prop_name: str, mode: Optional[str] = None) -> list[FeatureRef]:
    """Read current native references for diagnostics/explicit binding."""
    mode = mode or property_mode(obj, prop_name)
    if mode == "Link":
        value = read_property_value(obj, prop_name)
        if hasattr(value, "Name"):
            return [FeatureRef(object_name(value), "", "Shape")]
        if isinstance(value, (list, tuple)):
            return [FeatureRef(object_name(item), "", "Shape") for item in value if hasattr(item, "Name")]
        return []
    if mode == "LinkSub":
        pairs = normalize_linksub(read_property_value(obj, prop_name))
        sources = {_obj_name for _obj_name, names in pairs if names}
        if len(sources) != 1 or any(not names for _source, names in pairs):
            return []
        source_obj, _ = next(((source, names) for source, names in pairs if names), (None, []))
        if source_obj is None:
            return []
        return [
            FeatureRef(object_name(source_obj), str(subname), shape_type_from_subname(str(subname)) or "Shape")
            for _source, names in pairs
            for subname in names
        ]
    if mode == "FilletEdges":
        base = getattr(obj, "Base", None)
        source = base if hasattr(base, "Name") else None
        if source is None:
            pairs = normalize_linksub(base)
            if len(pairs) == 1:
                source = pairs[0][0]
        if source is None:
            return []
        value = read_property_value(obj, prop_name)
        if not isinstance(value, (tuple, list)):
            return []
        refs: list[FeatureRef] = []
        for item in value:
            if not isinstance(item, (tuple, list)) or not item:
                return []
            try:
                edge_id = int(item[0])
            except Exception:
                return []
            if edge_id <= 0:
                return []
            refs.append(FeatureRef(object_name(source), f"Edge{edge_id}", "Edge"))
        return refs
    return []


def target_property_candidates(target_obj: Any, source_obj: Any = None) -> list[dict[str, Any]]:
    """Return explicit writable native property choices understood by this workbench."""
    result = []
    source_name = object_name(source_obj) if source_obj is not None else ""
    for prop_name in _property_names(target_obj):
        mode = property_mode(target_obj, prop_name)
        if mode is None or not is_property_writable(target_obj, prop_name):
            continue
        current_source = source_object_from_property(target_obj, prop_name, mode)
        current_name = object_name(current_source) if current_source is not None else ""
        rank = 0 if source_name and current_name == source_name else 1 if not current_name else 2
        result.append({
            "name": prop_name, "mode": mode, "source": current_source,
            "source_name": current_name, "rank": rank, "type": property_type_name(target_obj, prop_name),
        })
    result.sort(key=lambda item: (item["rank"], item["name"]))
    return result


def find_matching_property(target_obj: Any, refs: Iterable[Any]) -> list[tuple[str, str]]:
    """Return supported properties whose current native value exactly matches *refs*."""
    refs = list(refs)
    if not refs:
        return []
    source_names = {str(getattr(r, "object_name", "") or "") for r in refs}
    matches: list[tuple[str, str]] = []
    for candidate in target_property_candidates(target_obj):
        mode = candidate["mode"]
        prop_name = candidate["name"]
        value = read_property_value(target_obj, prop_name)
        if mode == "LinkSub":
            declared = property_type_name(target_obj, prop_name)
            if "List" in declared:
                if linksub_source_matches(value, refs):
                    matches.append((prop_name, mode))
            elif linksub_matches(value, refs):
                matches.append((prop_name, mode))
        elif mode == "FilletEdges" and fillet_edges_match(target_obj, refs, prop_name):
            matches.append((prop_name, mode))
        elif mode == "Link" and len(refs) == 1 and refs[0].subname == "":
            if source_names == {object_name(value)}:
                matches.append((prop_name, mode))
    return matches


def _set_property(obj: Any, prop_name: str, value: Any) -> None:
    setattr(obj, prop_name, value)


def _find_doc_object(doc: Any, name: str) -> Any:
    if doc is None or not name:
        return None
    try:
        return doc.getObject(name)
    except Exception:
        return None


def _require_same_document(obj: Any, source: Any, prop_name: str) -> None:
    """Refuse cross-document writes: selectors only model same-document sources."""
    obj_doc = getattr(obj, "Document", None)
    src_doc = getattr(source, "Document", None)
    obj_name = getattr(obj_doc, "Name", None)
    src_name = getattr(src_doc, "Name", None)
    if obj_name is None or src_name is None or obj_name != src_name:
        raise ValueError(
            f"Cannot bind {object_name(obj)}.{prop_name} to '{object_name(source)}': "
            f"cross-document references are not supported, keep source and consumer in one document"
        )


def write_linksub(
    obj: Any,
    prop_name: str,
    refs: Iterable[Any],
    source: Any = None,
    preserve_other_sources: bool = True,
) -> None:
    """Write LinkSub/LinkSubList using FreeCAD's documented Python representations."""
    refs = list(refs)
    if not refs:
        raise ValueError("Cannot project an empty robust selection into a native property")
    source_names = {str(getattr(r, "object_name", "") or object_name(r)) for r in refs}
    if len(source_names) != 1:
        raise ValueError("This binding currently requires one source object")
    source_name = next(iter(source_names))
    if source is None:
        source = _find_doc_object(getattr(obj, "Document", None), source_name)
    if source is None or object_name(source) != source_name:
        raise ValueError("Source object for robust selection is not available")
    names = [str(getattr(r, "subname", "") or "") for r in refs]
    declared = property_type_name(obj, prop_name)
    if "XLink" in declared:
        _require_same_document(obj, source, prop_name)
    is_list = "List" in declared
    stored = normalize_linksub(read_property_value(obj, prop_name))
    for linked, linked_names in stored:
        if object_name(linked) == source_name and any("." in name for name in linked_names):
            raise ValueError(
                f"Cannot rewrite {object_name(obj)}.{prop_name}: assembly subelement paths "
                f"({linked_names}) have no verified round-trip form; select the part directly"
            )
    if is_list:
        existing = stored if preserve_other_sources else []
        kept = [(linked, linked_names) for linked, linked_names in existing if object_name(linked) != source_name]
        kept.append((source, names))
        _set_property(obj, prop_name, kept)
    else:
        _set_property(obj, prop_name, [source, names])


def write_link(obj: Any, prop_name: str, source: Any) -> None:
    """Write a same-document App::PropertyLink / LinkList."""
    if source is None or not hasattr(source, "Name"):
        raise ValueError("A source object is required")
    declared = property_type_name(obj, prop_name)
    if "XLink" in declared:
        _require_same_document(obj, source, prop_name)
    if "List" in declared:
        current = list(read_property_value(obj, prop_name) or [])
        if source not in current:
            current.append(source)
        _set_property(obj, prop_name, current)
    else:
        _set_property(obj, prop_name, source)


def write_fillet_edges(obj: Any, prop_name: str, refs: Iterable[Any], current_value: Any) -> None:
    """Refresh legacy Part edge IDs without inventing per-edge parameters."""
    refs = list(refs)
    if not isinstance(current_value, (tuple, list)) or len(current_value) != len(refs):
        raise ValueError("Legacy edge property changed cardinality; radius mapping is ambiguous")
    ids = [_edge_index(getattr(r, "subname", "")) for r in refs]
    if any(number is None for number in ids):
        raise ValueError("Legacy edge property can only project EdgeN selections")

    records = {}
    for item in current_value:
        if not isinstance(item, (tuple, list)) or len(item) < 2:
            raise ValueError("Unexpected PropertyFilletEdges record format")
        edge_id = int(item[0])
        records[edge_id] = tuple(item[1:])

    current_ids = set(records)
    wanted_ids = set(ids)
    if current_ids == wanted_ids:
        updated = []
        for edge_id in ids:
            tail = records[edge_id]
            updated.append((int(edge_id), *tail))
        _set_property(obj, prop_name, updated)
        return

    tails = list(records.values())
    if not tails or any(tail != tails[0] for tail in tails[1:]):
        raise ValueError(
            "Legacy Part edge parameters vary per edge; a changed edge identity cannot be "
            "mapped safely. Rebind after changing the native edge set, or make the radii uniform."
        )
    template = tails[0]
    _set_property(obj, prop_name, [(int(edge_id), *template) for edge_id in ids])


def build_proxy_shape(source_obj: Any, refs: Iterable[Any]) -> tuple[Any, list[str]]:
    """Extract exactly the referenced subshapes as one stable proxy shape.

    Returns ``(shape, names)`` with positional subelement names counted per
    kind exactly like an OCCT compound (``Face1..``, ``Edge1..``, ...).
    Empty ``refs`` (an unresolved selector, already reported as a Warning by
    the caller) return ``(None, [])`` so the last good value is kept.
    Anything else that goes wrong raises: a resolved reference that cannot be
    extracted is a real problem and must surface as an error, never as
    silently stale geometry.
    """
    import Part

    wanted = [r for r in refs if str(getattr(r, "subname", "") or "")]
    if not wanted:
        return None, []
    base = source_obj.Shape
    if base.isNull():
        raise ValueError(f"Source '{object_name(source_obj)}' has no shape to extract {len(wanted)} reference(s) from")
    subs = [base.getElement(str(ref.subname)) for ref in wanted]
    for sub in subs:
        if sub is None or sub.isNull():
            raise ValueError(f"Source '{object_name(source_obj)}' lost a referenced subelement")
    counters: dict[str, int] = {}
    names = []
    for ref in wanted:
        kind = str(getattr(ref, "kind", "") or shape_type_from_subname(str(ref.subname)) or "Shape")
        counters[kind] = counters.get(kind, 0) + 1
        names.append(f"{kind}{counters[kind]}" if kind != "Shape" else "")
    if len(subs) == 1:
        return subs[0], names
    return Part.Compound(subs), names


def proxy_native_refs(selector_obj: Any, source_obj: Any, refs: Iterable[Any]) -> list[Any]:
    """Map source ``refs`` onto the selector's stable proxy subelements.

    Returns ``FeatureRef`` objects addressed at the *selector* in evaluation
    order (``Face1..`` etc.).  Empty ``refs`` return ``[]``; extraction
    failures raise.
    """
    wanted = [r for r in refs if str(getattr(r, "subname", "") or "")]
    if not wanted:
        return []
    _shape, names = build_proxy_shape(source_obj, wanted)
    sel_name = object_name(selector_obj)
    native = [
        FeatureRef(sel_name, name, str(getattr(ref, "kind", "") or shape_type_from_subname(name) or "Face"))
        for ref, name in zip(wanted, names)
        if name
    ]
    if len(native) != len(wanted):
        raise ValueError(f"Cannot project {len(wanted)} reference(s) onto '{sel_name}': unnamed proxy subelement")
    return native


def sync_proxy_support(target_obj: Any, prop_name: str, selector_obj: Any, native_refs: Iterable[Any]) -> bool:
    """Point a placement property at the selector's proxy subelements.

    Replaces the original source entry while keeping unrelated third-party
    supports, so the consumer ends up with no direct link to the volatile
    source at all.  Returns True when the native value was rewritten, False
    when it already matched.  A missing source or empty refs raises instead
    of writing an incomplete value.
    """
    native_refs = list(native_refs)
    source = getattr(selector_obj, "BaseObject", None)
    if source is None:
        raise ValueError(f"Selector '{object_name(selector_obj)}' has no source object; cannot project proxy support")
    names = [str(getattr(r, "subname", "") or "") for r in native_refs]
    sel_name = object_name(selector_obj)
    if not names or any(not name for name in names):
        raise ValueError(f"Selector '{sel_name}' resolved to no proxy subelements; refusing to write an empty support")
    if any(str(getattr(r, "object_name", "") or "") != sel_name for r in native_refs):
        raise ValueError("Proxy references must address the selector itself")
    declared = property_type_name(target_obj, prop_name)
    if "List" in declared:
        drop = {sel_name, object_name(source)}
        existing = normalize_linksub(read_property_value(target_obj, prop_name))
        kept = [(linked, linked_names) for linked, linked_names in existing if object_name(linked) not in drop]
        desired = kept + [(selector_obj, names)]
        before = sorted((object_name(linked), name) for linked, linked_names in existing for name in linked_names)
        after = sorted((object_name(linked), name) for linked, linked_names in desired for name in linked_names)
        if before == after:
            return False
        _set_property(target_obj, prop_name, desired)
        return True
    if linksub_matches(read_property_value(target_obj, prop_name), native_refs):
        return False
    _set_property(target_obj, prop_name, (selector_obj, names))
    return True


def _is_proxy_binding(record: dict[str, Any]) -> bool:
    return bool(record.get("proxy"))


def _binding_record_for(selector_obj: Any, target_obj: Any, prop_name: str) -> Optional[dict[str, Any]]:
    for record in read_binding_records(selector_obj):
        if binding_matches_target(selector_obj, record, target_obj, prop_name):
            return record
    return None


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if hasattr(value, "Name"):
        return {"object": object_name(value)}
    try:
        return float(value)
    except Exception:
        return str(value)


def read_binding_records(selector_obj: Any) -> list[dict[str, Any]]:
    try:
        raw = list(getattr(selector_obj, BINDINGS_PROPERTY))
    except Exception:
        return []
    records = []
    for text in raw:
        try:
            record = json.loads(str(text))
            if isinstance(record, dict) and int(record.get("version", 0)) in {1, 2, BINDING_VERSION}:
                records.append(record)
        except Exception:
            continue
    return records


def _write_binding_records(selector_obj: Any, records: list[dict[str, Any]]) -> None:
    setattr(
        selector_obj,
        BINDINGS_PROPERTY,
        [json.dumps(record, sort_keys=True, separators=(",", ":")) for record in records],
    )


def _ensure_prop(obj: Any, type_name: str, name: str, group: str, description: str, default: Any = None) -> None:
    existing = set(getattr(obj, "PropertiesList", []) or [])
    if name in existing:
        return
    obj.addProperty(type_name, name, group, description)
    if default is not None:
        setattr(obj, name, default)


def ensure_binding_properties(selector_obj: Any) -> None:
    _ensure_prop(
        selector_obj,
        "App::PropertyStringList",
        BINDINGS_PROPERTY,
        "Robust Binding",
        "Explicit native-property bindings owned by this selector",
        [],
    )
    _ensure_prop(
        selector_obj,
        "App::PropertyString",
        BINDING_STATUS_PROPERTY,
        "Robust Binding",
        "Current binding diagnostics",
        "Not bound",
    )
    selector_obj.setEditorMode(BINDING_STATUS_PROPERTY, 1)


def ensure_target_link_properties(target_obj: Any) -> None:
    """Make the native consumer explicitly depend on its robust selector(s)."""
    _ensure_prop(
        target_obj, "App::PropertyLinkList", TARGET_LINK_PROPERTY, "Robust Selection",
        "Explicit robust selector dependencies feeding this native feature", [],
    )
    _ensure_prop(
        target_obj, "App::PropertyStringList", TARGET_INFO_PROPERTY, "Robust Selection",
        "Explicit robust selector/property bindings", [],
    )
    target_obj.setEditorMode(TARGET_LINK_PROPERTY, 1)
    target_obj.setEditorMode(TARGET_INFO_PROPERTY, 1)


def _sync_target_metadata(target_obj: Any, selector_obj: Any, property_name: str) -> None:
    ensure_target_link_properties(target_obj)
    links = [obj for obj in list(getattr(target_obj, TARGET_LINK_PROPERTY, []) or []) if obj is not None]
    if selector_obj not in links:
        links.append(selector_obj)
    setattr(target_obj, TARGET_LINK_PROPERTY, links)
    info = list(getattr(target_obj, TARGET_INFO_PROPERTY, []) or [])
    prefix = object_name(selector_obj)
    info = [entry for entry in info if not str(entry).startswith(prefix + "=")]
    info.append(f"{prefix}={property_name}")
    setattr(target_obj, TARGET_INFO_PROPERTY, info)


def _remove_target_metadata(target_obj: Any, selector_obj: Any, property_name: Optional[str] = None) -> None:
    ensure_target_link_properties(target_obj)
    links = [obj for obj in list(getattr(target_obj, TARGET_LINK_PROPERTY, []) or []) if obj is not selector_obj]
    setattr(target_obj, TARGET_LINK_PROPERTY, links)
    prefix = object_name(selector_obj)
    info = [str(entry) for entry in list(getattr(target_obj, TARGET_INFO_PROPERTY, []) or []) if not str(entry).startswith(prefix + "=")]
    setattr(target_obj, TARGET_INFO_PROPERTY, info)


def binding_matches_target(selector_obj: Any, record: dict[str, Any], target_obj: Any, prop_name: str) -> bool:
    return (
        str(record.get("target", "")) == object_name(target_obj)
        and str(record.get("property", "")) == str(prop_name)
    )


def bind_selector(selector_obj: Any, target_obj: Any, prop_name: str, mode: str, original_value: Any = None, proxy: Optional[bool] = None) -> dict[str, Any]:
    """Create/update one explicit binding and establish the native dependency.

    ``proxy=True`` means the native property addresses the selector's own
    stable proxy subelements (``Face1..``) instead of the volatile source
    subnames.  This is used for placement properties (``AttachmentSupport``):
    the consumer then has no direct link to the source at all, so a topology
    shock in the source can never even momentarily invalidate it — no false
    positive errors.  When ``proxy`` is None it defaults to True for
    ``AttachmentSupport`` LinkSub bindings and False otherwise.
    """
    if proxy is None:
        proxy = str(prop_name) == "AttachmentSupport" and str(mode) == "LinkSub"
    if not is_property_writable(target_obj, prop_name):
        raise ValueError(f"Native property {object_name(target_obj)}.{prop_name} is not writable")
    if property_mode(target_obj, prop_name) != mode:
        raise ValueError(f"Native property {object_name(target_obj)}.{prop_name} is not a supported {mode} property")
    ensure_binding_properties(selector_obj)
    ensure_target_link_properties(target_obj)
    records = read_binding_records(selector_obj)
    record = {
        "version": BINDING_VERSION,
        "target": object_name(target_obj),
        "property": str(prop_name),
        "mode": str(mode),
        "source": document_name(selector_obj),
        "original": _json_safe(original_value),
        "proxy": bool(proxy),
    }
    records = [r for r in records if not binding_matches_target(selector_obj, r, target_obj, prop_name)]
    records.append(record)
    _write_binding_records(selector_obj, records)
    _sync_target_metadata(target_obj, selector_obj, prop_name)
    selector_obj.BindingStatus = f"Bound explicitly to {object_label(target_obj)}.{prop_name} ({mode})"
    return record


def unbind_selector(selector_obj: Any, target_name: Optional[str] = None, prop_name: Optional[str] = None) -> int:
    """Remove explicit ownership without restoring or rewriting the native value."""
    ensure_binding_properties(selector_obj)
    doc = getattr(selector_obj, "Document", None)
    records = read_binding_records(selector_obj)
    kept = []
    removed = 0
    for record in records:
        match = (
            (target_name is None or record.get("target") == target_name)
            and (prop_name is None or record.get("property") == prop_name)
        )
        if match:
            removed += 1
            target = _find_doc_object(doc, str(record.get("target", "")))
            if target is not None:
                _remove_target_metadata(target, selector_obj, str(record.get("property", "")))
        else:
            kept.append(record)
    _write_binding_records(selector_obj, kept)
    selector_obj.BindingStatus = "Not bound" if not kept else f"{len(kept)} robust binding(s) active"
    return removed


def update_binding(selector_obj: Any, record: dict[str, Any], selector: Any) -> tuple[bool, str]:
    """Project one binding; ``False`` means the native value is deliberately untouched."""
    doc = getattr(selector_obj, "Document", None)
    target = _find_doc_object(doc, str(record.get("target", "")))
    if target is None:
        return False, f"Target {record.get('target')} no longer exists"
    source = getattr(selector_obj, "BaseObject", None)
    if source is None:
        return False, "Source object is missing"
    try:
        refs = selector.evaluate(source)
    except Exception as exc:
        return False, f"Selector evaluation failed: {exc}"
    expected = getattr(selector, "expected_count", None)
    if expected is not None and len(refs) != int(expected):
        return False, f"Selector is unresolved ({len(refs)}/{expected}); native value left unchanged"
    if not refs:
        return False, "Selector resolved to no features; native value left unchanged"
    prop_name = str(record.get("property", ""))
    mode = str(record.get("mode", ""))
    try:
        current = read_property_value(target, prop_name)
        if mode == "Link":
            if len(refs) != 1 or refs[0].subname != "":
                return False, "A Link target requires exactly one whole-object selector result"
            declared = property_type_name(target, prop_name)
            current = read_property_value(target, prop_name)
            if "List" in declared:
                if source not in list(current or []):
                    write_link(target, prop_name, source)
            elif current is not source:
                write_link(target, prop_name, source)
        elif mode == "LinkSub":
            declared = property_type_name(target, prop_name)
            if _is_proxy_binding(record):
                native_refs = proxy_native_refs(selector_obj, source, refs)
                sync_proxy_support(target, prop_name, selector_obj, native_refs)
            elif "List" in declared:
                if not linksub_source_matches(current, refs):
                    write_linksub(target, prop_name, refs, source, preserve_other_sources=True)
            elif not linksub_matches(current, refs):
                write_linksub(target, prop_name, refs, source, preserve_other_sources=False)
        elif mode == "FilletEdges":
            if not fillet_edges_match(target, refs, prop_name):
                write_fillet_edges(target, prop_name, refs, current)
        else:
            return False, f"Unsupported binding mode {mode}"
        return True, f"OK — {object_label(target)}.{prop_name}"
    except Exception as exc:
        return False, f"Projection failed — {object_label(target)}.{prop_name}: {exc}"


def update_selector_bindings(selector_obj: Any, selector: Any) -> str:
    """Refresh all explicitly persisted native projections during selector execution."""
    ensure_binding_properties(selector_obj)
    records = read_binding_records(selector_obj)
    if not records:
        selector_obj.BindingStatus = "Not bound"
        return selector_obj.BindingStatus
    statuses = [update_binding(selector_obj, record, selector)[1] for record in records]
    good = sum(status.startswith("OK —") for status in statuses)
    if good == len(statuses):
        status = f"{good} binding(s) up to date"
    else:
        status = f"{good}/{len(statuses)} binding(s) current; unresolved bindings left unchanged"
    selector_obj.BindingStatus = status
    return status




def inspect_feature_references(target_obj: Any) -> list[dict[str, Any]]:
    """Inspect candidate properties on a native target object and return those with active references."""
    candidates = target_property_candidates(target_obj)
    results = []
    for cand in candidates:
        prop_name = cand["name"]
        mode = cand["mode"]
        refs = refs_from_property(target_obj, prop_name, mode)
        if not refs:
            continue
        source_obj = cand.get("source") or source_object_from_property(target_obj, prop_name, mode)
        if source_obj is None:
            continue
        subnames = [r.subname for r in refs if r.subname]
        if not subnames and refs and any(r.subname == "" for r in refs):
            subnames = [""]
            kind = "Shape"
        else:
            kind = refs[0].kind if refs else "Shape"
        results.append({
            "property": prop_name,
            "mode": mode,
            "source": source_obj,
            "source_name": object_name(source_obj),
            "refs": refs,
            "subnames": subnames,
            "kind": kind,
        })
    return results

def _profile_sketches(target_obj: Any) -> list[Any]:
    """Return sketch/profile objects consumed via ``Profile`` (Pad/Pocket pattern)."""
    sketches: list[Any] = []
    profile = getattr(target_obj, "Profile", None)
    objs: list[Any] = []
    if hasattr(profile, "Name"):
        objs = [profile]
    elif isinstance(profile, (tuple, list)):
        if len(profile) == 2 and hasattr(profile[0], "Name"):
            objs = [profile[0]]
        else:
            for item in profile:
                if hasattr(item, "Name"):
                    objs.append(item)
                elif isinstance(item, (tuple, list)) and item and hasattr(item[0], "Name"):
                    objs.append(item[0])
    for obj in objs:
        if obj is not None and obj is not target_obj and obj not in sketches:
            sketches.append(obj)
    return sketches


def robustify_feature(
    target_obj: Any,
    doc: Any = None,
    prop_name: Optional[str] = None,
) -> tuple[Any, Any]:
    """Convert an existing native feature into a robustly driven feature with 1 click.

    Inspects the feature's candidate properties, extracts its current subelement references,
    computes the optimal semantic selection query, creates an explicit FeatureSelector object,
    and establishes the binding.

    If the selected feature (e.g. a Pocket) has no direct fragile reference but its
    profile sketch does (e.g. ``Sketch.AttachmentSupport`` -> ``Pad.Face3``), the sketch
    is robustified automatically so selecting the Pocket "just works".

    Returns (selector_obj, plan).
    """
    prepared = prepare_robustify_feature(target_obj, doc, prop_name)
    if "existing" in prepared:
        return prepared["existing"], None
    bundle = prepared["prepared"]
    return apply_robustify_plan(bundle["doc"], bundle, compute_robustify_plan(bundle))


def prepare_robustify_feature(
    target_obj: Any,
    doc: Any = None,
    prop_name: Optional[str] = None,
) -> dict[str, Any]:
    """Inspect and choose the fragile reference; runs on the GUI thread.

    Returns ``{"existing": selector}`` when the property is already bound, or
    ``{"prepared": bundle}`` ready for ``compute_robustify_plan`` (background
    safe) and ``apply_robustify_plan`` (GUI thread).  Cross-document sources
    raise here instead of building a selector that could never update.
    """
    if doc is None:
        doc = getattr(target_obj, "Document", None)
        if doc is None and App is not None:
            doc = App.ActiveDocument
    if doc is None:
        raise ValueError("Cannot robustify feature without an active document")

    refs_info = inspect_feature_references(target_obj)
    # Also look through the profile sketch: Pocket/Profile -> Sketch/AttachmentSupport
    # is the classic "pocket sketch" fragile link users mean when they select a Pocket.
    sketched: list[tuple[Any, dict[str, Any]]] = [(target_obj, info) for info in refs_info]
    for sketch in _profile_sketches(target_obj):
        for info in inspect_feature_references(sketch):
            sketched.append((sketch, info))

    def _is_fragile(item: tuple[Any, dict[str, Any]]) -> bool:
        _, info = item
        return bool(
            info.get("subnames")
            and info.get("subnames") != [""]
            and info.get("kind") != "Shape"
        )

    chosen = None
    actual_target = target_obj
    if prop_name:
        for owner, info in sketched:
            if info["property"] == prop_name and (owner is target_obj or _is_fragile((owner, info))):
                chosen = info
                actual_target = owner
                break
        if chosen is None:
            raise ValueError(f"Property '{prop_name}' has no active references on '{object_label(target_obj)}'")
    else:
        fragile = [item for item in sketched if _is_fragile(item)]
        if fragile:
            # Prefer a reference owned directly by the selected object; otherwise
            # fall through to the profile sketch so Pocket "just works".
            direct = [item for item in fragile if item[0] is target_obj]
            owner, chosen = direct[0] if direct else fragile[0]
            actual_target = owner
        else:
            if sketched:
                hint = ""
                for sketch in _profile_sketches(target_obj):
                    props = [i["property"] for i in inspect_feature_references(sketch)]
                    if props:
                        hint = f" Its profile sketch '{object_label(sketch)}' also has no fragile subelement reference."
                raise ValueError(
                    f"Feature '{object_label(target_obj)}' [{object_name(target_obj)}] has no fragile "
                    f"subelement reference (only whole-object links).{hint} Select a face/edge, "
                    f"a Fillet/Chamfer, or a sketch attached to a face, then robustify."
                )
            raise ValueError(
                f"Feature '{object_label(target_obj)}' [{object_name(target_obj)}] has no active "
                f"subelement references in any supported property."
            )

    prop = chosen["property"]
    mode = chosen["mode"]
    source_obj = chosen["source"]
    subnames = chosen["subnames"]
    kind = chosen["kind"]

    existing = find_bound_selector(doc, actual_target, prop)
    if existing is not None:
        return {"existing": existing}

    return {"prepared": prepare_robustify_plan(doc, actual_target, source_obj, prop, mode, subnames, kind)}


def prepare_robustify_plan(doc: Any, actual_target: Any, source_obj: Any, prop: str, mode: str, subnames: list[str], kind: str) -> dict[str, Any]:
    """Snapshot everything robustification needs; runs on the GUI thread.

    Returns a bundle with live objects for the write phase plus a detached
    source snapshot for background planning.  Raises on cross-document
    sources instead of building a selector that could never update.
    """
    if document_name(source_obj) != document_name(actual_target):
        raise ValueError(
            f"Cannot robustify '{object_label(actual_target)}': its reference lives on "
            f"'{object_name(source_obj)}' in document '{document_name(source_obj)}'. "
            f"Insert an App::Link to the external part in this document and select through the link instead."
        )
    from fs_selector import candidates
    return {
        "doc": doc,
        "actual_target": actual_target,
        "source_obj": source_obj,
        "snapshot": snapshot_source(source_obj),
        "pool": candidates(source_obj, kind),
        "prop": str(prop),
        "mode": str(mode),
        "subnames": [str(name) for name in subnames],
        "kind": str(kind),
    }


def compute_robustify_plan(prepared: dict[str, Any]) -> Any:
    """Compute the best semantic route; touches only the detached snapshot.

    Thread-safe by construction: no document object is dereferenced here.
    Raises when no unambiguous route exists.
    """
    from fs_planner import plan_selectors

    pool = prepared.get("pool")
    if pool is None:
        pool = prepared["snapshot"]
    subnames = prepared["subnames"]
    kind = prepared["kind"]
    plans = plan_selectors(pool, kind, subnames, max_results=12)
    if not plans:
        raise ValueError(
            f"Could not automatically determine an unambiguous semantic route to select "
            f"{subnames} on {object_name(prepared['source_obj'])}."
        )
    return plans[0]


def apply_robustify_plan(doc: Any, prepared: dict[str, Any], best_plan: Any) -> tuple[Any, Any]:
    """Create the selector object and bindings; runs on the GUI thread."""
    from fs_document import create_selector_object

    actual_target = prepared["actual_target"]
    source_obj = prepared["source_obj"]
    prop = prepared["prop"]
    mode = prepared["mode"]
    subnames = prepared["subnames"]
    selector = best_plan.selector

    # Note: Body Tip/ordering is owned by create_selector_object, which keeps
    # the selector inside the PartDesign history (right after its source),
    # restores a downstream Tip (never "loses" the Pocket), and leaves the
    # Tip at the new end when created last so later features append after it.
    doc.openTransaction(f"Robustify {object_label(actual_target)}.{prop}")
    try:
        selector_obj = create_selector_object(
            doc,
            source_obj,
            selector,
            label=f"Robust: {object_label(actual_target)} {prop}",
            captured_selection=subnames,
        )

        # Persistent explicit binding (record + DAG link source -> selector -> target).
        # Placement bindings (AttachmentSupport) are proxy bindings: the native
        # property addresses the selector's stable proxy faces, so the volatile
        # source topology can never invalidate the consumer mid-recompute.
        original = read_property_value(actual_target, prop)
        use_proxy = str(prop) == "AttachmentSupport" and str(mode) == "LinkSub"
        bind_selector(selector_obj, actual_target, prop, mode, original, proxy=use_proxy)
        # Live recompute link consumed by SelectorObjectProxy.execute().
        binding_prop = f"RobustSelector_{prop}"
        if mode in ("LinkSub", "FilletEdges"):
            if binding_prop not in set(getattr(actual_target, "PropertiesList", []) or []):
                actual_target.addProperty("App::PropertyLink", binding_prop, "Robust", f"Semantic selector for {prop}")
            setattr(actual_target, binding_prop, selector_obj)
        # No explicit native write here on purpose: the recompute below runs
        # the selector first (shape published before consumers update), so its
        # auto-update projects the support onto valid proxy geometry.  Writing
        # it here would re-attach the consumer while the selector shape does
        # not exist yet and print a bogus "subshape not found".

        doc.recompute()
        doc.commitTransaction()
        return selector_obj, best_plan
    except Exception:
        doc.abortTransaction()
        raise


def auto_update_consumers(selector_obj: Any, refs: list[Any]) -> None:
    """Push the freshly evaluated ``refs`` into explicitly bound consumers.

    An unresolved selector (empty ``refs``, already reported as a Warning on
    the selector itself) leaves every consumer untouched, exactly like
    ``update_binding`` does.  Anything else that goes wrong raises so it
    surfaces as a proper error instead of a silently incomplete update.
    """
    if not refs:
        return
    new_subnames = [str(getattr(r, "subname", "") or "") for r in refs if getattr(r, "subname", "")]

    for consumer in selector_obj.InList:
        for prop in consumer.PropertiesList:
            if prop.startswith("RobustSelector_") and getattr(consumer, prop) == selector_obj:
                target_prop = prop.replace("RobustSelector_", "")
                mode = property_mode(consumer, target_prop)
                if mode is None:
                    continue
                record = _binding_record_for(selector_obj, consumer, target_prop)
                use_proxy = _is_proxy_binding(record) if record is not None else False

                if mode == "LinkSub" and use_proxy:
                    # Placement consumers address the selector's stable proxy
                    # faces, so the volatile source can never invalidate them.
                    native_refs = proxy_native_refs(selector_obj, selector_obj.BaseObject, refs)
                    sync_proxy_support(consumer, target_prop, selector_obj, native_refs)
                    continue

                source_obj = source_object_from_property(consumer, target_prop, mode)

                if mode == "LinkSub":
                    # Skip the write when the native value already matches: this
                    # keeps the consumer Up-to-date instead of perpetually
                    # Touched, and avoids churning FreeCAD's element-map names.
                    declared = property_type_name(consumer, target_prop)
                    is_list = "List" in declared
                    current_value = read_property_value(consumer, target_prop)
                    if is_list and linksub_source_matches(current_value, refs):
                        continue
                    if not is_list and linksub_matches(current_value, refs):
                        continue
                    if is_list:
                        existing = normalize_linksub(current_value)
                        kept = [(linked, names) for linked, names in existing if object_name(linked) != object_name(source_obj)]
                        kept.append((source_obj, new_subnames))
                        _set_property(consumer, target_prop, kept)
                    else:
                        _set_property(consumer, target_prop, (source_obj, new_subnames))
                elif mode == "FilletEdges":
                    current = read_property_value(consumer, target_prop)
                    if not fillet_edges_match(consumer, refs, target_prop):
                        write_fillet_edges(consumer, target_prop, refs, current)


def collect_audit_snapshots(doc: Any = None) -> tuple[str, list[dict[str, Any]]]:
    """Snapshot every selector for background auditing; runs on the GUI thread.

    Live reads (properties, links, shape copies) happen here.  A source that
    cannot be snapshotted becomes an explicit ``snapshot_error`` item, which
    the evaluator reports as an ERROR row — never a silent skip.
    """
    if doc is None and App is not None:
        doc = App.ActiveDocument
    if doc is None:
        raise ValueError("No active document to audit")
    doc_name = str(getattr(doc, "Name", "") or "")
    items: list[dict[str, Any]] = []
    for obj in list(getattr(doc, "Objects", [])):
        if not hasattr(obj, "Query"):
            continue
        source = getattr(obj, "BaseObject", None)
        try:
            snapshot = snapshot_source(source) if source is not None else None
            snapshot_error = "" if source is not None else "Missing Source"
        except Exception as exc:
            snapshot = None
            snapshot_error = str(exc)
        pool = None
        if source is not None and not snapshot_error:
            try:
                from fs_selector import Selector, candidates
                sel = Selector.from_json(getattr(obj, "Query", ""))
                pool = candidates(source, sel.kind)
            except (ValueError, KeyError, AttributeError, TypeError):
                pool = None
        items.append({
            "name": object_name(obj),
            "label": object_label(obj),
            "source_name": object_name(source) if source is not None else "None",
            "query": str(getattr(obj, "Query", "") or ""),
            "captured": [str(name) for name in list(getattr(obj, "CapturedSelection", []) or [])],
            "renumbered": bool(getattr(obj, "Renumbered", False)),
            "renumbering_audit": str(getattr(obj, "RenumberingAudit", "") or ""),
            "bound_targets": [consumer.Name for consumer in getattr(obj, "InList", [])],
            "snapshot": snapshot,
            "pool": pool,
            "snapshot_error": snapshot_error,
        })
    return doc_name, items


def evaluate_audit_snapshots(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Evaluate snapshotted selectors; touches no live objects (thread-safe)."""
    import time

    from fs_selector import Selector

    rows: list[dict[str, Any]] = []
    for item in items:
        t_start = time.perf_counter()
        if item["snapshot_error"]:
            res_status = f"Error: {item['snapshot_error']}" if item["snapshot"] is None and item["source_name"] != "None" else item["snapshot_error"]
            captured: list[Any] = []
            res_count = 0
            expected_count = None
        else:
            try:
                selector = Selector.from_json(item["query"])
                target_src = item.get("pool") if item.get("pool") is not None else item["snapshot"]
                captured = selector.evaluate(target_src) if target_src is not None else []
                res_status = "Healthy" if target_src is not None else "Missing Source"
                res_count = len(captured)
                expected_count = getattr(selector, "expected_count", None)
            except Exception as exc:
                res_status = f"Error: {exc}"
                captured = []
                res_count = 0
                expected_count = None
        res_time = (time.perf_counter() - t_start) * 1000.0

        if "Error" in res_status:
            health = "ERROR"
        elif expected_count is not None and res_count != expected_count:
            health = "WARNING"
            res_status = "Mismatched Element Count"
        else:
            health = "OK"

        try:
            intent_dict = json.loads(item["query"])
            intent = intent_dict.get("route", "") if isinstance(intent_dict, dict) else str(item["query"])
        except (ValueError, TypeError):
            intent = str(item["query"])

        rows.append({
            "name": item["name"],
            "label": item["label"],
            "source": item["source_name"],
            "health": health,
            "status": res_status,
            "renumbered": item["renumbered"],
            "renumbering_audit": item["renumbering_audit"],
            "resolution_time_ms": res_time,
            "result_count": res_count,
            "expected_count": expected_count,
            "captured_count": len(captured),
            "bound_targets": item["bound_targets"],
            "intent": intent,
        })
    return rows


def assemble_audit_summary(doc_name: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Tally evaluated audit rows; pure, callable from any thread."""
    healthy = sum(1 for row in rows if row["health"] == "OK")
    renumbered = sum(1 for row in rows if row["renumbered"])
    warnings = sum(1 for row in rows if row["health"] == "WARNING")
    errors = sum(1 for row in rows if row["health"] == "ERROR")
    return {
        "document": doc_name,
        "total": len(rows),
        "healthy": healthy,
        "renumbered": renumbered,
        "warnings": warnings,
        "errors": errors,
        "selectors": rows,
    }


def audit_document_selectors(doc: Any = None) -> dict[str, Any]:
    """Scan the document and return an extensive audit of all robust selectors and bindings."""
    if doc is None and App is not None:
        doc = App.ActiveDocument
    if doc is None:
        return {"error": "No active document", "total": 0, "healthy": 0, "selectors": []}
    doc_name, items = collect_audit_snapshots(doc)
    return assemble_audit_summary(doc_name, evaluate_audit_snapshots(items))


def format_audit_report(audit: dict[str, Any]) -> str:
    """Format structured audit dictionary into a human-readable report."""
    if "error" in audit:
        return f"Audit Error: {audit['error']}"
    lines = [
        f"=== Robust Feature Selector Document Audit: '{audit.get('document', '')}' ===",
        f"Total Selectors: {audit['total']} | Healthy: {audit['healthy']} | Renumbered: {audit['renumbered']} | Warnings: {audit['warnings']} | Errors: {audit['errors']}",
        "-" * 70,
    ]
    for s in audit.get("selectors", []):
        renum_tag = " [RENUMBERED]" if s["renumbered"] else ""
        lines.append(f"• {s['label']} [{s['health']}]{renum_tag}")
        lines.append(f"  Source: {s['source']} | Matches: {s['result_count']}/{s['expected_count']} | Eval: {s['resolution_time_ms']:.2f}ms")
        if s["bound_targets"]:
            lines.append(f"  Bound To: {', '.join(s['bound_targets'])}")
        if s["intent"]:
            lines.append(f"  Intent: {s['intent']}")
        if s["renumbered"]:
            lines.append(f"  Notice: {s['renumbering_audit']}")
        lines.append("")
    return "\n".join(lines)


def robustify_all_features(doc: Any = None) -> dict[str, Any]:
    """Scan the entire document and robustify all native features referencing subelements.

    Finds features (e.g. Part::Fillet, PartDesign::Fillet, Chamfer, Sketcher::SketchObject,
    PartDesign::SubShapeBinder) that reference fragile subelements and replaces those references
    with explicit, robust FeatureSelector bindings.
    """
    if doc is None and App is not None:
        doc = App.ActiveDocument
    if doc is None:
        return {"error": "No active document", "total_scanned": 0, "robustified": 0}

    import time
    t_start = time.perf_counter()

    total_scanned = 0
    robustified = []
    skipped = []
    failed = []

    doc.openTransaction("Robustify Entire Document")
    try:
        candidates = []
        for obj in list(getattr(doc, "Objects", [])):
            if hasattr(obj, "Query"):
                continue
            total_scanned += 1
            refs = inspect_feature_references(obj)
            if refs:
                candidates.append((obj, refs))

        for target_obj, refs_info in candidates:
            # Only robustify actual subelement references; whole-object DAG links are already robust
            sub_refs = [
                info for info in refs_info
                if info.get("subnames") and info.get("subnames") != [""] and info.get("kind") != "Shape"
            ]
            for info in sub_refs:
                prop = info["property"]
                existing = find_bound_selector(doc, target_obj, prop)
                if existing is not None:
                    skipped.append(f"{object_label(target_obj)}.{prop} (already bound)")
                    continue
                try:
                    selector_obj, plan = robustify_feature(target_obj, doc, prop)
                    robustified.append({
                        "selector": object_name(selector_obj),
                        "subnames": info.get("subnames", []),
                        "target": object_name(target_obj),
                        "target_label": object_label(target_obj),
                        "property": prop,
                    })
                except Exception as ex:
                    failed.append({
                        "target": object_name(target_obj),
                        "target_label": object_label(target_obj),
                        "property": prop,
                        "error": str(ex),
                    })

        doc.recompute()
        doc.commitTransaction()
    except Exception:
        doc.abortTransaction()
        raise

    elapsed_ms = (time.perf_counter() - t_start) * 1000.0

    return {
        "document": str(getattr(doc, "Name", "") or ""),
        "total_scanned": total_scanned,
        "robustified_count": len(robustified),
        "skipped_count": len(skipped),
        "failed_count": len(failed),
        "time_ms": round(elapsed_ms, 2),
        "robustified": robustified,
        "skipped": skipped,
        "failed": failed,
    }

def format_robustify_all_report(summary: dict[str, Any]) -> str:
    """Format batch robustification summary into a readable report."""
    if "error" in summary:
        return f"Robustify All Error: {summary['error']}"
    lines = [
        f"=== Robustify All Features Summary: '{summary.get('document', '')}' ===",
        f"Scanned: {summary['total_scanned']} | Robustified: {summary['robustified_count']} | Skipped: {summary['skipped_count']} | Failed: {summary['failed_count']} | Time: {summary['time_ms']:.2f}ms",
        "-" * 70,
    ]
    for r in summary.get("robustified", []):
        lines.append(f"✓ {r['target_label']}.{r['property']} -> Bound to selector '{r['selector']}' ({len(r['subnames'])} elements: {r['subnames']})")
    for s in summary.get("skipped", []):
        lines.append(f"• Skipped: {s}")
    for f in summary.get("failed", []):
        lines.append(f"✗ Failed: {f['target_label']}.{f['property']}: {f['error']}")
    return "\n".join(lines)


def find_bound_selector(doc: Any, target_obj: Any, prop_name: str) -> Any:
    """Return the explicit selector bound to one native property, if any."""
    binding_prop = f"RobustSelector_{prop_name}"
    if hasattr(target_obj, binding_prop):
        return getattr(target_obj, binding_prop, None)
    return None

def all_bound_selectors_for_target(doc: Any, target_obj: Any) -> list[tuple[Any, str]]:
    result = []
    for prop in getattr(target_obj, "PropertiesList", []):
        if prop.startswith("RobustSelector_"):
            val = getattr(target_obj, prop)
            if val is not None:
                result.append((val, prop.replace("RobustSelector_", "")))
    return result


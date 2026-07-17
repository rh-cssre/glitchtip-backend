"""Budget-aware, drill-down-friendly serialization of issue events for MCP.

Why this module exists
----------------------
Events are ingested from end-user SDK configuration, so their size is
**unbounded and user-controlled across several independent dimensions**:

* number of stack frames (hundreds are common in some runtimes),
* per-frame local ``vars`` (Python frames include an entire ``__builtins__``
  module dump plus every local — mostly noise to an LLM),
* number of breadcrumbs (``max_breadcrumbs`` is a client setting and can be
  set very high),
* per-breadcrumb ``data`` blobs (e.g. a full raw-SQL statement + params),
* request bodies, ``contexts`` and ``extra`` payloads.

Capping any single dimension leaves the others free, so total size cannot be
predicted from the input. The approach here is therefore:

1.  Produce a *lean* default view: drop the data that is almost never useful
    to a triage agent (frame ``vars``, request body).
2.  **Measure** the serialized result and, if it exceeds a token budget, shed
    load in a fixed priority order, re-measuring after each step, so the
    exception core is never the thing that gets dropped.
3.  Record explicit **omission markers** so the agent knows exactly what it is
    not seeing (silent truncation reads as "this is everything").
4.  Pair the lean view with a **drill-down** tool so the rare-but-vital heavy
    data (a specific frame's locals, older breadcrumbs, the request body,
    contexts/extra) can be fetched on demand, a slice at a time, without ever
    loading the whole event.

All event field contents are untrusted, DSN-submitted data. This module only
reads and reshapes them; it never executes or trusts them.

The size proxy is ``len(json.dumps(...))`` in characters. We do not ship a
tokenizer server-side, so tokens are estimated with a coarse chars/token
heuristic. Budgets are documented in tokens for the reader and applied in
characters internally.
"""

import copy
import json

from . import serializers

# --- Budgets and caps --------------------------------------------------------

CHARS_PER_TOKEN = 4  # coarse heuristic; JSON is character-dense

# Default lean event payload budget. ~4k tokens leaves plenty of room in an
# agent's context for the surrounding conversation while still fitting a
# real exception with its in-app stack and recent breadcrumbs.
DEFAULT_EVENT_TOKEN_BUDGET = 4000

# Drill-down responses may legitimately be a bit larger (the caller explicitly
# asked for the heavy slice), but must still be bounded.
DRILL_DOWN_TOKEN_BUDGET = 6000

# Most-recent breadcrumbs kept in the lean view when shedding by count.
MAX_BREADCRUMBS_DEFAULT = 20

# Frames nearest the crash kept per exception value when all frames are
# system frames (so a pure third-party stack still shows the crash site).
KEEP_TAIL_FRAMES = 8

# Hard tail cap applied only by the final safety guard.
HARD_TAIL_FRAMES = 5

# Per-string truncation thresholds.
MAX_STRING_LEN = 1024
MAX_STRING_LEN_HARD = 256

# Pagination defaults for drill-down.
FRAME_PAGE_SIZE = 50
BREADCRUMB_PAGE_SIZE = 50

_OMITTED = "<omitted — fetch with get_event_detail>"

# Small slack left when shedding so the two scalar fields added to _meta after
# the final measurement (estimatedTokens, truncated) cannot tip us over budget.
_META_SLACK = 128


# --- Small helpers -----------------------------------------------------------


def _measure(obj) -> int:
    """Serialized character length, our proxy for output size."""
    return len(json.dumps(obj, default=str))


def estimate_tokens(char_len: int) -> int:
    return char_len // CHARS_PER_TOKEN


def _normalize_exception(exception):
    """Return the exception object as a dict with a ``values`` list.

    Handles the legacy list format (pre-2025) and the current dict format.
    """
    if not exception:
        return None
    if isinstance(exception, list):
        return {"values": exception}
    if isinstance(exception, dict):
        return exception
    return None


def _iter_frames(exception):
    """Yield ``(global_index, frame, value)`` across all exception values.

    The iteration order (values in list order, frames in list order) is
    invariant to the camelCase renaming done by ``get_entries`` and to whether
    the exception came from Postgres or cold storage, so the global index is a
    stable handle a drill-down call can resolve against the raw event data.
    """
    exc = _normalize_exception(exception)
    if not exc:
        return
    gidx = 0
    for value in exc.get("values") or []:
        if not isinstance(value, dict):
            continue
        stacktrace = value.get("stacktrace") or {}
        for frame in stacktrace.get("frames") or []:
            if isinstance(frame, dict):
                yield gidx, frame, value
                gidx += 1


def _frame_in_app(frame: dict):
    """in_app flag tolerant of raw (``in_app``) and camelCased (``inApp``)."""
    if "inApp" in frame:
        return frame.get("inApp")
    return frame.get("in_app")


def _collect_frames(data: dict) -> list:
    return [frame for _, frame, _ in _iter_frames(data.get("exception"))]


def _collect_breadcrumbs(data: dict) -> list:
    breadcrumbs = data.get("breadcrumbs")
    if isinstance(breadcrumbs, list):
        return breadcrumbs
    if isinstance(breadcrumbs, dict):
        return breadcrumbs.get("values") or []
    return []


def _find_entry(result: dict, entry_type: str):
    for entry in result.get("entries") or []:
        if isinstance(entry, dict) and entry.get("type") == entry_type:
            return entry
    return None


def _truncate_in_place(obj, max_len: int) -> int:
    """Recursively truncate over-long strings; return count truncated.

    Mutates ``obj`` in place. Only used on structures we own (deep copies),
    never on the live ORM object's ``data``.
    """
    count = 0
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(value, str):
                if len(value) > max_len:
                    obj[key] = value[:max_len] + f"…[truncated {len(value)} chars]"
                    count += 1
            else:
                count += _truncate_in_place(value, max_len)
    elif isinstance(obj, list):
        for i, value in enumerate(obj):
            if isinstance(value, str):
                if len(value) > max_len:
                    obj[i] = value[:max_len] + f"…[truncated {len(value)} chars]"
                    count += 1
            else:
                count += _truncate_in_place(value, max_len)
    return count


# --- Lean default serialization ---------------------------------------------


def serialize_event_lean(event, token_budget: int = DEFAULT_EVENT_TOKEN_BUDGET) -> dict:
    """Serialize an event into a lean, budget-bounded triage view.

    Always drops frame ``vars`` and the request body (replaced by markers).
    If the result still exceeds ``token_budget``, sheds load in priority order:
    system frames → breadcrumb ``data`` → older breadcrumbs → long strings →
    (final guard) whole heavy sections. Never tail-truncates raw JSON; the
    exception core (type/value/culprit + frames nearest the crash) is kept.

    The returned dict carries a ``_meta`` block describing what was omitted and
    how to fetch it with ``get_event_detail``.
    """
    char_budget = token_budget * CHARS_PER_TOKEN
    # Deep copy so mutation never touches the cached ORM ``event.data`` (the
    # serialized entries reference it directly).
    result = copy.deepcopy(serializers.serialize_event(event))
    omissions: dict = {}

    exception_entry = _find_entry(result, "exception")
    breadcrumbs_entry = _find_entry(result, "breadcrumbs")
    request_entry = _find_entry(result, "request")

    exc_obj = exception_entry.get("data") if exception_entry else None

    # --- Always: index frames + strip vars -------------------------------
    frames_with_vars = 0
    for gidx, frame, _value in _iter_frames(exc_obj):
        frame["frameIndex"] = gidx
        if frame.pop("vars", None) is not None:
            frame["varsOmitted"] = True
            frames_with_vars += 1
    if frames_with_vars:
        omissions["frameVars"] = {
            "framesWithVars": frames_with_vars,
            "note": (
                "Local variables (including any builtins module dump) stripped "
                "from all stack frames. Fetch one frame's locals with "
                "get_event_detail(event_id, section='vars', frame=<frameIndex>)."
            ),
        }

    # --- Always: strip request body --------------------------------------
    if request_entry is not None:
        req = request_entry.get("data")
        if isinstance(req, dict) and req.get("data") not in (None, "", {}, []):
            req["data"] = _OMITTED
            omissions["requestBody"] = {
                "omitted": True,
                "note": (
                    "Request body omitted. Fetch with "
                    "get_event_detail(event_id, section='request')."
                ),
            }

    # Attach _meta up front so its bytes (which grow as we record omissions)
    # count against the budget while shedding. ``omissions`` is shared by
    # reference, so recording an omission enlarges _meta too.
    result["_meta"] = {
        "lean": True,
        "tokenBudget": token_budget,
        "omissions": omissions,
        "drillDown": (
            "This is a reduced view. Use get_event_detail(event_id=<this "
            "event's `id`>, section=...) to fetch omitted data. Sections: "
            "'vars' (requires frame=<frameIndex>), 'frames', 'breadcrumbs' "
            "(supports offset/limit), 'request', 'contexts', 'extra'."
        ),
    }

    shed_budget = char_budget - _META_SLACK

    def over_budget() -> bool:
        return _measure(result) > shed_budget

    # --- Priority shedding (only while over budget) ----------------------
    if over_budget():
        _shed_system_frames(exc_obj, omissions)
    if over_budget():
        _strip_breadcrumb_data(breadcrumbs_entry, omissions)
    if over_budget():
        _cap_breadcrumbs(breadcrumbs_entry, omissions)
    if over_budget():
        truncated = _truncate_in_place(result, MAX_STRING_LEN)
        if truncated:
            omissions["truncatedStrings"] = {
                "count": truncated,
                "maxLen": MAX_STRING_LEN,
            }
    if over_budget():
        _final_guard(result, exc_obj, omissions, shed_budget)

    result["_meta"]["estimatedTokens"] = estimate_tokens(_measure(result))
    result["_meta"]["truncated"] = bool(omissions)
    return result


def _shed_system_frames(exc_obj, omissions: dict) -> None:
    """Drop non-in-app frames, keeping in-app frames and the crash-site tail.

    Frames retain their ``frameIndex`` so a drill-down can still address the
    dropped (system) frames by index.
    """
    exc = _normalize_exception(exc_obj)
    if not exc:
        return
    shown = 0
    omitted = 0
    for value in exc.get("values") or []:
        if not isinstance(value, dict):
            continue
        stacktrace = value.get("stacktrace") or {}
        frames = stacktrace.get("frames")
        if not frames:
            continue
        kept = [f for f in frames if _frame_in_app(f) is True]
        if not kept:
            # Pure third-party stack: keep the frames nearest the crash so the
            # exception still has a visible call site.
            kept = frames[-KEEP_TAIL_FRAMES:]
        omitted += len(frames) - len(kept)
        shown += len(kept)
        stacktrace["frames"] = kept
    if omitted:
        omissions["frames"] = {
            "shown": shown,
            "omitted": omitted,
            "note": (
                "System (non-in-app) frames omitted. Page the full flattened "
                "frame list with get_event_detail(event_id, section='frames', "
                "offset=<n>), then fetch a frame's locals with section='vars'."
            ),
        }


def _strip_breadcrumb_data(breadcrumbs_entry, omissions: dict) -> None:
    if breadcrumbs_entry is None:
        return
    values = (breadcrumbs_entry.get("data") or {}).get("values") or []
    stripped = 0
    for crumb in values:
        if isinstance(crumb, dict) and crumb.get("data") not in (None, "", {}, []):
            crumb["data"] = _OMITTED
            stripped += 1
    if stripped:
        omissions["breadcrumbData"] = {
            "stripped": stripped,
            "note": (
                "Per-breadcrumb `data` (e.g. raw SQL, params) omitted. Fetch "
                "full breadcrumbs with get_event_detail(event_id, "
                "section='breadcrumbs')."
            ),
        }


def _cap_breadcrumbs(breadcrumbs_entry, omissions: dict) -> None:
    if breadcrumbs_entry is None:
        return
    data = breadcrumbs_entry.get("data") or {}
    values = data.get("values") or []
    total = len(values)
    if total <= MAX_BREADCRUMBS_DEFAULT:
        return
    # Breadcrumbs are chronological (oldest first); keep the most recent.
    data["values"] = values[-MAX_BREADCRUMBS_DEFAULT:]
    omissions["breadcrumbs"] = {
        "shown": MAX_BREADCRUMBS_DEFAULT,
        "omitted": total - MAX_BREADCRUMBS_DEFAULT,
        "total": total,
        "note": (
            "Only the most recent breadcrumbs are shown. Older ones are at "
            "lower offsets (offset=0 is the oldest): page them with "
            "get_event_detail(event_id, section='breadcrumbs', offset=<n>)."
        ),
    }


def _final_guard(result: dict, exc_obj, omissions: dict, char_budget: int) -> None:
    """Last-resort shedding for pathological events, largest sections first.

    Only reached when the ordered steps above did not fit the budget (e.g. a
    single enormous context blob, or thousands of in-app frames). Drops whole
    labeled sections with markers rather than blindly cutting JSON, always
    preserving the exception type/value and a crash-site tail of frames.
    """

    def over() -> bool:
        return _measure(result) > char_budget

    # 1. Drop top-level contexts (custom contexts can be arbitrarily large).
    if over() and result.get("contexts"):
        result["contexts"] = _OMITTED
        omissions["contexts"] = {
            "omitted": True,
            "note": (
                "Contexts omitted. Fetch with get_event_detail(event_id, "
                "section='contexts')."
            ),
        }

    # 2. Reduce the request entry to method + url only.
    request_entry = _find_entry(result, "request")
    if over() and request_entry is not None:
        req = request_entry.get("data")
        if isinstance(req, dict):
            request_entry["data"] = {
                k: req.get(k) for k in ("method", "url") if req.get(k)
            }
            request_entry["data"]["_omitted"] = _OMITTED
            omissions["request"] = {
                "omitted": True,
                "note": (
                    "Request reduced to method/url. Fetch with "
                    "get_event_detail(event_id, section='request')."
                ),
            }

    # 3. Hard-cap frames to the crash-site tail per exception value.
    if over():
        exc = _normalize_exception(exc_obj)
        capped = 0
        shown = 0
        for value in (exc.get("values") if exc else []) or []:
            if not isinstance(value, dict):
                continue
            stacktrace = value.get("stacktrace") or {}
            frames = stacktrace.get("frames")
            if frames and len(frames) > HARD_TAIL_FRAMES:
                capped += len(frames) - HARD_TAIL_FRAMES
                kept = frames[-HARD_TAIL_FRAMES:]
                stacktrace["frames"] = kept
                shown += len(kept)
        if capped:
            existing = omissions.get("frames", {})
            existing_omitted = existing.get("omitted", 0)
            omissions["frames"] = {
                "shown": shown,
                "omitted": existing_omitted + capped,
                "note": (
                    "Frames hard-capped to the crash-site tail to fit budget. "
                    "Page the full list with get_event_detail(event_id, "
                    "section='frames', offset=<n>)."
                ),
            }

    # 4. Harder string truncation as the final measure.
    if over():
        truncated = _truncate_in_place(result, MAX_STRING_LEN_HARD)
        if truncated:
            omissions["truncatedStrings"] = {
                "count": truncated,
                "maxLen": MAX_STRING_LEN_HARD,
            }


# --- Drill-down --------------------------------------------------------------


def _fit_to_budget(payload, char_budget: int, omissions: dict):
    """Bound a drill-down payload: string-truncate, then drop items/keys.

    Returns the (possibly reduced) payload. Records what was dropped in
    ``omissions``. Never silently over-runs the budget.
    """
    if _measure(payload) <= char_budget:
        return payload
    _truncate_in_place(payload, MAX_STRING_LEN)
    if _measure(payload) <= char_budget:
        omissions["truncatedStrings"] = {"maxLen": MAX_STRING_LEN}
        return payload
    _truncate_in_place(payload, MAX_STRING_LEN_HARD)
    omissions["truncatedStrings"] = {"maxLen": MAX_STRING_LEN_HARD}

    # Still over: drop trailing list items / dict keys with a marker.
    if isinstance(payload, list):
        while payload and _measure(payload) > char_budget:
            payload.pop()
        omissions["itemsDropped"] = {
            "note": "Some items dropped to fit budget; narrow with offset/limit."
        }
    elif isinstance(payload, dict):
        keys = list(payload.keys())
        while keys and _measure(payload) > char_budget:
            payload.pop(keys.pop())
        omissions["keysDropped"] = {"note": "Some keys dropped to fit budget."}
    return payload


def serialize_event_detail(
    event,
    section: str,
    frame: int | None = None,
    offset: int = 0,
    limit: int | None = None,
    token_budget: int = DRILL_DOWN_TOKEN_BUDGET,
) -> dict:
    """Return a single heavy slice of an event for drill-down.

    Sections:
      * ``vars``       — locals for one frame (requires ``frame``=frameIndex).
                         ``__builtins__`` is always dropped as pure noise.
      * ``frames``     — paginated flattened frame list (offset/limit).
      * ``breadcrumbs``— paginated full breadcrumb list incl. ``data``.
      * ``request``    — full request entry incl. body/cookies/env.
      * ``contexts``   — full contexts.
      * ``extra``      — the event's ``extra`` payload.

    Only touches ``event.data`` and already-loaded scalar fields, so it is
    safe to call from the async serialization path (no sync ORM access).
    """
    char_budget = token_budget * CHARS_PER_TOKEN
    data = event.data or {}
    base: dict = {"eventId": event.id.hex, "section": section}
    omissions: dict = {}

    if section == "vars":
        if frame is None:
            raise ValueError("section='vars' requires a frame index")
        frames = _collect_frames(data)
        if not frames:
            raise ValueError("event has no stack frames")
        if frame < 0 or frame >= len(frames):
            raise ValueError(f"frame index {frame} out of range (0..{len(frames) - 1})")
        f = frames[frame]
        vars_dict = dict(f.get("vars") or {})
        if "__builtins__" in vars_dict:
            vars_dict.pop("__builtins__", None)
            omissions["builtins"] = "dropped (__builtins__ module dump is never useful)"
        vars_dict = copy.deepcopy(vars_dict)
        vars_dict = _fit_to_budget(vars_dict, char_budget, omissions)
        base.update(
            {
                "frame": frame,
                "frameSummary": {
                    "function": f.get("function"),
                    "filename": f.get("filename"),
                    "module": f.get("module"),
                    "lineNo": f.get("lineno", f.get("lineNo")),
                    "inApp": _frame_in_app(f),
                },
                "vars": vars_dict,
            }
        )

    elif section == "frames":
        frames = _collect_frames(data)
        total = len(frames)
        page = limit or FRAME_PAGE_SIZE
        window = frames[offset : offset + page]
        out = []
        for i, f in enumerate(window, start=offset):
            out.append(
                {
                    "frameIndex": i,
                    "function": f.get("function"),
                    "filename": f.get("filename"),
                    "module": f.get("module"),
                    "lineNo": f.get("lineno", f.get("lineNo")),
                    "colNo": f.get("colno", f.get("colNo")),
                    "inApp": _frame_in_app(f),
                    "hasVars": bool(f.get("vars")),
                }
            )
        base.update(
            {
                "frames": out,
                "total": total,
                "offset": offset,
                "limit": page,
                "shown": len(out),
            }
        )

    elif section == "breadcrumbs":
        crumbs = _collect_breadcrumbs(data)
        total = len(crumbs)
        page = limit or BREADCRUMB_PAGE_SIZE
        window = copy.deepcopy(crumbs[offset : offset + page])
        window = _fit_to_budget(window, char_budget, omissions)
        base.update(
            {
                "breadcrumbs": window,
                "total": total,
                "offset": offset,
                "limit": page,
                "shown": len(window),
            }
        )

    elif section in ("request", "contexts", "extra"):
        payload = copy.deepcopy(data.get(section))
        if payload is None:
            payload = {} if section != "extra" else {}
        payload = _fit_to_budget(payload, char_budget, omissions)
        base[section] = payload

    else:
        raise ValueError(
            f"unknown section {section!r}. Valid sections: vars, frames, "
            "breadcrumbs, request, contexts, extra."
        )

    if omissions:
        base["_meta"] = {"omissions": omissions}
    return base

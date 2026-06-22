"""Test helper for the Issue hot-split.

count/last_seen/status/level/last_release moved off Issue onto the one-to-one
IssueIndex leaf. ``baker.make("issue_events.Issue", status=...)`` no longer
works because those are not Issue fields anymore. ``make_issue`` bakes the Issue
(a post_save signal creates its leaf row) and applies the moved fields to the
leaf, keeping the in-memory relation consistent for direct attribute asserts.
"""

from model_bakery import baker

from apps.issue_events.models import IssueIndex

_LEAF_FIELDS = ("count", "last_seen", "status", "level", "last_release")


def make_issue(**kwargs):
    leaf = {k: kwargs.pop(k) for k in _LEAF_FIELDS if k in kwargs}
    issue = baker.make("issue_events.Issue", **kwargs)
    if leaf:
        IssueIndex.objects.filter(issue_id=issue.id).update(**leaf)
    # Load and cache the leaf so callers (including async serializers) can read
    # the proxy properties without an extra/sync query.
    issue._state.fields_cache.pop("index", None)
    issue.index  # noqa: B018 - triggers select_related-style caching
    return issue

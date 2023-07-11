from prometheus_client import Counter

issues_counter = Counter(
    "glitchtip_issues",
    "Issue creation counter per project",
    ["project", "organization"],
)

events_counter = Counter(
    "glitchtip_events",
    "Events creation counter per project",
    ["project", "organization", "issue"],
)

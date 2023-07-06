from prometheus_client import Counter, Gauge

organizations_metric = Gauge("glitchtip_organizations", "Number of organizations")
projects_metric = Gauge(
    "glitchtip_projects", "Number of projects per organization", ["organization"]
)
issues_counter = Counter(
    "glitchtip_issues",
    "Issue creation counter per project",
    ["project", "organization"],
)
events_counter = Counter(
    "glitchtip_events",
    "Events creation counter per project",
    ["project", "organization"],
)

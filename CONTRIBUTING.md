# How to contribute

You are encouraged to submit issues and merge requests.

A good issue includes reproducible steps for bugs. Clear use cases for feature requests.

A good merge request includes a unit test demonstrating how a bug exists and is fixed with your change. Out of caution, contributors must not view or be familiar with proprietary Sentry code. Our codebase borrows code and ideas from Sentry when it was open source. We provide a fork of the last open source version of sentry [here](https://gitlab.com/glitchtip/sentry-open-source). You may and should read, understand, and copy open source Sentry code. While Sentry's current code is on GitHub, it would violate their proprietary license to use it.

# Adding larger features and Python dependencies

Please open an issue to discuss any larger feature or new Python dependency before starting work. We aim to be very dependency-light, so as to keep the project maintainable with very little time. Larger feature development is encouraged, provided you are willing to assist with general project maintenance. Consider asking what maintenance task you can help with.

# Backend Architecture Overview

GlitchTip has an API only Django backend. The frontend is built in Angular and runs as a single page application, fully isolated from our backend. You could build your own frontend if you wanted to. We attempt to be API compatible with Sentry. GlitchTip users can use Sentry client SDK's to send their events to GlitchTip without any modification.

[This diagram](https://docs.google.com/drawings/d/1e2eKmEY21W1KaJsoC797j5ZedpDSsghGuYWV6CEuILY) shows how the backend handles events from ingestion to REST API endpoints.

Backend code has high test coverage and all features and bug fixes require a unit test.

## Coding style and philosophy

We are not a Sentry fork. The older open source Sentry project had a vast code base including multiple programming languages and a custom search engine. We do not believe that a small team of interested contributors can maintain such a large codebase. Instead we reimplement Sentry's features and sometimes port Sentry's open source python code.

- Use community solutions like django-ninja or Django Organizations over custom built code.
- Prefer simple over complex - it's better to have less features that are more reliable and easier to maintain. We do not wish to build a custom search engine.
- Economical over completeness. Make running GlitchTip as easy and simple as possible, especially for small and medium sized projects. Be wary of introducing additional external dependencies. The entire project must be maintained on a budget of 4 person-hours per week. When introducing a large new feature, offer to help with maintenance in addition.

GlitchTip backend is built with

- Django
- Celery background task runner
- django-ninja/Pydantic for async views
- mypy for types

Avoid:

- Django Rest Framework, port this code to django-ninja. We value type hinting, performance, and async first approach
- Inefficient database calls - GlitchTip must work for both small self hosters and 100 million event projects
  GlitchTip backend is built with. Always assume scale. If you need to edit every user, assume there are 100 million users and the queries much be chunked in batches. Neither one query per user nor one giant query that takes too long to execute.

## Terms

- Event - Any piece of tracked data that is ephemeral and consumes resources (and thus can be billed for or limited).
- Issue Event - An event that is related to a resolvable issue, such as a code exception or CSP report.
- Issue - A group of related Issue Events. When resolved, we consider all events to be resolved.
- Transaction Event - Event that tracks performance information.
- Uptime Check Event - Recoding of uptime status.

## Code Walkthrough

This section describes the idealized approach to coding GlitchTip. You may notice inconsistencies.

- /glitchtip - Django app configuration and select number of shared code that doesn't fit in any one Django app
- /apps - All Django apps go here. These apps follow current best practices.
- /apps/event_ingest - Accepts new issue and transaction events

### Legacy Sentry SDK Client support

The GlitchTip core team at this time is not interested in legacy sdk client support. Merge requests are accepted and welcome. Open legacy client feature requests along with intention to implement or interest in funding development.

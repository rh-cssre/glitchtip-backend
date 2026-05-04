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
- Prefer simple over complex - it's better to have less features that are more reliable and easier to maintain. Postgres full text search is a good enough and we do not wish to build a custom search engine.
- Performance with purpose. While we prefer simple code, it must perform up to it's task. A solution that is too slow isn't simple. It's a dead end requiring rewrite. High throughput event ingest is an example of when this is important.
- Economical over completeness. Make running GlitchTip as easy and simple as possible, especially for small and medium sized projects. Be wary of introducing additional external dependencies. The entire project must be maintained on a budget of 4 person-hours per week. When introducing a large new feature, offer to help with maintenance in addition.

GlitchTip backend is built with:

- [Django](https://www.djangoproject.com/)
- [Celery background task runner](https://docs.celeryq.dev/en/stable/)
- [django-ninja/Pydantic](https://django-ninja.dev/) for async views
- [mypy](https://mypy-lang.org/) for types

Avoid:

- Inefficient database calls - GlitchTip must work for both small self hosters and 100 million event projects. Always assume scale. If you need to edit every user, assume there are 100 million users and the queries much be chunked in batches. Neither one query per user nor one giant query that takes too long to execute.

## Formatting

Use ruff and mypy. Use an editor plugin or run `ruff check glitchtip/ apps/`. Add --fix to auto fix. The shortcuts
`make lint` and `make lint-check` are also provided.

## Terms

- Event - Any piece of tracked data that is ephemeral and consumes resources (and thus can be billed for or limited).
- Issue Event - An event that is related to a resolvable issue, such as a code exception or CSP report.
- Issue - A group of related Issue Events. When resolved, we consider all events to be resolved.
- Transaction Event - Event that tracks performance information.
- Uptime Check Event - Recoding of uptime status.

## Code Walkthrough

This section describes the idealized approach to coding GlitchTip. You may notice inconsistencies.

- `/glitchtip` - Django app configuration and select number of globally shared code such as startup scripts, pagination classes, asgi, settings, etc.
- `/apps` - All Django apps go here. These apps follow current best practices.
- `/apps/event_ingest` - Accepts new issue and transaction events
- `/apps/issue_events` - Issue event API and models
- `/apps/shared` - Shared code between apps. For example, a Schema that is shared between issue_events and event_ingest

### Legacy Sentry SDK Client support

The GlitchTip core team, at this time, is not interested in legacy sdk client support. Merge requests are accepted and welcome. Open legacy client feature requests along with the intention to implement or interest in funding development.

## Submitting bugs/feature requests around supporting sentry sdk events

Does GlitchTip not handle an event the way you expect? Here's how to contribute.

1. Create a demonstration, by creating/forking an [error factory project](https://gitlab.com/glitchtip/error-factories/). Remember that the reviewer is not likely to be very familiar with your code language/framework.
2. Open an issue in this repo. If you aren't able to create a clear issue with repeatable steps, then use Gitter instead to ask for help.
3. Write a unit test that fails without a code change. [Examples](https://gitlab.com/glitchtip/glitchtip-backend/-/tree/master/apps/event_ingest/tests).
4. Ideally, submit a merge request with a fix.

Clear examples, with use case explanations and a failing test, are much more likely to be accepted and fixed. Help requests, where the problem isn't understood, will not be.
If you don't have time to write a clear demonstration of the problem and propose fixes, we do offer [paid support](https://glitchtip.com/pricing).


## Security reports

We ask that vulnerabilities are reported through GitLab issues. High severity reports should be marked confidential. DOS mitigations should be posted publicaly. Best practices should not be reported. If you'd like to discuss best practices, please start a discussion on Gitter.

We do not offer any bug bounty program. Reporters soliciting fees will be directed to this page.

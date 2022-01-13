# How to contribute

You are encouraged to submit issues and merge requests.

A good issue includes reproducible steps for bugs. Clear use cases for feature requests.

A good merge request includes a unit test demonstrating how a bug exists and is fixed with your change. Out of caution, contributors must not view or be familiar with proprietary Sentry code. Our codebase borrows code and ideas from Sentry when it was open source. We provide a fork of the last open source version of sentry [here](https://gitlab.com/glitchtip/sentry-open-source). You may and should read, understand, and copy open source Sentry code. While Sentry's current code is on Github, it would violate their proprietary license to use it.

# Backend Architecture Overview

GlitchTip has an API only Django backend. The frontend is built in Angular and runs as a single page application, fully isolated from our backend. You could build your own frontend if you wanted to. We attempt to be API compatible with Sentry. GlitchTip users can use Sentry client SDK's to send their events to GlitchTip without any modification.

Backend code has high test coverage and all features and bug fixes require a unit test.

## Coding style and philosophy

We are not a Sentry fork. The older open source Sentry project had a vast code base including multiple programming languages and a custom search engine. We do not believe that a small team of interested contributors nor Burke Software can maintain such a large codebase. Instead we reimplement Sentry's features and sometimes port Sentry's open source python code.

- Use community solutions like Django Rest Framework or Django Organizations over custom built code.
- Prefer simple over complex - it's better to have less features that are more reliable and easier to maintain. We do not wish to build a custom search engine.
- Economical over completeness - the vast number of use cases do not require storing each and every event a high load usage. We'd rather figure out how to throttle and sample events well over sharding our Database to an extreme scale. Make running GlitchTip as easy and simple as possible, especially for small and medium sized projects. Be wary of introducing additional external dependencies.

### Serializers

GlitchTip django app serializers need to refer to each other. For example a project detail view will need to refer to an organization.

base_serializers.py can be shared apps. All other serializers should be internal to their app only. This avoids the need for circular imports.

The following naming conventions are applied:

- FooReferenceSerializer - smallest serializer that can be shared between Django apps. Often avoids relations to avoid circular dependencies.
- FooSerializer - Serializer used for List views. Maybe use some "cheap" relations that are appropiate with many rows of data.
- FooDetailSerializer - Serializer used for Retrieve views - the most detailed serializer that may have many relations.

### Porting Sentry Code to GlitchTip

GlitchTip has a "sentry" module for code ported from open source Sentry. Run it through `2to3` to make it Python 3 compatible and use it only as needed. Keeping the sentry module namespace makes porting easier.

### Legacy Sentry SDK Client support

The GlitchTip core team at this time is not interested in legacy sdk client support. Merge requests are accepted and welcome. Open legacy client feature requests along with intention to implement or interest in funding development.

# AI Usage Policy

GlitchTip's AI policy is maintained in one place for the whole organization:

**https://gitlab.com/glitchtip/glitchtip/-/blob/master/AI_POLICY.md**

In short: every issue and merge request must open with a **human-written
summary**, all **AI usage must be disclosed**, AI-authored code must be **verified
with real human use**, and no AI-generated media. Known contributors (GitLab
Developer+ members) are exempt from the mandatory human-summary rule. Please read
the full policy before contributing.

## Backend-specific: clean-room development

GlitchTip is API-compatible with Sentry, but Sentry's server code and docs are
**not** open source (BSL/FSL). Whether you write code by hand or with AI, you
must follow a strict clean-room process:

- Do **not** read, search, copy, or reference Sentry's server-side source or
  official documentation (`getsentry/sentry`, `getsentry/self-hosted`,
  `docs.sentry.io`, `getsentry/sentry-docs`, or similar).
- You **may** read Sentry's MIT-licensed client SDKs (e.g. `sentry-python`,
  `sentry-javascript`) to understand wire formats and payloads.
- AI tools are held to the same standard: do not paste or prompt with
  proprietary Sentry code, and do not ask an AI to reproduce it.

Merge requests must confirm clean-room compliance (see the MR template checkbox).

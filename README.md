[![Gitter](https://badges.gitter.im/GlitchTip/community.svg)](https://gitter.im/GlitchTip/community?utm_source=badge&utm_medium=badge&utm_campaign=pr-badge)

<script src="https://liberapay.com/GlitchTip/widgets/button.js"></script>

<noscript><a href="https://liberapay.com/GlitchTip/donate"><img alt="Donate using Liberapay" src="https://liberapay.com/assets/widgets/donate.svg"></a></noscript>

# GlitchTip Backend

GlitchTip is an open source, Sentry API compatible error tracking platform. It is a partial fork/mostly reimplementation of Sentry's open source codebase before it went proprietary. Its goals are to be a modern, easy-to-develop error tracking platform that respects your freedom to use it any way you wish. Some differences include:

- A modern development environment with Python 3 and Django 3.2.
- Simplicity over features. We use Postgres to store error data. Our code base is a fraction of the size of Sentry and looks like a typical Django app. We leverage existing open source Django ecosystem apps whenever possible.
- Respects your privacy. No massive JS bundles. No invasive tracking. No third party spying. Our marketing site only the privacy-focused Plausible analytics. Self hosted GlitchTip will never report home. We will never know if you run it yourself.
- Commitment to open source. We use open source tools like GitLab whenever possible. With our MIT license, you can use it for anything you'd like and even sell it. We believe in competition and hope you make GlitchTip even better.

Project status: Stable. Open an issue and say hello if you'd like to help. We are able to process basic error requests from the open source Sentry client tools. More features are on the way.

# Developing

We use Docker for development. View our [Contributing](./CONTRIBUTING.md) documentation if you'd like to help make GlitchTip better.

## Run local dev environment

1. Ensure docker and docker-compose are installed
2. `docker-compose up`
3. `docker-compose run --rm web ./manage.py migrate

Run tests with `docker-compose run --rm web ./manage.py test`

### Run HTTPS locally for testing FIDO2 keys

1. `cp docker-compose.yml docker-compose.override.yml`
2. Edit the override file and set `command: ./manage.py runsslserver 0.0.0.0:8000`
3. Restart docker compose services

### VS Code (Optional)

VS Code can do type checking and type inference. However, it requires setting up a virtual environment.

1. Install Python 3 dependencies. For Ubuntu this is `apt install python3-dev python3-venv`
2. Install [poetry](https://python-poetry.org/docs/#installation)
3. Create Python virtual environment `python3 -m venv env`
4. Activate environment `source env/bin/activate`
5. Install packages `poetry install`

### Load testing

Locust is built into the dev dependencies. To run with Locust run
`docker-compose -f docker-compose.yml -f docker-compose.locust.yml up`

Now go to localhost:8089 to run the test.

Locust will not be installed to production docker images and cannot be run from them.

# GCP Logging

In order to enable json logging, set the environment as follows::

```
DJANGO_LOGGING_HANDLER_CLASS=google.cloud.logging_v2.handlers.ContainerEngineHandler
UWSGI_LOG_ENCODER='json {"severity":"info","timestamp":${unix},"message":"${msg}"}}'
```

# Acknowledgements

- Thank you to the Sentry team for their ongoing open source SDK work and formerly open source backend of which this project is based on.
- We use element.io for internal chat and public gitter room
- Plausible Analytics is used for analytics

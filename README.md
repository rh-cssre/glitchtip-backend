# GlitchTip Backend

GlitchTip is an open source, Sentry API compatible error tracking platform. It is a partial fork/partial reimplementation of Sentry's open source codebase before it went proprietary. Its goals are to be a modern, easy-to-develop error tracking platform that respects your freedom to use it any way you wish. Some differences include:

- A modern development environment with Python 3 and Django 3.0
- Simplicity over features. We use Postgres to store error data. Our code base is a fraction of the size of Sentry and looks like a typical Django app. We leverage existing open source Django ecosystem apps whenever possible
- Respects your privacy. No Zendesk. No massive JS bundles. No tracking. Even our marketing site runs Matomo and respects Do Not Track. GlitchTip will never report home. We will never know if you run it yourself
- Commitment to open source. We use open source tools like GitLab whenever possible. With our MIT license, you can use it for anything you'd like and even sell it. We believe in competition and hope you make GlitchTip even better

Project status: Experimental. Open an issue and say hello if you'd like to help. We are able to process basic error requests from the open source Sentry client tools.

# Developing

We use Docker for development.

## Run local dev environment

1. Ensure docker and docker-compose are installed
2. `docker-compose up`
3. `docker-compose run --rm web ./manage.py migrate

Run tests with `docker-compose run --rm web ./manage.py test`

### VS Code (Optional)

VS Code can do type checking and type inference. However, it requires setting up a virtual environment.

1. Install Python 3 dependencies. For Ubuntu this is `apt install python3-dev python3-venv`
2. Install [poetry](https://python-poetry.org/docs/#installation)
3. `poetry install`

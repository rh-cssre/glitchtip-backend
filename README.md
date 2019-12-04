# Developing

## VS Code

VS Code can do as you type type checking, including type inference. However it does not work with Python in Docker. So set it up a virtual environment.

1. Install OS level dependencies. For Ubuntu this is `apt install python3-dev python3-venv`
2. Create virtual env in server folder `python3 -m venv env`
3. Install requirements `env/bin/pip install -r requirements.txt`

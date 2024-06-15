#!/usr/bin/env bash

export PYTHONUNBUFFERED=1

# change into script dir to use relative path's
SCRIPT_PATH=$(readlink -f $0)
SCRIPT_DIR=$(dirname "$SCRIPT_PATH")
SCRIPT_NAME=$(basename $0)
cd "$SCRIPT_DIR"

VENV_ACTIVATE="./venv/bin/activate"
if [ ! -f "$VENV_ACTIVATE" ] ; then
	echo "$SCRIPT_NAME\nvenv environment doesn't exist. Creating and installing dependencies..."
	python -m venv venv
	./venv/bin/pip install --upgrade -r requirements.txt
fi

./venv/bin/python ./mail_backup.py $@
exit $?

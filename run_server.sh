#!/bin/bash

source ./source_bearer_demo.sh

export PYTHONWARNINGS="ignore:Unverified HTTPS request"

#./.venv/bin/python  mim_proxy.py -l info -m claude-opus-4-7
./.venv/bin/python  mim_proxy.py -l info  -m gpt-4o-mini
#./.venv/bin/python  mim_proxy.py -l debug 

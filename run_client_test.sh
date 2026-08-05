#!/bin/bash

source ./source_bearer_demo.sh

curl -s -X POST http://127.0.0.1:8080/proxy \
-H 'Content-Type: application/json' \
-d '{"prompt":"add a main routine to the end of attached code and return the updated complete file"}' \
--data-binary @.//prompt.txt| jq


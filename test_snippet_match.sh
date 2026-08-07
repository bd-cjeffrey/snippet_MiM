#!/bin/bash

set -x

source ./source_bearer_demo.sh

./run_snippet_hash.sh ./test_code.c

cat ./snippet_match.json | jq

#!/bin/bash
# 
# Show snippet matching output, both the fingerprint that is generated,
# and the set of license matches found against what is in the test_code.c 
# file.
#

set -x

source ./set_envars.sh

./run_snippet_hash.sh ./test_code.c

cat ./snippet_match.json | jq

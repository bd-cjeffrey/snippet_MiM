#!/bin/bash

set -x

export JAVA_HOME=/opt/homebrew/Cellar/openjdk@17/17.0.20
export PATH=$JAVA_HOME/bin:$PATH

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

java -cp "$SCRIPT_DIR:$SCRIPT_DIR/sca-fingerprint-client-1.0.0.jar" sca \
"$1" \
./snippet_fingerp.json



curl  -k -X POST -H "Content-Type: application/vnd.blackducksoftware.bill-of-materials-6+json" -H "Authorization: bearer ${BEARER_TOK}" \
-o ./snippet_match.json -w '%{http_code}\n' \
--data-binary @./snippet_fingerp.json \
${BLACKDUCK_HOST}/api/snippet-matching > ./snippet_status.txt

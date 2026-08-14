#!/bin/bash

set -x


SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

java -cp "$SCRIPT_DIR:$SCRIPT_DIR/sca-fingerprint-client-1.0.0.jar" sca \
"$1" \
./snippet_fingerp.json



curl  -k -X POST -H "Content-Type: application/vnd.blackducksoftware.bill-of-materials-6+json" -H "Authorization: bearer ${BEARER_TOK}" \
-o ./snippet_match.json -w '%{http_code}\n' \
--data-binary @./snippet_fingerp.json \
${BLACKDUCK_HOST}/api/snippet-matching > ./snippet_status.txt

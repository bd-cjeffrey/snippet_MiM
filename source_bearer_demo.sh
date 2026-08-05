#!/bin/bash

#source this script, so envars are set outside 
export BLACKDUCK_TOK=${BLACKDUCK_MCP_GATEWAY_KEY}
export BLACKDUCK_HOST=https://sca.demo.blackduck.com

export BEARER_TOK=$(curl -k -X POST -H "Accept: application/vnd.blackducksoftware.user-4+json" -H "Authorization: token ${BLACKDUCK_TOK}" ${BLACKDUCK_HOST}/api/tokens/authenticate | jq ".bearerToken"| tr -d '"')


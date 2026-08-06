#!/bin/bash

#source this script, so envars are set outside

#export BLACKDUCK_TOK=< replace with your personal Black Duck SCA key>
export BLACKDUCK_HOST=https://sca.field-test.blackduck.com

_bd_resp=$(curl -k -s -w '\n%{http_code}' -X POST \
  -H "Accept: application/vnd.blackducksoftware.user-4+json" \
  -H "Authorization: token ${BLACKDUCK_TOK}" \
  "${BLACKDUCK_HOST}/api/tokens/authenticate")
_bd_status=$(echo "$_bd_resp" | tail -n1)
_bd_body=$(echo "$_bd_resp" | sed '$d')
_bd_token=$(echo "$_bd_body" | jq -r '.bearerToken // empty' 2>/dev/null)

if [ -z "$_bd_token" ]; then
  echo "ERROR: bearer token generation failed" >&2
  echo "  endpoint:     ${BLACKDUCK_HOST}/api/tokens/authenticate" >&2
  echo "  HTTP status:  ${_bd_status:-?}" >&2
  echo "  response:     ${_bd_body:-<empty>}" >&2
  if [ -z "$BLACKDUCK_TOK" ]; then
    echo "  hint: BLACKDUCK_TOK is not set — set it to a Black Duck personal access token" >&2
  else
    echo "  hint: check that BLACKDUCK_TOK is a valid Black Duck PAT for ${BLACKDUCK_HOST}" >&2
  fi
  unset BEARER_TOK _bd_resp _bd_status _bd_body _bd_token
  return 1 2>/dev/null || exit 1
fi

export BEARER_TOK="$_bd_token"
unset _bd_resp _bd_status _bd_body _bd_token

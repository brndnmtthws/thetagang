#!/bin/bash

# Overrides a specified ini file with IBC_ environment variables
# Note: Will NOT work if backslashes '\' are present
# Case-sensitive

# Fail fast
set -Eeuo pipefail

if [ $# -ne 1 ]; then
	echo "Usage: ./replace.sh <target>"
	exit 1
fi

target=$1

# Only select environment variables with IBC_ prefix,
# then trim that prefix out
prefix="IBC_"
env_vars=$(printenv | grep -E "^${prefix}.*" | cut -c $((${#prefix} + 1))-)

printf "Set variables:\n%s\n" "${env_vars}"

# Generate sed script file from override
script=$(sed -r 's/^((\w+=).*$)/\/^\2.*$\/c\\\1/' <<<"$env_vars")

# Replace in-place, making a backup
sed --in-place=.bak -r "$script" "$target"

printf "Changes made to %s:\n" "$target"
printf "%s\n" "$(diff "$target.bak" "$target")"

exit 0

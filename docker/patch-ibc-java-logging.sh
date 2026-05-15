#!/bin/sh
set -eu

ibcstart=${1:-/opt/ibc/scripts/ibcstart.sh}
log4j_config=${2:-file:/opt/thetagang/ibgateway-log4j2.xml}

if grep -q -- "-Dlog4j.configurationFile=" "$ibcstart"; then
  exit 0
fi

tmp=$(mktemp)
awk -v log4j_config="$log4j_config" '
  {
    print
  }
  $0 ~ /^java_vm_options="\$java_vm_options -Dinstall4jType=standalone"/ {
    print "java_vm_options=\"$java_vm_options -Dlog4j.configurationFile=" log4j_config "\""
    print "java_vm_options=\"$java_vm_options -Dlog4j2.statusLoggerLevel=OFF\""
    print "java_vm_options=\"$java_vm_options -Dorg.apache.logging.log4j.simplelog.StatusLogger.level=OFF\""
  }
' "$ibcstart" > "$tmp"

if ! grep -q -- "-Dlog4j.configurationFile=$log4j_config" "$tmp"; then
  rm -f "$tmp"
  echo "Unable to patch IBC Java logging options into $ibcstart" >&2
  exit 1
fi

cat "$tmp" > "$ibcstart"
rm -f "$tmp"

#!/bin/sh
set -eu

ibcstart=${1:-/opt/ibc/scripts/ibcstart.sh}
log4j_config=${2:-file:/opt/thetagang/ibgateway-log4j2.xml}

required_options=$(cat <<EOF
-Dlog4j.configurationFile=$log4j_config
-Dlog4j2.statusLoggerLevel=OFF
-Dlog4j2.defaultStatusLevel=ERROR
-DLog4jDefaultStatusLevel=ERROR
-Dlog4j2.StatusLogger.level=OFF
-Dorg.apache.logging.log4j.simplelog.StatusLogger.level=OFF
-DStatusLogger.level=OFF
EOF
)

all_options_present=true
missing_options=
for option in $required_options; do
  if ! grep -Fq -- "$option" "$ibcstart"; then
    all_options_present=false
    missing_options="${missing_options}${option}
"
  fi
done

if $all_options_present; then
  exit 0
fi

anchor='java_vm_options="$java_vm_options -Dinstall4jType=standalone"'
if ! grep -Fxq -- "$anchor" "$ibcstart"; then
  echo "Unable to patch IBC Java logging options into $ibcstart" >&2
  exit 1
fi

tmp=$(mktemp)
while IFS= read -r line; do
  printf '%s\n' "$line"
  case "$line" in
    "$anchor")
      for option in $missing_options; do
        printf '%s\n' "java_vm_options=\"\$java_vm_options $option\""
      done
      ;;
  esac
done < "$ibcstart" > "$tmp"

for option in $required_options; do
  if ! grep -Fq -- "$option" "$tmp"; then
    rm -f "$tmp"
    echo "Unable to patch IBC Java logging option $option into $ibcstart" >&2
    exit 1
  fi
done

cat "$tmp" > "$ibcstart"
rm -f "$tmp"

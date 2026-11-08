#!/bin/sh
# Post-deploy proof of the worker boundary. Runs INSIDE the coxd container as root:
#   ssh nas 'docker exec -i coxd sh -s' < deploy/smoke-sandbox.sh
# Every line must say ok. Prints no secret values (only whether a read succeeded).
u="${COXD_WORKER_USER:?}"
as() { setpriv --reuid="$u" --regid="$u" --init-groups --no-new-privs --inh-caps=-all "$@"; }
fail=0
ok()   { echo "ok   $1"; }
bad()  { echo "FAIL $1"; fail=1; }
denied()  { if as "$@" >/dev/null 2>&1; then bad "worker CAN: $*"; else ok "worker cannot: $*"; fi; }
allowed() { if as "$@" >/dev/null 2>&1; then ok "worker can: $*"; else bad "worker CANNOT: $*"; fi; }

denied cat "$COXD_HOME/coxd.sqlite"
denied ls "$COXD_HOME/repos"
denied sh -c "echo x > $COXD_HOME/probe"
denied cat /proc/1/environ
denied ls /root
denied sh -c 'echo x >> /repo/MoneyPulse/.git/config'
denied sh -c 'touch /repo/MoneyPulse/.git/hooks/pre-push'
[ -e /var/run/docker.sock ] && bad "docker.sock is mounted" || ok "no docker.sock"
[ -d /volume1/docker ] && bad "/volume1/docker is mounted" || ok "no /volume1/docker"
# No NAS share, cloud-storage or document volume may be mounted in the container
# (gate for setting intake: true on a repo with data_globs). Fail-closed: every mountpoint
# and filesystem type must be on the allowlist of what compose creates; anything else fails.
# mountinfo fields: $4 = mount root on the source fs, $5 = mountpoint (spaces as \040);
# fstype is the field after the "-" separator.
if [ -r /proc/self/mountinfo ]; then
  mounts=$(awk '{t=""; for(i=1;i<=NF;i++) if($i=="-"){t=$(i+1); break} print $5, t, $4}' /proc/self/mountinfo)
  badmp=$(echo "$mounts" | awk -v home="${COXD_HOME:-/data/coxswain}" -v u="$u" '
    $1=="/" || $1==home || $1=="/repo" || $1=="/work" || $1=="/home/" u "/.claude" { next }
    $1=="/etc/hosts" || $1=="/etc/hostname" || $1=="/etc/resolv.conf" { next }
    $1 ~ /^\/(proc|sys|dev)(\/|$)/ { next }
    { print $1 }')
  [ -z "$badmp" ] && ok "only expected mountpoints present" || bad "unexpected mountpoint(s): $(echo $badmp)"
  badfs=$(echo "$mounts" | awk '$2 !~ /^(overlay|btrfs|ext[234]|xfs|proc|sysfs|tmpfs|devtmpfs|devpts|mqueue|cgroup2?|shm)$/ { print $2 }' | sort -u)
  [ -z "$badfs" ] && ok "no network/cloud filesystem mounted" || bad "unexpected filesystem type(s): $(echo $badfs)"
  badroot=$(echo "$mounts" | awk '$3 ~ /^\/volume[0-9]+(\/|$)/ { print $1 }')
  [ -z "$badroot" ] && ok "no document/data volume mounted" || bad "host share bind-mounted at: $(echo $badroot)"
else
  bad "cannot read /proc/self/mountinfo to check data mounts"
fi
allowed sh -c 'touch /work/.probe && rm /work/.probe'
allowed git -C /repo/MoneyPulse log -1 --format=%h
allowed test -r "/home/$u/.claude"
[ "$(as id -u)" = "$(id -u "$u")" ] && ok "worker uid is $(id -u "$u")" || bad "uid switch failed"
coxd-claude --version >/dev/null 2>&1 && ok "coxd-claude runs" || bad "coxd-claude does not run"
[ $fail = 0 ] && echo "ALL OK" || { echo "SOME CHECKS FAILED"; exit 1; }

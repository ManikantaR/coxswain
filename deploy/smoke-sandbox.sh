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
allowed sh -c 'touch /work/.probe && rm /work/.probe'
allowed git -C /repo/MoneyPulse log -1 --format=%h
allowed test -r "/home/$u/.claude"
[ "$(as id -u)" = "$(id -u "$u")" ] && ok "worker uid is $(id -u "$u")" || bad "uid switch failed"
coxd-claude --version >/dev/null 2>&1 && ok "coxd-claude runs" || bad "coxd-claude does not run"
[ $fail = 0 ] && echo "ALL OK" || { echo "SOME CHECKS FAILED"; exit 1; }

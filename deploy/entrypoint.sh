#!/bin/sh
# Runs as root before coxd starts. Sets the file boundary between the control plane
# (root: store, registry, credentials, push) and the worker user (coxd/sandbox.py).
# Idempotent; re-applied on every start because volumes keep old ownership.
set -eu
umask 007   # files coxd creates are never world-readable
u="${COXD_WORKER_USER:-}"
if [ -n "$u" ]; then
    # Worker-owned: task worktrees, its claude login (it must read its own token).
    mkdir -p /work "/home/$u/.claude"
    chown -R "$u:$u" /work "/home/$u/.claude"
    chmod 2770 /work
    chmod 700 "/home/$u/.claude"
    # Target repos: the worker commits in its worktree, so the shared git object store
    # and refs are group-writable (core.sharedRepository=group is set system-wide).
    # .git itself, its config and its hooks stay root-only: a worker that could edit
    # them could make root run its code at the next fetch/push.
    for g in /repo/*/.git; do
        [ -d "$g" ] || continue
        chgrp -R "$u" "$g"
        chmod -R g+rwX "$g"
        find "$g" -type d -exec chmod g+s {} +
        chown root:root "$g" "$g/config"
        chmod 755 "$g"
        chmod 644 "$g/config"
        if [ -d "$g/hooks" ]; then chown -R root:root "$g/hooks"; chmod -R go-w "$g/hooks"; fi
    done
    # Control-plane state: root only. 711 lets the worker reach worktrees of tasks
    # dispatched before /work existed, without listing or reading anything else.
    h="${COXD_HOME:?}"
    mkdir -p "$h"
    chmod 711 "$h"
    for f in "$h"/coxd.sqlite*; do [ -e "$f" ] && chmod 600 "$f"; done
    [ -d "$h/repos" ] && chmod -R go-rwx "$h/repos"
    [ -d "$h/worktrees" ] && chown -R "$u:$u" "$h/worktrees"
fi
exec "$@"

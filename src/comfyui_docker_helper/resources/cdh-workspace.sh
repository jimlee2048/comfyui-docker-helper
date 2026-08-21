# cdh's POSIX-shell workspace convenience for SSH-associated login profiles.
if [ -n "${SSH_CONNECTION:-}" ]; then
    if [ "${WORKSPACE+x}" = x ] && cd "$WORKSPACE" 2>/dev/null; then
        :
    else
        cd /root 2>/dev/null || :
        printf '%s\n' 'Warning: cdh could not enter WORKSPACE; continuing in /root' >&2
    fi
fi

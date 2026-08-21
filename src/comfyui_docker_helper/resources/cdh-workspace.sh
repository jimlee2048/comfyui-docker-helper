# cdh's SSH-login workspace convenience for the project-provided root Bash shell.
if [[ -n ${SSH_CONNECTION:-} ]]; then
    if [[ -n ${WORKSPACE+x} ]] && cd -- "$WORKSPACE" 2>/dev/null; then
        :
    else
        cd /root 2>/dev/null || :
        printf '%s\n' 'Warning: cdh could not enter WORKSPACE; continuing in /root' >&2
    fi
fi

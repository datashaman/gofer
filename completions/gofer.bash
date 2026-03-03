#!/usr/bin/env bash
# Bash completion for gofer CLI
# Source this file: . completions/gofer.bash

_gofer_complete() {
    local cur prev words cword
    _init_completion || return

    # Top-level subcommands
    local subcommands="run approve reject select do"

    if [[ $cword -eq 1 ]]; then
        COMPREPLY=($(compgen -W "$subcommands" -- "$cur"))
        return
    fi

    local subcmd="${words[1]}"

    case "$subcmd" in
        select)
            # After "gofer select ISSUE-KEY", complete branch names
            if [[ $cword -eq 3 ]] && [[ "$cur" != -* ]]; then
                # Read branches from pending_approvals.json
                local config="config.yaml"
                for i in "${!words[@]}"; do
                    if [[ "${words[$i]}" == "--config" ]] && [[ -n "${words[$((i+1))]}" ]]; then
                        config="${words[$((i+1))]}"
                        break
                    fi
                done

                local pending_file
                pending_file=$(python3 -c "
import yaml, sys
try:
    with open('$config') as f:
        cfg = yaml.safe_load(f) or {}
    print(cfg.get('approvals', {}).get('pending_file', 'pending_approvals.json'))
except Exception:
    print('pending_approvals.json')
" 2>/dev/null)

                local branches
                branches=$(python3 -c "
import json, sys
issue_key = '${words[2]}'
try:
    with open('$pending_file') as f:
        data = json.load(f)
    for e in data:
        if (e.get('type') == 'branch_select'
                and e['issue_key'] == issue_key
                and e.get('decision') is None):
            for b in e.get('branches', []):
                print(b)
            break
except Exception:
    pass
" 2>/dev/null)
                COMPREPLY=($(compgen -W "$branches" -- "$cur"))
            elif [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "--fresh --list" -- "$cur"))
            fi
            ;;
    esac
}

complete -F _gofer_complete gofer

#!/usr/bin/env bash
# Usage: setup_codex_exec_home.sh [--force] [TARGET [EXECUTOR_MD]]
set -euo pipefail

force=0
args=()
for arg in "$@"; do
    case "$arg" in
        --force) force=1 ;;
        -h|--help)
            echo 'Usage: setup_codex_exec_home.sh [--force] [TARGET [EXECUTOR_MD]]'
            exit 0 ;;
        --*) echo "Unknown option: $arg" >&2; exit 2 ;;
        *) args+=("$arg") ;;
    esac
done
if (( ${#args[@]} > 2 )); then
    echo 'Expected at most TARGET and EXECUTOR_MD' >&2
    exit 2
fi
skill_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
target="${args[0]:-$HOME/.codex-exec}"
executor_md="${args[1]:-$skill_dir/EXECUTOR.md}"
auth_src="${CODEX_HOME_SRC:-$HOME/.codex}/auth.json"
if [[ ! -f "$auth_src" ]]; then
    echo "Source auth.json does not exist: $auth_src" >&2
    exit 1
fi
if [[ ! -f "$executor_md" ]]; then
    echo "EXECUTOR.md does not exist: $executor_md" >&2
    exit 1
fi
# Absolute links also work when TARGET is elsewhere or contains spaces.
auth_src="$(cd -- "$(dirname -- "$auth_src")" && pwd -P)/auth.json"
executor_md="$(cd -- "$(dirname -- "$executor_md")" && pwd -P)/$(basename -- "$executor_md")"
mkdir -p -- "$target"
target="$(cd -- "$target" && pwd -P)"
if [[ "$target/auth.json" == "$auth_src" ||
      "$target/AGENTS.md" == "$executor_md" ]]; then
    echo 'Target would replace a source file' >&2
    exit 1
fi
# Check all destinations before changing any of them; never remove directories.
for name in auth.json AGENTS.md config.toml; do
    path="$target/$name"
    if [[ -e "$path" || -L "$path" ]]; then
        if (( force == 0 )); then
            echo "Keeping existing: $path"
        elif [[ -d "$path" && ! -L "$path" ]]; then
            echo "Refusing to replace directory: $path" >&2
            exit 1
        fi
    fi
done
for name in auth.json AGENTS.md config.toml; do
    path="$target/$name"
    if [[ -e "$path" || -L "$path" ]]; then
        (( force == 1 )) || continue
        rm -- "$path"
    fi
    case "$name" in
        auth.json) ln -s -- "$auth_src" "$path" ;;
        AGENTS.md) ln -s -- "$executor_md" "$path" ;;
        config.toml) printf 'model_reasoning_effort = "medium"\n' > "$path" ;;
    esac
done
echo "Executor home ready: $target"

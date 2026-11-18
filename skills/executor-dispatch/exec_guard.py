#!/usr/bin/env python3
"""Real-dispatch guard. Benchmarks deliberately do not use this wrapper."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time


def rollout_paths(sessions):
    return set(Path(sessions).rglob("rollout-*.jsonl"))


def read_rollout(path):
    """Retry incomplete files on the next scan; retain all model IDs."""
    try:
        with path.open(encoding="utf-8", errors="replace") as stream:
            payload = json.loads(stream.readline()).get("payload", {})
            if not isinstance(payload, dict):
                return {}, []
            models = set()
            for line in stream:
                try:
                    collect_models(json.loads(line), models)
                except ValueError:
                    continue
        return payload, sorted(models)
    except (OSError, ValueError, AttributeError):
        return {}, []


def collect_models(value, models):
    if isinstance(value, dict):
        model = value.get("model")
        if isinstance(model, str) and model:
            models.add(model)
        for child in value.values():
            collect_models(child, models)
    elif isinstance(value, list):
        for child in value:
            collect_models(child, models)


def scan_codex(sessions, baseline):
    rollouts = [read_rollout(p) for p in rollout_paths(sessions) - baseline]
    parents = {p.get("id") for p, _ in rollouts
               if p.get("source") == "exec" and p.get("id")}
    children = {}
    for payload, models in rollouts:
        if (payload.get("thread_source") == "subagent"
                and payload.get("parent_thread_id") in parents):
            child = payload.get("id")
            if child:
                children[child] = {"thread_id": child,
                                   "parent_thread_id": payload["parent_thread_id"],
                                   "models": models}
    return list(children.values())


def terminate_group(proc):
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    # The group may outlive its leader; always send KILL after the grace period.
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.killpg(proc.pid, 0)
        except ProcessLookupError:
            break
        proc.poll()
        time.sleep(0.05)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait()


def claude_stats(output):
    try:
        events = [json.loads(output)]
    except ValueError:
        events = []
        for line in output.splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
    spawned = 0
    models = set()
    for event in events:
        if not isinstance(event, dict) or event.get("type") != "result":
            continue
        stats = event.get("subagent_stats")
        count = stats.get("spawned") if isinstance(stats, dict) else None
        if type(count) is int and count > spawned:
            spawned = count
        usage = event.get("modelUsage")
        if isinstance(usage, dict):
            models.update(usage)
    return spawned, sorted(models)


def run(executor, arguments, report=None):
    sessions = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser() / "sessions"
    baseline = rollout_paths(sessions) if executor == "codex" else set()
    if executor == "claude" and not any(
            a == "--disallowedTools" or a.startswith("--disallowedTools=")
            for a in arguments):
        arguments = ["--disallowedTools", "Agent,Task", *arguments]
    children = []
    models = []
    spawned = 0
    # Inherit stdin/stderr; tee Claude stdout verbatim while retaining it on disk.
    with tempfile.TemporaryFile() as captured:
        proc = subprocess.Popen(
            [executor, "exec" if executor == "codex" else "-p", *arguments],
            start_new_session=True,
            stdout=subprocess.PIPE if executor == "claude" else None)
        if executor == "codex":
            while True:
                try:
                    proc.wait(timeout=1)
                    break
                except subprocess.TimeoutExpired:
                    children = scan_codex(sessions, baseline)
                    if children:
                        terminate_group(proc)
                        break
            children = scan_codex(sessions, baseline) or children
            spawned = len(children)
            models = sorted({m for c in children for m in c["models"]})
            if children:
                # Also clean up any descendants left after normal leader exit.
                terminate_group(proc)
                for child in children:
                    print("EXEC_GUARD: subagent detected "
                          f"(parent={child['parent_thread_id']}, "
                          f"child={child['thread_id']}, "
                          f"model={','.join(child['models']) or 'unknown'})",
                          file=sys.stderr)
        else:
            while True:
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    break
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
                captured.write(chunk)
            proc.stdout.close()
            proc.wait()
            captured.seek(0)
            spawned, models = claude_stats(captured.read().decode("utf-8", "replace"))
            if spawned:
                print(f"EXEC_GUARD: subagent detected (spawned={spawned})",
                      file=sys.stderr)
    if report:
        Path(report).write_text(json.dumps({
            "executor": executor, "subagents_spawned": spawned,
            "subthreads": children, "models_used": models,
            "delegation_detected": spawned > 0}, ensure_ascii=False,
            indent=2) + "\n", encoding="utf-8")
    return 86 if spawned else proc.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report")
    parser.add_argument("executor", choices=("codex", "claude"))
    # Parse wrapper options on either side of executor, up to the delimiter.
    argv = sys.argv[1:]
    boundary = argv.index("--") if "--" in argv else len(argv)
    args = parser.parse_args(argv[:boundary])
    return run(args.executor, argv[boundary + 1:], args.report)


if __name__ == "__main__":
    sys.exit(main())

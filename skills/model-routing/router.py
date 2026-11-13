#!/usr/bin/env python3
"""Small, standard-library router and append-only JSONL ledger utility."""
import argparse
import json
import os
from pathlib import Path
import sys
from datetime import datetime, timezone

try:
    import yaml
except ImportError as exc:  # pragma: no cover - environment error
    raise SystemExit("error: PyYAML is required (pip install PyYAML)") from exc

EXECUTORS = {"codex", "claude", "dsh"}
STATUSES = {"active", "candidate", "retired"}
CATEGORIES = {"A", "B", "C", "D"}
OUTCOMES = {"pass", "fail_test", "fail_review", "regression", "test_gaming",
            "misunderstood", "quota", "env_error"}
FIELDS = ("ts", "task_id", "project", "category", "executor", "model", "attempt",
          "outcome", "escalated_to", "wall_s", "tokens_in", "tokens_out", "cost_usd", "note")
OPTIONAL = ("project", "escalated_to", "wall_s", "tokens_in", "tokens_out", "cost_usd", "note")


class RouterError(Exception):
    pass


def load_config(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except OSError as e:
        raise RouterError(f"cannot read config {path}: {e}")
    except yaml.YAMLError as e:
        raise RouterError(f"invalid YAML in config {path}: {e}")
    if not isinstance(data, dict):
        raise RouterError("config must be a YAML mapping")
    if set(data) != {"pool", "routing", "rework", "thresholds"}:
        raise RouterError("config must contain exactly pool, routing, rework, thresholds")
    pool = data["pool"]
    if not isinstance(pool, list) or not pool:
        raise RouterError("config pool must be a non-empty list")
    ids = set()
    for i, m in enumerate(pool):
        required = {"id", "executor", "status", "evaluated", "note"}
        if not isinstance(m, dict) or not required <= set(m) or set(m) - required - {"alias"}:
            raise RouterError(f"pool[{i}] must have id, executor, status, evaluated, note; alias is optional")
        if "alias" in m and (not isinstance(m["alias"], str) or not m["alias"].strip()):
            raise RouterError(f"pool[{i}].alias must be a non-empty string")
        if not isinstance(m["id"], str) or not m["id"].strip() or m["id"] in ids:
            raise RouterError(f"pool[{i}].id must be a unique non-empty string")
        ids.add(m["id"])
        if m["executor"] not in EXECUTORS or m["status"] not in STATUSES:
            raise RouterError(f"pool[{i}] has invalid executor or status")
        if type(m["evaluated"]) is not bool or not isinstance(m["note"], str):
            raise RouterError(f"pool[{i}] evaluated must be boolean and note must be string")
    routing = data["routing"]
    if not isinstance(routing, dict) or set(routing) != {"A", "B", "C"}:
        raise RouterError("routing must define exactly A, B, and C")
    for cat, models in routing.items():
        if not isinstance(models, list) or any(not isinstance(x, str) or x not in ids for x in models) or len(models) != len(set(models)):
            raise RouterError(f"routing.{cat} must be a list of unique pool model IDs")
    for section, keys in (("rework", {"same_defect_rounds", "total_rounds"}),
                          ("thresholds", {"median_time_ratio", "cost_ratio", "reeval_min_records", "reeval_window", "reeval_rate", "rework_records_min", "rework_rescue_rate"})):
        val = data[section]
        if not isinstance(val, dict) or set(val) != keys:
            raise RouterError(f"{section} must contain {', '.join(sorted(keys))}")
        if any(type(v) not in (int, float) or v < 0 for v in val.values()):
            raise RouterError(f"{section} values must be non-negative numbers")
    for key in ("reeval_min_records", "reeval_window"):
        if type(data["thresholds"][key]) is not int or data["thresholds"][key] <= 0:
            raise RouterError(f"thresholds.{key} must be a positive integer")
    return data


def read_ledger(path):
    records = []
    try:
        with open(path, encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                if not line.strip():
                    raise RouterError(f"invalid ledger {path}:{lineno}: blank lines are not allowed")
                try:
                    row = json.loads(line)
                    validate_record(row)
                except (json.JSONDecodeError, RouterError) as e:
                    raise RouterError(f"invalid ledger {path}:{lineno}: {e}")
                records.append(row)
    except FileNotFoundError:
        return []
    except OSError as e:
        raise RouterError(f"cannot read ledger {path}: {e}")
    return records


def parse_timestamp(value):
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise RouterError("ts must be an ISO8601 timestamp with a timezone")
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise RouterError("ts must be an ISO8601 timestamp with a timezone")
    return stamp


def validate_record(row):
    if not isinstance(row, dict) or set(row) != set(FIELDS):
        raise RouterError("record must contain exactly: " + ", ".join(FIELDS))
    for key in ("task_id", "category", "executor", "model", "outcome", "ts"):
        if not isinstance(row[key], str) or not row[key]:
            raise RouterError(f"{key} must be a non-empty string")
    parse_timestamp(row["ts"])
    if row["category"] not in CATEGORIES:
        raise RouterError("category must be A, B, C, or D")
    if row["executor"] not in EXECUTORS:
        raise RouterError("executor must be codex, claude, or dsh")
    if row["outcome"] not in OUTCOMES:
        raise RouterError("invalid outcome")
    if type(row["attempt"]) is not int or row["attempt"] < 0:
        raise RouterError("attempt must be an integer >= 0")
    for key in ("project", "escalated_to", "note"):
        if row[key] is not None and not isinstance(row[key], str):
            raise RouterError(f"{key} must be a string or null")
    for key in ("wall_s", "tokens_in", "tokens_out", "cost_usd"):
        val = row[key]
        if val is not None and (type(val) not in (int, float) or val < 0):
            raise RouterError(f"{key} must be a non-negative number or null")


def cmd_route(args, cfg):
    if args.category not in cfg["routing"]:
        raise RouterError("route category must be A, B, or C")
    by_id = {m["id"]: m for m in cfg["pool"]}
    for mid in cfg["routing"][args.category]:
        if by_id[mid]["status"] == "active":
            print(mid)


def cmd_log(args, cfg):
    if args.category not in CATEGORIES:
        raise RouterError("category must be A, B, C, or D")
    if args.executor not in EXECUTORS:
        raise RouterError("executor must be codex, claude, or dsh")
    model_ids = {m["id"] for m in cfg["pool"]}
    if args.model not in model_ids:
        raise RouterError(f"unknown model: {args.model}")
    row = {"ts": datetime.now(timezone.utc).isoformat(), "task_id": args.task_id,
           "project": args.project, "category": args.category, "executor": args.executor,
           "model": args.model, "attempt": args.attempt, "outcome": args.outcome,
           "escalated_to": args.escalated_to, "wall_s": args.wall_s,
           "tokens_in": args.tokens_in, "tokens_out": args.tokens_out,
           "cost_usd": args.cost_usd, "note": args.note}
    validate_record(row)
    payload = (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    path = Path(args.ledger)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
    except OSError as e:
        raise RouterError(f"cannot append ledger {path}: {e}")
    print(json.dumps(row, ensure_ascii=False))


def cmd_stats(args, cfg):
    rows = read_ledger(args.ledger)
    if args.last is not None:
        if args.last <= 0:
            raise RouterError("--last must be a positive integer")
        if args.category:
            rows = [r for r in rows if r["category"] == args.category]
        rows = rows[-args.last:]
    elif args.category:
        rows = [r for r in rows if r["category"] == args.category]
    groups = {}
    for r in rows:
        groups.setdefault((r["category"], r["model"]), []).append(r)
    for (cat, model), records in sorted(groups.items()):
        tasks = {}
        for r in records:
            tasks.setdefault(r["task_id"], []).append(r)
        first = [r for r in records if r["attempt"] == 0]
        first_pass = sum(r["outcome"] == "pass" for r in first) / len(first) if first else 0.0
        escalated = sum(bool(r["escalated_to"]) for r in records)
        reworked = sum(r["attempt"] > 0 for r in records)
        rounds = [max(r["attempt"] for r in task_rows) for task_rows in tasks.values() if any(r["attempt"] > 0 for r in task_rows)]
        print(json.dumps({"category": cat, "model": model, "records": len(records),
                          "first_pass_rate": first_pass, "escalation_rate": escalated / len(records),
                          "rework_rate": reworked / len(records),
                          "avg_rework_rounds": sum(rounds) / len(rounds) if rounds else 0.0}, ensure_ascii=False))


def cmd_detect(args, cfg):
    known = {m["id"] for m in cfg["pool"]}
    try:
        with open(args.codex_cache, encoding="utf-8") as f:
            cache = json.load(f)
    except OSError as e:
        raise RouterError(f"cannot read Codex cache {args.codex_cache}: {e}")
    except json.JSONDecodeError as e:
        raise RouterError(f"invalid JSON in Codex cache {args.codex_cache}: {e}")
    if not isinstance(cache, dict) or not isinstance(cache.get("models"), list):
        raise RouterError("Codex cache must be an object containing a models list")
    codex_new = []
    for i, m in enumerate(cache["models"]):
        if not isinstance(m, dict) or not isinstance(m.get("slug"), str) or not isinstance(m.get("visibility"), str):
            raise RouterError(f"Codex cache models[{i}] needs string slug and visibility")
        if m["visibility"] == "list" and m["slug"] not in known:
            codex_new.append(m["slug"])
    claude_new = []
    if args.claude_ids:
        ids = args.claude_ids.split(",")
        if any(not x.strip() for x in ids):
            raise RouterError("--claude-ids must be comma-separated non-empty IDs")
        known_claude = {m["id"] for m in cfg["pool"] if m["executor"] == "claude"}
        claude_new = sorted({x.strip() for x in ids} - known_claude)
    print(json.dumps({"codex": sorted(set(codex_new)), "claude": claude_new}, ensure_ascii=False))


def cmd_check(args, cfg):
    by_cat = {}
    for r in read_ledger(args.ledger):
        by_cat.setdefault(r["category"], []).append(r)
    threshold = cfg["thresholds"]
    needed = []
    details = {}
    for cat, all_rows in sorted(by_cat.items()):
        rows = sorted(all_rows, key=lambda r: parse_timestamp(r["ts"]))[-threshold["reeval_window"]:]
        if len(rows) < threshold["reeval_min_records"]:
            continue
        upgrade = sum(bool(r["escalated_to"]) for r in rows) / len(rows)
        rework = sum(r["attempt"] > 0 for r in rows) / len(rows)
        details[cat] = {"records": len(rows), "upgrade_rate": upgrade, "rework_rate": rework}
        if upgrade > threshold["reeval_rate"] or rework > threshold["reeval_rate"]:
            needed.append(cat)
    print(json.dumps({"categories": needed, "rates": details}, ensure_ascii=False))


def positive_number(value):
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a number")
    if number < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return number


def positive_int(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be an integer")
    if number < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return number


def make_parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", default=None)
    p.add_argument("--ledger", default=None)
    subs = p.add_subparsers(dest="command", required=True)
    route = subs.add_parser("route")
    route.add_argument("category")
    log = subs.add_parser("log")
    log.add_argument("--task-id", required=True)
    log.add_argument("--category", required=True)
    log.add_argument("--executor", required=True)
    log.add_argument("--model", required=True)
    log.add_argument("--attempt", required=True, type=positive_int)
    log.add_argument("--outcome", required=True)
    log.add_argument("--project")
    log.add_argument("--escalated-to")
    log.add_argument("--wall-s", type=positive_number)
    log.add_argument("--tokens-in", type=positive_int)
    log.add_argument("--tokens-out", type=positive_int)
    log.add_argument("--cost-usd", type=positive_number)
    log.add_argument("--note")
    stats = subs.add_parser("stats")
    stats.add_argument("--category")
    stats.add_argument("--last", type=int)
    detect = subs.add_parser("detect")
    detect.add_argument("--codex-cache", default=str(Path.home() / ".codex/models_cache.json"))
    detect.add_argument("--claude-ids", default="")
    subs.add_parser("check-reeval")
    return p


def main(argv=None):
    parser = make_parser()
    args = parser.parse_args(argv)
    module_dir = Path(__file__).resolve().parent
    root = module_dir.parent if module_dir.name == "tools" else module_dir
    args.config = args.config or root / "config/models.yaml"
    args.ledger = args.ledger or root / "ledger/ledger.jsonl"
    try:
        cfg = load_config(args.config)
        if args.command == "route": cmd_route(args, cfg)
        elif args.command == "log": cmd_log(args, cfg)
        elif args.command == "stats":
            if args.category and args.category not in CATEGORIES: raise RouterError("category must be A, B, C, or D")
            cmd_stats(args, cfg)
        elif args.command == "detect": cmd_detect(args, cfg)
        elif args.command == "check-reeval": cmd_check(args, cfg)
    except RouterError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

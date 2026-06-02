"""
Convert temp.jsonl (cargo nextest libtest-json-plus output) to execution.jsonl and result.json.

execution.jsonl: one line per test result (type=test, event!=started); name after $; event->status (ok->pass, failed->fail); path before $ with :: -> /.
result.json: num_tests (sum of test_count from type=suite, event=started), pass_count, failed_count, pass_rate, failed_rate.
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def _event_to_status(event: str) -> str:
    if event == "ok":
        return "pass"
    if event == "failed":
        return "fail"
    return event


def _parse_test_name(full_name: str) -> tuple[str, str]:
    if "$" in full_name:
        before, after = full_name.rsplit("$", 1)
        path = before.replace("::", "/")
        return after.strip(), path
    return full_name.strip(), ""


def convert_temp_to_execution_and_result(temp_path: Path) -> tuple[Path, Path]:
    """
    Read temp.jsonl, write execution.jsonl and result.json in the same directory.
    Returns (execution_path, result_path).
    """
    temp_path = Path(temp_path).resolve()
    if not temp_path.is_file():
        raise FileNotFoundError(f"temp.jsonl not found: {temp_path}")
    out_dir = temp_path.parent
    execution_path = out_dir / "execution.jsonl"
    result_path = out_dir / "result.json"

    num_tests = 0
    pass_count = 0
    failed_count = 0
    execution_lines: list[dict] = []

    with open(temp_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            obj_type = obj.get("type")
            event = obj.get("event", "")

            if obj_type == "suite":
                if event == "started":
                    num_tests += obj.get("test_count", 0)
                else:
                    pass_count += obj.get("passed", 0)
                    failed_count += obj.get("failed", 0)
                continue

            if obj_type == "test" and event != "started":
                name_full = obj.get("name", "")
                test_name, path = _parse_test_name(name_full)
                status = _event_to_status(event)
                out_obj = {
                    "name": test_name,
                    "status": status,
                    "path": path,
                    "exec_time": obj.get("exec_time"),
                }
                if "stdout" in obj:
                    out_obj["stdout"] = obj["stdout"]
                execution_lines.append(out_obj)

    with open(execution_path, "w") as f:
        for rec in execution_lines:
            f.write(json.dumps(rec) + "\n")

    num_tests = num_tests or (pass_count + failed_count)
    pass_rate = (pass_count / num_tests) if num_tests else 0.0
    failed_rate = (failed_count / num_tests) if num_tests else 0.0
    result_obj = {
        "num_tests": num_tests,
        "pass_count": pass_count,
        "pass_rate": round(pass_rate, 4),
        "failed_count": failed_count,
        "failed_rate": round(failed_rate, 4),
    }
    with open(result_path, "w") as f:
        json.dump(result_obj, f, indent=2)

    logger.info(
        "Wrote %s (%d lines) and %s (num_tests=%d, pass=%d, fail=%d)",
        execution_path.name,
        len(execution_lines),
        result_path.name,
        num_tests,
        pass_count,
        failed_count,
    )
    return execution_path, result_path


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Convert temp.jsonl to execution.jsonl and result.json")
    parser.add_argument("temp_jsonl", type=Path, help="Path to temp.jsonl")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    convert_temp_to_execution_and_result(args.temp_jsonl)


if __name__ == "__main__":
    main()

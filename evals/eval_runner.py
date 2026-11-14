import argparse
import json
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

if __package__:
    from .eval_context import (
        EvalContextError, EvalRunRegistry, build_agent_prompt,
        collect_evidence, group_eval_cases, validate_eval_handoff,
    )
    from .evaluator import evaluate_response
    from .eval_process import EvalProcessCleanupError, open_opencode
else:
    from eval_context import (
        EvalContextError, EvalRunRegistry, build_agent_prompt,
        collect_evidence, group_eval_cases, validate_eval_handoff,
    )
    from evaluator import evaluate_response
    from eval_process import EvalProcessCleanupError, open_opencode


# =============================================================
# CLI
# =============================================================

def parse_args():
    parser = argparse.ArgumentParser(
        description="Запуск QA Agent на golden dataset"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        default=False,
        help="Показывать полный вывод агента: события, текст, tool calls, stderr",
    )
    # parse_known_args — чтобы лишние аргументы не ломали запуск
    return parser.parse_known_args()[0]


DEBUG = False


# =============================================================
# НАСТРОЙКИ ДИАГНОСТИКИ
# =============================================================

# Сколько последних сырых строк вывода агента хранить в памяти
MAX_TRANSCRIPT_LINES = 300

# Ограничение на печать одного сырого события в debug-режиме
MAX_DEBUG_PAYLOAD_CHARS = 4000

# Сколько последних событий печатать при падении агента без debug
MAX_FAILURE_TAIL_LINES = 30


# =============================================================
# PATHS
# =============================================================

project_path = Path(__file__).resolve().parent.parent

dataset_path = (
        Path(__file__).parent
        / "dataset"
        / "golden_dataset.json"
)


# =============================================================
# QUALITY GATE THRESHOLDS
# =============================================================

MIN_PASS_RATE = 100.0
MIN_EXPECTED_PROPERTIES_PASS_RATE = 95.0
MAX_FORBIDDEN_BEHAVIOR_VIOLATION_RATE = 0.0
MAX_ERRORS = 0


# =============================================================
# ЗАГРУЗКА GOLDEN DATASET
# =============================================================

def load_eval_groups(path):
    with open(path, "r", encoding="utf-8") as file:
        return group_eval_cases(json.load(file))


# =============================================================
# ДИАГНОСТИКА ВЫВОДА QA AGENT
# =============================================================

def debug_print(*args, **kwargs):
    """Печать только при запуске с флагом --debug."""
    if DEBUG:
        print(*args, **kwargs)


def as_text(value):
    """Привести произвольное значение события к строке."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def extract_error_text(event):
    """Достать текст ошибки из события OpenCode (схема может отличаться)."""
    for key in ("error", "message", "data", "part"):
        text = as_text(event.get(key)).strip()
        if text and text != "{}":
            return text
    return as_text(event).strip()


def report_agent_diagnostics(reason, return_code, stderr, error_events,
                             transcript, agent_response):
    """Объяснить, что именно вернул агент и почему это ошибка."""
    print()
    print(reason)
    print(f"RETURN CODE: {return_code}")

    print()
    print("STDERR:")
    print(stderr if stderr else "<пусто>")

    if error_events:
        print()
        print("ОШИБКИ В ВЫВОДЕ АГЕНТА:")
        for error_event in error_events:
            print(f"- {error_event[:MAX_DEBUG_PAYLOAD_CHARS]}")

    if agent_response:
        print()
        print("ЧАСТИЧНЫЙ ОТВЕТ АГЕНТА ДО ОШИБКИ:")
        print(agent_response)

    if DEBUG:
        print()
        print("ПОСЛЕДНИЕ СОБЫТИЯ АГЕНТА (debug):")
        for raw_line in transcript:
            print(raw_line[:MAX_DEBUG_PAYLOAD_CHARS])
    elif transcript:
        print()
        print(f"ПОСЛЕДНИЕ {MAX_FAILURE_TAIL_LINES} СОБЫТИЙ АГЕНТА:")
        tail = list(transcript)[-MAX_FAILURE_TAIL_LINES:]
        for raw_line in tail:
            print(raw_line[:MAX_DEBUG_PAYLOAD_CHARS])
        print()
        print("Полная трасса уже сохранена в evals/results/runs; --debug добавляет вывод в консоль.")


def save_debug_transcript(page_id, prompt, command, transcript, stderr,
                          agent_response, return_code, trace_path, prompt_path,
                          workdir=project_path):
    """Сохранить дополнительную диагностику (только в debug-режиме)."""
    debug_dir = Path(workdir) / "evals/results/debug"
    debug_dir.mkdir(parents=True, exist_ok=True)

    timestamp = time.strftime("%y%m%d_%H%M%S")
    log_file = debug_dir / (
        f"{timestamp}_page_{page_id}_{trace_path.parent.name}_{trace_path.stem}.log"
    )

    with open(log_file, "w", encoding="utf-8") as file:
        file.write("COMMAND:\n")
        file.write(" ".join(command) + " <stdin>\n\n")
        file.write(f"STDIN PROMPT FILE: {prompt_path}\n\n")
        file.write("PROMPT:\n")
        file.write(prompt + "\n\n")
        file.write("RETURN CODE:\n")
        file.write(f"{return_code}\n\n")
        file.write("STDERR:\n")
        file.write((stderr or "<пусто>") + "\n\n")
        file.write("AGENT RESPONSE:\n")
        file.write((agent_response or "<пусто>") + "\n\n")
        file.write(f"FULL RAW EVENTS: {trace_path}\n\n")
        file.write("RAW EVENTS (последние события):\n")
        for raw_line in transcript:
            file.write(raw_line + "\n")

    print(f"DEBUG: диагностика агента сохранена: {log_file}")


# =============================================================
# ЗАПУСК QA AGENT
# =============================================================

def run_agent(page_id, prompt, trace_path, workdir=project_path):
    """Запустить QA Agent и собрать ответ + диагностику."""

    command = [
        "opencode.cmd",
        "run",
        "--standalone",
        "--agent",
        "orchestrator",
        "-m",
        "opencode/deepseek-v4.1-flash-free",
        "--format",
        "json",
    ]

    debug_print()
    debug_print("КОМАНДА ЗАПУСКА:", " ".join(command), "<stdin>")
    debug_print("PROMPT:", prompt)
    debug_print()

    trace_path = Path(trace_path)
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    response_path = trace_path.with_suffix(".response.md")
    prompt_path = trace_path.with_suffix(".prompt.md")
    transcript = deque(maxlen=MAX_TRANSCRIPT_LINES)
    stderr_lines = []
    text_parts = []
    error_events = []
    stderr_thread = None

    # Файлы открываются до старта процесса: при коллизии ничего не публикуется.
    with (
        trace_path.open("x", encoding="utf-8", buffering=1) as trace_file,
        response_path.open("x", encoding="utf-8") as response_file,
        prompt_path.open("x+", encoding="utf-8", newline="") as prompt_file,
    ):
        # .cmd обрезает многострочный аргумент; stdin сохраняет JSON-контекст,
        # кавычки и длинные промпты без разбора командной оболочкой.
        prompt_file.write(prompt)
        prompt_file.flush()
        prompt_file.seek(0)
        try:
            with open_opencode(command, workdir, stdin=prompt_file) as process:
                # stderr читается параллельно, чтобы не заполнить пайп.
                def read_stderr():
                    if process.stderr is None:
                        return
                    try:
                        for stderr_line in process.stderr:
                            stderr_lines.append(stderr_line.rstrip("\n"))
                    except Exception as error:
                        stderr_lines.append(f"<не удалось прочитать stderr: {error}>")

                stderr_thread = threading.Thread(target=read_stderr, daemon=True)
                stderr_thread.start()
                print("QA Agent запущен...", flush=True)

                stdout = process.stdout if process.stdout is not None else ()
                for raw_line in stdout:
                    trace_file.write(raw_line)
                    line = raw_line.strip()
                    if not line:
                        continue
                    transcript.append(line)
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        event = None
                    if not isinstance(event, dict):
                        print("Не удалось разобрать событие OpenCode", flush=True)
                        debug_print("  RAW:", line[:MAX_DEBUG_PAYLOAD_CHARS])
                        continue

                    event_type = event.get("type")
                    print("AGENT EVENT:", event_type, flush=True)
                    part = event.get("part")
                    part = part if isinstance(part, dict) else {}

                    if event_type == "text":
                        text = part.get("text")
                        if isinstance(text, str) and text:
                            text_parts.append(text)
                            debug_print(text)
                    elif event_type in ("tool", "tool_use", "tool_call"):
                        state = part.get("state")
                        state = state if isinstance(state, dict) else {}
                        status = state.get("status") or ""
                        tool_name = part.get("tool") or part.get("name") or "unknown"
                        debug_print(f"  TOOL: {tool_name} [{status}]")
                        if status == "error" or state.get("error"):
                            error_text = as_text(state.get("error") or state.get("output"))
                            error_events.append(f"tool {tool_name}: {error_text}")
                            print(f"  ОШИБКА TOOL {tool_name}: {error_text[:500]}", flush=True)
                    elif event_type == "error":
                        error_text = extract_error_text(event)
                        error_events.append(error_text)
                        print("AGENT ERROR:", error_text[:1000], flush=True)
                    debug_print("  RAW:", line[:MAX_DEBUG_PAYLOAD_CHARS])

                return_code = process.wait()
                stderr_thread.join(timeout=5)
        finally:
            if stderr_thread is not None and stderr_thread.ident is not None:
                stderr_thread.join(timeout=5)
            # Сохраняется и частичный ответ после ошибки чтения/Ctrl+C.
            response_file.write("\n".join(text_parts).strip())

    stderr = "\n".join(stderr_lines).strip()
    agent_response = "\n".join(text_parts).strip()

    print()
    print("QA AGENT RETURN CODE:", return_code)

    debug_print()
    debug_print("STDERR АГЕНТА:", stderr if stderr else "<пусто>")
    debug_print(f"ДЛИНА ТЕКСТОВОГО ОТВЕТА АГЕНТА: {len(agent_response)}")

    if DEBUG:
        save_debug_transcript(
            page_id=page_id,
            prompt=prompt,
            command=command,
            transcript=transcript,
            stderr=stderr,
            agent_response=agent_response,
            return_code=return_code,
            trace_path=trace_path,
            prompt_path=prompt_path,
            workdir=workdir,
        )

    return {
        "return_code": return_code,
        "stderr": stderr,
        "agent_response": agent_response,
        "error_events": error_events,
        "transcript": transcript,
        "trace_path": trace_path,
        "prompt_path": prompt_path,
    }


# =============================================================
# ЗАПУСК QA AGENT
# =============================================================

def error_result(eval_case, context, error):
    return {
        "eval_id": eval_case.get("eval_id", eval_case.get("id")),
        "name": eval_case["name"],
        **context,
        "status": "ERROR",
        "score": 0.0,
        "expected_properties": [],
        "forbidden_behavior": [],
        "error": str(error),
    }


def run_eval_groups(groups, project=project_path):
    registry = EvalRunRegistry(project)
    # Блокировка охватывает и judge: он читает ещё неизменённые ledger.
    with registry.locked():
        return _run_eval_groups(groups, registry)


def _run_eval_groups(groups, registry):
    eval_results = []
    for (page_id, run_type), page_eval_cases in groups:
        print()
        print("=" * 80)
        print("ЗАПУСК QA AGENT")
        print("=" * 80)
        print(f"Источник: Confluence, pageId: {page_id}, run_type: {run_type}")
        print(f"Количество Eval Cases: {len(page_eval_cases)}")
        print(f"Режим отладки (--debug): {'ВКЛ' if DEBUG else 'ВЫКЛ'}")
        context = {"page_id": page_id, "run_type": run_type, "run_id": None, "manifest": None}
        try:
            path, manifest = registry.prepare(page_id, run_type)
            context.update(run_id=manifest["run_id"], manifest=registry.relative_path(path))
            attempt = manifest["attempts"][-1]["number"]
            trace_path = registry.project_path / "evals/results/runs" / manifest["run_id"] / f"attempt-{attempt}.jsonl"
            prompt = build_agent_prompt(registry, path, manifest)
            try:
                agent_run = run_agent(page_id=page_id, prompt=prompt, trace_path=trace_path, workdir=registry.project_path)
            except BaseException:
                try:
                    registry.finish_attempt(path, manifest, None)
                except (OSError, RuntimeError) as error:
                    print("Не удалось завершить запись попытки:", error)
                raise
            registry.finish_attempt(path, manifest, agent_run["return_code"])
            evidence = collect_evidence(registry, path, trace_path)
        except EvalProcessCleanupError:
            raise
        except (OSError, RuntimeError) as error:
            print("Ошибка запуска eval:", error)
            eval_results.extend(error_result(case, context, error) for case in page_eval_cases)
            continue

        agent_response = agent_run["agent_response"]
        if agent_run["return_code"] != 0 or not agent_response:
            reason = "QA Agent завершился с ошибкой." if agent_run["return_code"] != 0 else "QA Agent завершился без текстового ответа."
            report_agent_diagnostics(
                reason, agent_run["return_code"], agent_run["stderr"],
                agent_run["error_events"], agent_run["transcript"], agent_response,
            )
            eval_results.extend(error_result(case, context, reason) for case in page_eval_cases)
            continue

        try:
            validate_eval_handoff(evidence)
        except EvalContextError as error:
            print("Некорректный eval handoff:", error)
            eval_results.extend(error_result(case, context, error) for case in page_eval_cases)
            continue

        print()
        print("ОТВЕТ QA AGENT")
        print(agent_response)
        for eval_case in page_eval_cases:
            eval_case_id = eval_case.get("eval_id", eval_case.get("id"))
            print()
            print(f"Evaluation: {eval_case_id} — {eval_case['name']}")
            try:
                evaluation_result = evaluate_response(eval_case, agent_response, evidence=evidence, workdir=registry.project_path)
                print(json.dumps(evaluation_result, ensure_ascii=False, indent=2))
                eval_results.append({
                    "eval_id": eval_case_id,
                    "name": eval_case["name"],
                    **context,
                    "status": evaluation_result["status"],
                    "score": evaluation_result["score"],
                    "expected_properties": evaluation_result["expected_properties"],
                    "forbidden_behavior": evaluation_result["forbidden_behavior"],
                })
            except EvalProcessCleanupError:
                raise
            except Exception as error:
                print("Ошибка Evaluator:", error)
                eval_results.append(error_result(eval_case, context, error))
    return eval_results


# =============================================================
# EVALUATION SUMMARY
# =============================================================

def summarize_results(eval_results):
    total = len(eval_results)
    passed = sum(result["status"] == "PASS" for result in eval_results)
    failed = sum(result["status"] == "FAIL" for result in eval_results)
    errors = sum(result["status"] == "ERROR" for result in eval_results)
    pass_rate = passed / total * 100 if total else 0.0
    average_score = sum(result["score"] for result in eval_results) / total if total else 0.0
    properties = [prop for result in eval_results for prop in result.get("expected_properties", [])]
    forbidden = [rule for result in eval_results for rule in result.get("forbidden_behavior", [])]
    passed_properties = sum(prop.get("passed") is True for prop in properties)
    violations = sum(rule.get("violated") is True for rule in forbidden)
    properties_rate = passed_properties / len(properties) * 100 if properties else 0.0
    violation_rate = violations / len(forbidden) * 100 if forbidden else 0.0
    criteria = {
        "pass_rate": {"threshold": MIN_PASS_RATE, "actual": pass_rate, "passed": pass_rate >= MIN_PASS_RATE},
        "expected_properties_pass_rate": {"threshold": MIN_EXPECTED_PROPERTIES_PASS_RATE, "actual": properties_rate, "passed": properties_rate >= MIN_EXPECTED_PROPERTIES_PASS_RATE},
        "forbidden_behavior_violation_rate": {"threshold": MAX_FORBIDDEN_BEHAVIOR_VIOLATION_RATE, "actual": violation_rate, "passed": violation_rate <= MAX_FORBIDDEN_BEHAVIOR_VIOLATION_RATE},
        "errors": {"threshold": MAX_ERRORS, "actual": errors, "passed": errors <= MAX_ERRORS},
    }
    overall_status = "PASS" if all(criterion["passed"] for criterion in criteria.values()) else "FAIL"
    print()
    print("=" * 80)
    print("EVALUATION SUMMARY")
    print(f"Total: {total}, Passed: {passed}, Failed: {failed}, Errors: {errors}")
    print(f"Pass Rate: {pass_rate:.1f}%, Average Score: {average_score:.2f}")
    print(f"Expected Properties: {passed_properties}/{len(properties)} ({properties_rate:.1f}%)")
    print(f"Forbidden Behaviors Violated: {violations}/{len(forbidden)} ({violation_rate:.1f}%)")
    for result in eval_results:
        print(f"{result['eval_id']:<15}{result['status']:<10}Score: {result['score']} ({result['run_type']})")
    print("QUALITY GATE STATUS:", overall_status)
    print("=" * 80)
    return {
        "summary": {
            "total": total, "passed": passed, "failed": failed, "errors": errors,
            "pass_rate": round(pass_rate, 1), "average_score": round(average_score, 2),
            "expected_properties_pass_rate": round(properties_rate, 1),
            "forbidden_behavior_violation_rate": round(violation_rate, 1),
            "overall_status": overall_status,
        },
        "metrics": {
            "expected_properties": {"total": len(properties), "passed": passed_properties, "pass_rate": round(properties_rate, 1)},
            "forbidden_behavior": {"total": len(forbidden), "violated": violations, "violation_rate": round(violation_rate, 1)},
        },
        "quality_gate": {"status": overall_status, "criteria": criteria},
        "results": eval_results,
    }


def main():
    global DEBUG
    DEBUG = parse_args().debug
    try:
        groups = load_eval_groups(dataset_path)
    except (OSError, ValueError, EvalContextError) as error:
        print("Некорректный golden dataset:", error)
        return 2
    try:
        report = summarize_results(run_eval_groups(groups))
    except (OSError, RuntimeError) as error:
        print("Eval остановлен:", error)
        return 2
    except KeyboardInterrupt:
        print("Eval прерван; сохранённый fresh можно продолжить через run_type: resume.")
        return 130
    results_dir = Path(__file__).parent / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    results_file = results_dir / "eval_results.json"
    with results_file.open("w", encoding="utf-8") as file:
        json.dump(report, file, ensure_ascii=False, indent=2)
    print(f"Результаты сохранены: {results_file}")
    return 0 if report["quality_gate"]["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

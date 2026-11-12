import argparse
import json
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

from evaluator import evaluate_response


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


DEBUG = parse_args().debug


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

with open(dataset_path, "r", encoding="utf-8") as file:
    eval_cases = json.load(file)

eval_results = []


# =============================================================
# ГРУППИРОВКА EVAL CASES ПО СТРАНИЦЕ CONFLUENCE
# =============================================================

cases_by_page = {}

for eval_case in eval_cases:
    eval_input = eval_case.get("input_data", eval_case.get("input"))
    eval_case_id = eval_case.get("eval_id", eval_case.get("id"))
    source = eval_input.get("source", "").lower()

    if source != "confluence":
        raise RuntimeError(f"Eval Case {eval_case_id} использует неподдерживаемый источник: {source}")

    page_id = eval_input["page_id"]

    if page_id not in cases_by_page:
        cases_by_page[page_id] = []

    cases_by_page[page_id].append(eval_case)


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
        print("Полный вывод агента: запусти скрипт с флагом --debug")


def save_debug_transcript(page_id, prompt, command, transcript, stderr,
                          agent_response, return_code):
    """Сохранить полный вывод агента в файл (только в debug-режиме)."""
    debug_dir = Path(__file__).parent / "results" / "debug"
    debug_dir.mkdir(parents=True, exist_ok=True)

    timestamp = time.strftime("%y%m%d_%H%M%S")
    log_file = debug_dir / f"{timestamp}_page_{page_id}.log"

    with open(log_file, "w", encoding="utf-8") as file:
        file.write("COMMAND:\n")
        file.write(" ".join(command[:-1]) + " <prompt>\n\n")
        file.write("PROMPT:\n")
        file.write(prompt + "\n\n")
        file.write("RETURN CODE:\n")
        file.write(f"{return_code}\n\n")
        file.write("STDERR:\n")
        file.write((stderr or "<пусто>") + "\n\n")
        file.write("AGENT RESPONSE:\n")
        file.write((agent_response or "<пусто>") + "\n\n")
        file.write("RAW EVENTS:\n")
        for raw_line in transcript:
            file.write(raw_line + "\n")

    print(f"DEBUG: полный вывод агента сохранён: {log_file}")


# =============================================================
# ЗАПУСК QA AGENT
# =============================================================

def run_agent(page_id, prompt):
    """Запустить QA Agent и собрать ответ + диагностику."""

    command = [
        "opencode.cmd",
        "run",
        "-m",
        "openrouter/openrouter/free",
        "--format",
        "json",
        prompt
    ]

    debug_print()
    debug_print("КОМАНДА ЗАПУСКА:", " ".join(command[:-1]), "<prompt>")
    debug_print("PROMPT:", prompt)
    debug_print()

    process = subprocess.Popen(
        command,
        cwd=project_path,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1
    )

    transcript = deque(maxlen=MAX_TRANSCRIPT_LINES)
    stderr_lines = []
    text_parts = []
    error_events = []

    # stderr читается параллельно: иначе при заполнении пайпа
    # процесс агента может заблокироваться, а причина падения потеряться
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

    # =========================================================
    # ЧТЕНИЕ JSON EVENTS
    # =========================================================

    if process.stdout:
        for raw_line in process.stdout:
            line = raw_line.strip()
            if not line:
                continue

            transcript.append(line)

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                print("Не удалось разобрать событие OpenCode", flush=True)
                debug_print("  RAW:", line[:MAX_DEBUG_PAYLOAD_CHARS])
                continue

            event_type = event.get("type")
            print("AGENT EVENT:", event_type, flush=True)

            if event_type == "text":
                part = event.get("part") or {}
                text = part.get("text")
                if text:
                    text_parts.append(text)
                    debug_print(text)

            elif event_type in ("tool", "tool_use", "tool_call"):
                part = event.get("part") or {}
                state = part.get("state") or {}
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

            # В debug-режиме показываем событие целиком
            debug_print("  RAW:", line[:MAX_DEBUG_PAYLOAD_CHARS])

    # =========================================================
    # ЗАВЕРШЕНИЕ QA AGENT
    # =========================================================

    return_code = process.wait()
    stderr_thread.join(timeout=5)

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
            return_code=return_code
        )

    return {
        "return_code": return_code,
        "stderr": stderr,
        "agent_response": agent_response,
        "error_events": error_events,
        "transcript": transcript,
    }


# =============================================================
# ЗАПУСК QA AGENT
# =============================================================

for page_id, page_eval_cases in cases_by_page.items():
    print()
    print("=" * 80)
    print("ЗАПУСК QA AGENT")
    print("=" * 80)
    print()
    print("Источник требований: Confluence")
    print(f"pageId: {page_id}")
    print(f"Количество Eval Cases для этой страницы: {len(page_eval_cases)}")
    print(f"Режим отладки (--debug): {'ВКЛ' if DEBUG else 'ВЫКЛ'}")
    print()

    # ОБНОВЛЕННЫЙ ПРОМПТ (добавлено жесткое правило остановки)
    prompt = (
        f"Выполни флоу агента "
        f"требования из Confluence "
        f"со страницы pageId={page_id} "
        f"Даю разрешение на публикацию результатов для паблишеров. "
        f"Jira: cloudId: a6b9202f-c2db-401f-85e1-a2ec85eb1ea2, projectKey: SCRUM, projectId: 10000, "
        f"issueTypeName: Задание, issueTypeId: 10003, unspecifiedPriority: omit (Jira default Medium/3), "
        f"links: Blocks/10000, Relates/10003"
        f"Qase: projectCode: TMS, priority: HIGH=1, MEDIUM=2, LOW=3, type: omit, behavior: omit, "
        f"modelTypeTag: model-type:<value>, customFields: none"
   )

    # =========================================================
    # ЗАПУСК OPENCODE
    # =========================================================

    agent_run = run_agent(page_id=page_id, prompt=prompt)

    return_code = agent_run["return_code"]
    agent_response = agent_run["agent_response"]

    # =========================================================
    # ОБРАБОТКА ОШИБКИ QA AGENT
    # =========================================================

    if return_code != 0:
        report_agent_diagnostics(
            reason="QA Agent завершился с ошибкой.",
            return_code=return_code,
            stderr=agent_run["stderr"],
            error_events=agent_run["error_events"],
            transcript=agent_run["transcript"],
            agent_response=agent_response
        )

        for eval_case in page_eval_cases:
            eval_case_id = eval_case.get("eval_id", eval_case.get("id"))
            eval_results.append({
                "eval_id": eval_case_id,
                "name": eval_case["name"],
                "status": "ERROR",
                "score": 0.0,
                "expected_properties": [],
                "forbidden_behavior": []
            })
        continue

    # =========================================================
    # ПРОВЕРКА ПУСТОГО ОТВЕТА
    # =========================================================

    if not agent_response:
        print("QA Agent не вернул текстовый ответ.")
        report_agent_diagnostics(
            reason="QA Agent завершился без текстового ответа.",
            return_code=return_code,
            stderr=agent_run["stderr"],
            error_events=agent_run["error_events"],
            transcript=agent_run["transcript"],
            agent_response=agent_response
        )
        for eval_case in page_eval_cases:
            eval_case_id = eval_case.get("eval_id", eval_case.get("id"))
            eval_results.append({
                "eval_id": eval_case_id,
                "name": eval_case["name"],
                "status": "ERROR",
                "score": 0.0,
                "expected_properties": [],
                "forbidden_behavior": []
            })
        continue

    # =========================================================
    # ВЫВОД ОТВЕТА QA AGENT
    # =========================================================

    print()
    print("=" * 80)
    print("ОТВЕТ QA AGENT")
    print("=" * 80)
    print(agent_response)
    print("=" * 80)

    # =========================================================
    # EVALUATION
    # =========================================================

    for eval_case in page_eval_cases:
        eval_case_id = eval_case.get("eval_id", eval_case.get("id"))

        print()
        print()
        print("-" * 80)
        print(f"Evaluation: {eval_case_id}")
        print(f"Название: {eval_case['name']}")
        print("-" * 80)
        print()
        print("Запускаем Evaluator...")

        try:
            evaluation_result = evaluate_response(eval_case, agent_response)

            print()
            print("=" * 80)
            print("РЕЗУЛЬТАТ EVALUATION")
            print("=" * 80)
            print(json.dumps(evaluation_result, ensure_ascii=False, indent=2))
            print("=" * 80)

            status = evaluation_result["status"]
            score = evaluation_result["score"]

            eval_results.append({
                "eval_id": eval_case_id,
                "name": eval_case["name"],
                "status": status,
                "score": score,
                "expected_properties": evaluation_result["expected_properties"],
                "forbidden_behavior": evaluation_result["forbidden_behavior"]
            })

            print()
            print("EVAL CASE:", eval_case_id)
            print("STATUS:", status)
            print("SCORE:", score)

        except Exception as error:
            print()
            print("Ошибка Evaluator:")
            print(error)

            eval_results.append({
                "eval_id": eval_case_id,
                "name": eval_case["name"],
                "status": "ERROR",
                "score": 0.0,
                "expected_properties": [],
                "forbidden_behavior": []
            })

# =============================================================
# EVALUATION SUMMARY
# =============================================================

print()
print()
print("=" * 80)
print("EVALUATION SUMMARY")
print("=" * 80)

total = len(eval_results)
passed = sum(1 for result in eval_results if result["status"] == "PASS")
failed = sum(1 for result in eval_results if result["status"] == "FAIL")
errors = sum(1 for result in eval_results if result["status"] == "ERROR")

pass_rate = (passed / total) * 100 if total > 0 else 0.0
average_score = (sum(result["score"] for result in eval_results) / total) if total > 0 else 0.0

total_expected_properties = 0
passed_expected_properties = 0

for result in eval_results:
    expected_properties = result.get("expected_properties", [])
    for expected_property in expected_properties:
        total_expected_properties += 1
        if expected_property.get("passed") is True:
            passed_expected_properties += 1

expected_properties_pass_rate = (
    (passed_expected_properties / total_expected_properties) * 100
    if total_expected_properties > 0 else 0.0
)

total_forbidden_behaviors = 0
violated_forbidden_behaviors = 0

for result in eval_results:
    forbidden_behaviors = result.get("forbidden_behavior", [])
    for forbidden_behavior in forbidden_behaviors:
        total_forbidden_behaviors += 1
        if forbidden_behavior.get("violated") is True:
            violated_forbidden_behaviors += 1

forbidden_behavior_violation_rate = (
    (violated_forbidden_behaviors / total_forbidden_behaviors) * 100
    if total_forbidden_behaviors > 0 else 0.0
)

print()
print(f"Total:         {total}")
print(f"Passed:        {passed}")
print(f"Failed:        {failed}")
print(f"Errors:        {errors}")
print(f"Pass Rate:     {pass_rate:.1f}%")
print(f"Average Score: {average_score:.2f}")

print()
print("QUALITY METRICS")
print("-" * 80)
print(f"Expected Properties:                 {passed_expected_properties}/{total_expected_properties}")
print(f"Expected Properties Pass Rate:       {expected_properties_pass_rate:.1f}%")
print(f"Forbidden Behaviors Violated:        {violated_forbidden_behaviors}/{total_forbidden_behaviors}")
print(f"Forbidden Behavior Violation Rate:   {forbidden_behavior_violation_rate:.1f}%")

print()
print("-" * 80)

for result in eval_results:
    print(f"{result['eval_id']:<10}{result['status']:<10}Score: {result['score']}")

print("-" * 80)

pass_rate_gate = (pass_rate >= MIN_PASS_RATE)
expected_properties_gate = (expected_properties_pass_rate >= MIN_EXPECTED_PROPERTIES_PASS_RATE)
forbidden_behavior_gate = (forbidden_behavior_violation_rate <= MAX_FORBIDDEN_BEHAVIOR_VIOLATION_RATE)
errors_gate = (errors <= MAX_ERRORS)

quality_gate_passed = all([pass_rate_gate, expected_properties_gate, forbidden_behavior_gate, errors_gate])
overall_status = "PASS" if quality_gate_passed else "FAIL"

print()
print("=" * 80)
print("QUALITY GATE")
print("=" * 80)
print()
print(f"Pass Rate >= {MIN_PASS_RATE:.1f}%: {'PASS' if pass_rate_gate else 'FAIL'} (actual: {pass_rate:.1f}%)")
print(f"Expected Properties >= {MIN_EXPECTED_PROPERTIES_PASS_RATE:.1f}%: {'PASS' if expected_properties_gate else 'FAIL'} (actual: {expected_properties_pass_rate:.1f}%)")
print(f"Forbidden Behavior <= {MAX_FORBIDDEN_BEHAVIOR_VIOLATION_RATE:.1f}%: {'PASS' if forbidden_behavior_gate else 'FAIL'} (actual: {forbidden_behavior_violation_rate:.1f}%)")
print(f"Errors <= {MAX_ERRORS}: {'PASS' if errors_gate else 'FAIL'} (actual: {errors})")
print()
print("QUALITY GATE STATUS:", overall_status)
print("=" * 80)

results_dir = (Path(__file__).parent / "results")
results_dir.mkdir(parents=True, exist_ok=True)
results_file = (results_dir / "eval_results.json")

report = {
    "summary": {
        "total": total,
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "pass_rate": round(pass_rate, 1),
        "average_score": round(average_score, 2),
        "expected_properties_pass_rate": round(expected_properties_pass_rate, 1),
        "forbidden_behavior_violation_rate": round(forbidden_behavior_violation_rate, 1),
        "overall_status": overall_status
    },
    "metrics": {
        "expected_properties": {
            "total": total_expected_properties,
            "passed": passed_expected_properties,
            "pass_rate": round(expected_properties_pass_rate, 1)
        },
        "forbidden_behavior": {
            "total": total_forbidden_behaviors,
            "violated": violated_forbidden_behaviors,
            "violation_rate": round(forbidden_behavior_violation_rate, 1)
        }
    },
    "quality_gate": {
        "status": overall_status,
        "criteria": {
            "pass_rate": {"threshold": MIN_PASS_RATE, "actual": round(pass_rate, 1), "passed": pass_rate_gate},
            "expected_properties_pass_rate": {"threshold": MIN_EXPECTED_PROPERTIES_PASS_RATE, "actual": round(expected_properties_pass_rate, 1), "passed": expected_properties_gate},
            "forbidden_behavior_violation_rate": {"threshold": MAX_FORBIDDEN_BEHAVIOR_VIOLATION_RATE, "actual": round(forbidden_behavior_violation_rate, 1), "passed": forbidden_behavior_gate},
            "errors": {"threshold": MAX_ERRORS, "actual": errors, "passed": errors_gate}
        }
    },
    "results": eval_results
}

with open(results_file, "w", encoding="utf-8") as file:
    json.dump(report, file, ensure_ascii=False, indent=2)

print()
print(f"Результаты сохранены: {results_file}")
import json
import subprocess
from pathlib import Path

from evaluator import evaluate_response


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
    print()

    # ОБНОВЛЕННЫЙ ПРОМПТ (добавлено жесткое правило остановки)
    prompt = (
        f"Выполни флоу агента "
        f"требования из Confluence "
        f"со страницы pageId={page_id} "
        f"Даю разрешение на публикацию результатов для паблишеров. В Qase используй проект TMS"
    )

    # =========================================================
    # ЗАПУСК OPENCODE
    # =========================================================

    process = subprocess.Popen(
        [
            "opencode.cmd",
            "run",
            "-m",
            "tokenharbor/deepseek-v4.1-flash:free",
            "--format",
            "json",
            prompt
        ],
        cwd=project_path,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1
    )

    text_parts = []
    print("QA Agent запущен...")

    # =========================================================
    # ЧТЕНИЕ JSON EVENTS
    # =========================================================

    if process.stdout:
        for line in process.stdout:
            line = line.strip()
            if not line:
                continue

            try:
                event = json.loads(line)
                event_type = event.get("type")
                print("AGENT EVENT:", event_type, flush=True)

                if event_type == "text":
                    part = event.get("part", {})
                    text = part.get("text")
                    if text:
                        text_parts.append(text)

            except json.JSONDecodeError:
                print("Не удалось разобрать событие OpenCode", flush=True)

    # =========================================================
    # ЗАВЕРШЕНИЕ QA AGENT
    # =========================================================

    return_code = process.wait()
    agent_response = "\n".join(text_parts).strip()

    print()
    print("QA AGENT RETURN CODE:", return_code)

    # =========================================================
    # ОБРАБОТКА ОШИБКИ QA AGENT
    # =========================================================

    if return_code != 0:
        print("QA Agent завершился с ошибкой.")
        stderr = ""
        if process.stderr:
            stderr = process.stderr.read()
        if stderr:
            print(stderr)

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
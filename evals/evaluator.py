import json
import subprocess
from pathlib import Path


# =============================================================
# PATHS
# =============================================================

# Корень проекта
project_path = Path(__file__).resolve().parent.parent


# =============================================================
# EVALUATOR
# =============================================================

def evaluate_response(eval_case, agent_response):

    # ---------------------------------------------------------
    # INPUT EVAL CASE
    # ---------------------------------------------------------

    eval_input = eval_case.get("input_data", eval_case.get("input"))
    eval_case_id = eval_case.get("eval_id", eval_case.get("id"))
    source = eval_input.get("source", "").lower()


    # ---------------------------------------------------------
    # ОПИСАНИЕ ИСТОЧНИКА
    # ---------------------------------------------------------

    if source == "inline":
        input_description = eval_input.get("requirements", "")
    elif source == "confluence":
        input_description = (
            f"Требования находятся в Confluence. "
            f"pageId={eval_input.get('page_id')}."
        )
    else:
        input_description = str(eval_input)


    # =========================================================
    # PROMPT ДЛЯ LLM-AS-A-JUDGE
    # =========================================================

    expected_properties = eval_case.get("expected_behavior", eval_case.get("expected_properties", []))
    forbidden_behavior = eval_case.get("forbidden_behavior", [])

    judge_prompt = f"""
Ты — независимый Evaluator результатов AI QA Agent.
Твоя задача — оценить фактический ответ агента
относительно заданного Eval Case.
ПРАВИЛА ОЦЕНКИ:
1. Оценивай смысл ответа, а не точное совпадение формулировок.
2. Не добавляй собственные требования.
3. Каждое Expected Property оцени отдельно.
4. Каждое Forbidden Behavior оцени отдельно.
5. Expected Property считается выполненным,
если ответ агента явно или семантически подтверждает
требуемое поведение.
6. Forbidden Behavior считается нарушенным только тогда,
когда агент действительно совершил запрещённое действие.
7. Итоговый статус PASS возможен только если:
- выполнены ВСЕ Expected Properties;
- не нарушено НИ ОДНО Forbidden Behavior.
8. Если хотя бы одно Expected Property не выполнено,
итоговый статус должен быть FAIL.
9. Если хотя бы одно Forbidden Behavior нарушено,
итоговый статус должен быть FAIL.
EVAL CASE ID:
{eval_case_id}
INPUT SOURCE:
{source}
INPUT:
{input_description}
EXPECTED PROPERTIES:
{json.dumps(expected_properties, ensure_ascii=False, indent=2)}
FORBIDDEN BEHAVIOR:
{json.dumps(forbidden_behavior, ensure_ascii=False, indent=2)}
ACTUAL AGENT RESPONSE:
{agent_response}
Верни ТОЛЬКО валидный JSON следующей структуры:
{{
  "eval_case_id": "{eval_case_id}",
  "expected_properties": [
    {{
      "property": "текст проверяемого Expected Property",
      "passed": true,
      "reason": "краткое объяснение решения"
    }}
  ],
  "forbidden_behavior": [
    {{
      "rule": "текст проверяемого Forbidden Behavior",
      "violated": false,
      "reason": "краткое объяснение решения"
    }}
  ],
  "status": "PASS",
  "score": 1.0
}}
ПРАВИЛА SCORE:
1.0 — PASS.
0.0 — FAIL.
ВАЖНО:
Не используй Markdown.
Не используй ```json.
Не используй ```.
Не добавляй пояснения до JSON.
Не добавляй пояснения после JSON.
Ответ должен содержать только один JSON-объект.
"""


    # =========================================================
    # ЗАПУСК EVALUATOR (ЧЕРЕЗ STDIN)
    # =========================================================

    process = subprocess.Popen(
        [
            "opencode.cmd",
            "run",
            "-m",
            "tokenharbor/deepseek-v4.1-flash:free",
            "--format",
            "json",
        ],
        cwd=project_path,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1
    )

    text_parts = []
    print("Evaluator запущен...")

    # Передача огромного промпта через stdin, чтобы избежать ошибки длины
    process.stdin.write(judge_prompt)
    process.stdin.close()

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
                print("EVALUATOR EVENT:", event_type, flush=True)

                if event_type == "text":
                    part = event.get("part", {})
                    text = part.get("text")
                    if text:
                        text_parts.append(text)

            except json.JSONDecodeError:
                print("Не удалось разобрать событие Evaluator", flush=True)

    # =========================================================
    # ЗАВЕРШЕНИЕ EVALUATOR
    # =========================================================

    return_code = process.wait()
    print("EVALUATOR RETURN CODE:", return_code)

    if return_code != 0:
        stderr = ""
        if process.stderr:
            stderr = process.stderr.read()
        raise RuntimeError(
            f"Evaluator завершился с ошибкой. "
            f"Return code: {return_code}. "
            f"STDERR: {stderr}"
        )

    # =========================================================
    # СБОРКА ОТВЕТА
    # =========================================================

    judge_response = "\n".join(text_parts).strip()

    if not judge_response:
        raise RuntimeError("Evaluator не вернул текстовый ответ.")

    # =========================================================
    # УДАЛЕНИЕ MARKDOWN CODE BLOCK
    # =========================================================

    if judge_response.startswith("```json"):
        judge_response = judge_response[7:]
    elif judge_response.startswith("```"):
        judge_response = judge_response[3:]

    if judge_response.endswith("```"):
        judge_response = judge_response[:-3]

    judge_response = judge_response.strip()

    # =========================================================
    # JSON PARSING
    # =========================================================

    try:
        evaluation_result = json.loads(judge_response)
    except json.JSONDecodeError as error:
        print()
        print("Не удалось разобрать ответ Evaluator как JSON.")
        print()
        print("Фактический ответ Evaluator:")
        print(judge_response)
        raise RuntimeError("Evaluator вернул невалидный JSON.") from error

    # =========================================================
    # ПРОВЕРКА СТРУКТУРЫ
    # =========================================================

    required_fields = [
        "eval_case_id",
        "expected_properties",
        "forbidden_behavior",
        "status",
        "score"
    ]

    for field in required_fields:
        if field not in evaluation_result:
            raise RuntimeError(
                f"В ответе Evaluator отсутствует обязательное поле: {field}"
            )

    # =========================================================
    # RETURN
    # =========================================================

    return evaluation_result
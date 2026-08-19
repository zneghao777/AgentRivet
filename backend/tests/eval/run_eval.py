"""LangSmith 评测脚本骨架（评测预留，SHOULD）。

示例：exact_match / llm_judge。
"""

from langsmith import Client
from langsmith.evaluation import evaluate


def exact_match(outputs: dict, reference_outputs: dict) -> dict:
    pred = (outputs.get("answer") or "").strip()
    ref = (reference_outputs.get("answer") or "").strip()
    return {"key": "exact_match", "score": pred == ref}


def main() -> None:
    client = Client()
    evaluate(
        lambda inputs: {"answer": "placeholder"},
        data="harness-mvp",
        evaluators=[exact_match],
        experiment_prefix="harness-eval",
        client=client,
    )


if __name__ == "__main__":
    main()

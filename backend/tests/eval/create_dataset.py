"""LangSmith 数据集创建脚本（评测预留，SHOULD）。

用法：
    python -m tests.eval.create_dataset
"""

from langsmith import Client


def main() -> None:
    client = Client()
    dataset = client.create_dataset(
        dataset_name="harness-mvp",
        description="Agent Harness MVP 基础评测数据集",
    )
    examples = [
        {"inputs": {"question": "现在几点？"}, "outputs": {"answer": ""}},
        {"inputs": {"question": "你好"}, "outputs": {"answer": ""}},
    ]
    client.create_examples(
        inputs=[e["inputs"] for e in examples],
        outputs=[e["outputs"] for e in examples],
        dataset_id=dataset.id,
    )
    print(f"数据集 {dataset.name} 创建完成，共 {len(examples)} 条示例")


if __name__ == "__main__":
    main()

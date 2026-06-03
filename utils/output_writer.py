# utils/output_writer.py

from pathlib import Path
import pandas as pd


def save_results_to_excel(results):

    output_dir = Path("output")
    output_dir.mkdir(exist_ok=True)

    df = pd.DataFrame(results)

    for col in ["request_types", "expected_data"]:
        if col in df.columns:
            df[col] = df[col].apply(
                lambda x: ", ".join(x) if isinstance(x, list) else x
            )

    output_file = output_dir / "request_intent_results.xlsx"

    df.to_excel(
        output_file,
        index=False
    )

    print(f"Results saved to {output_file}")

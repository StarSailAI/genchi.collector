from __future__ import annotations

import subprocess
from pathlib import Path


def main() -> None:
    app = Path(__file__).with_name("app.py")
    raise SystemExit(
        subprocess.call(
            [
                "streamlit",
                "run",
                str(app),
                "--server.address",
                "0.0.0.0",
                "--server.port",
                "8050",
                "--server.headless",
                "true",
                "--theme.base",
                "dark",
                "--theme.primaryColor",
                "#58a6ff",
                "--theme.backgroundColor",
                "#0b0f14",
                "--theme.secondaryBackgroundColor",
                "#111821",
                "--theme.textColor",
                "#d9e2ec",
            ]
        )
    )


if __name__ == "__main__":
    main()

"""eval/__main__.py：允许 python -m eval ... 调用。

用法：
  python -m eval --baseline --dry-run
  python -m eval --compare report_a.json report_b.json
"""

from eval.run import main

if __name__ == "__main__":
    main()

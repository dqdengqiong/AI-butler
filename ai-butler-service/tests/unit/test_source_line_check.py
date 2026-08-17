from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


def _load_checker() -> ModuleType:
    script = Path(__file__).parents[2] / "scripts" / "check_source_lines.py"
    spec = importlib.util.spec_from_file_location("check_source_lines", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_effective_line_count_ignores_blanks_and_comment_only_lines(tmp_path: Path) -> None:
    checker = _load_checker()
    source = tmp_path / "sample.py"
    source.write_text(
        '\n# comment\nvalue = 1  # inline comment\n"""docstring\ntext\n"""\n',
        encoding="utf-8",
    )

    assert checker.effective_line_count(source) == 4


def test_nested_source_at_limit_passes_and_one_line_over_fails(tmp_path: Path) -> None:
    checker = _load_checker()
    nested = tmp_path / "package" / "nested"
    nested.mkdir(parents=True)
    source = nested / "module.py"
    source.write_text("value = 1\n" * 500, encoding="utf-8")

    assert checker.find_violations([tmp_path], 500) == []
    assert checker.main(["--max-lines", "500", str(tmp_path)]) == 0

    source.write_text("value = 1\n" * 501, encoding="utf-8")

    assert checker.find_violations([tmp_path], 500) == [(source, 501)]
    assert checker.main(["--max-lines", "500", str(tmp_path)]) == 1

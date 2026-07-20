# remold

Concisely reshape Python code with [LibCST](https://github.com/Instagram/LibCST) or [ast-grep](https://ast-grep.github.io/).

```bash
pip install remold
```

Usage docs live in the module docstring, where remold's main users (AI agents) read them: `doc(remold)` in a pyskills session, or `help(remold)` anywhere.

## Development

```bash
pip install -e .[dev]
pytest -q
```

Version lives in `remold/__init__.py` as `__version__`; bump with `ship-bump`. Release with `ship-gh` and `ship-pypi`.

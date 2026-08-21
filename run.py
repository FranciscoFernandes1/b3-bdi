#!/usr/bin/env python
"""Ponto de entrada do pipeline.

Existe para dispensar a variável PYTHONPATH, cuja sintaxe muda de shell para
shell (`PYTHONPATH=src cmd` no bash, `$env:PYTHONPATH="src"` no PowerShell,
`set PYTHONPATH=src` no cmd). Aqui o caminho é resolvido em Python, então o
mesmo comando funciona em todos:

    python run.py descobrir --date 31-07-2026
    python run.py sync
    python run.py sync --force
    python run.py reprocessar

Funciona também chamado de outro diretório — os caminhos são resolvidos a
partir da localização deste arquivo, não do diretório atual.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from b3_bdi.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())

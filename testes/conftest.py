"""Banco de teste: esquema real, dado nenhum.

Os testes das invariantes rodam contra o esquema de verdade (`db.init_db`),
nunca contra o banco do usuário — `UFMG_DATA_DIR` é redirecionado para um
diretório temporário antes de qualquer import que leia caminho.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))


@pytest.fixture()
def con(tmp_path, monkeypatch) -> sqlite3.Connection:
    monkeypatch.setenv("UFMG_DATA_DIR", str(tmp_path))
    import db

    c = db.conectar(tmp_path / "teste.db")
    db.init_db(c)
    yield c
    c.close()


class ClienteFalso:
    """Substituto do MoodleClient que registra o que foi chamado.

    `erro` faz a chamada falhar, que é como se testa que a falha também vai
    para o log.
    """

    def __init__(self, erro: Exception | None = None):
        self.alias = "teste"
        self.chamadas: list[tuple[str, dict]] = []
        self.erro = erro

    def call(self, funcao: str, **params):
        self.chamadas.append((funcao, params))
        if self.erro:
            raise self.erro
        return {"ok": True}


@pytest.fixture()
def cli() -> ClienteFalso:
    return ClienteFalso()

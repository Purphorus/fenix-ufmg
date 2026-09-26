"""Conferência humana dos rótulos: o usuário julga uma amostra às cegas.

  .venv/bin/python -m avaliacao.conferir            julga (retoma de onde parou)
  .venv/bin/python -m avaliacao.conferir --resultado concordância com os rótulos

Os 1.332 rótulos de `rotulos.json` foram feitos pelo assistente. Se o critério
dele não bate com o do usuário, toda medição da busca vale menos do que parece
— e só o usuário pode dizer isso. A amostra é estratificada (metade que o
assistente marcou "s", metade "n", consulta e documento) e mostrada sem o
rótulo, na ordem sorteada. O resultado é o kappa de Cohen: acima de 0,6 a
régua serve; abaixo, os rótulos precisam ser revistos antes de mais medição.
"""
from __future__ import annotations

import json
import random
import sqlite3
import sys
from pathlib import Path

import db

AQUI = Path(__file__).parent
ROTULOS = AQUI / "rotulos.json"
SAIDA = db.BASE_DIR / "avaliacao" / "conferencia.json"
POR_ESTRATO = 10          # 4 estratos (tarefa × s/n) = 40 pares
SEMENTE = 20260924
MOSTRAR = 900

CRITERIO = {
    "consulta": "O trecho permite ENTENDER ou RESPONDER a pergunta?",
    "documento": "O trecho entraria num resumo para estudar ESTE tópico?",
}


def amostra(rotulos: dict, por_estrato: int = POR_ESTRATO,
            semente: int = SEMENTE) -> list[dict]:
    """Pares sorteados por estrato, embaralhados; sem o rótulo do assistente."""
    rng = random.Random(semente)
    saida = []
    for tarefa in ("consulta", "documento"):
        for valor in ("s", "n"):
            pares = sorted((q, sha) for q, d in rotulos[tarefa].items()
                           if isinstance(d, dict) for sha, v in d.items() if v == valor)
            for q, sha in rng.sample(pares, min(por_estrato, len(pares))):
                saida.append({"tarefa": tarefa, "item": q, "sha": sha})
    rng.shuffle(saida)
    return saida


def kappa(pares: list[tuple[str, str]]) -> float:
    """Kappa de Cohen entre dois juízes s/n: concordância além do acaso."""
    n = len(pares)
    if not n:
        return 0.0
    obs = sum(a == b for a, b in pares) / n
    pa = sum(a == "s" for a, _ in pares) / n
    pb = sum(b == "s" for _, b in pares) / n
    esp = pa * pb + (1 - pa) * (1 - pb)
    return 1.0 if esp == 1 else (obs - esp) / (1 - esp)


def _ler() -> dict:
    return json.loads(SAIDA.read_text()) if SAIDA.exists() else {}


def julgar(con: sqlite3.Connection) -> None:
    rot = json.loads(ROTULOS.read_text())
    feitos = _ler()
    texto = {r[0]: r[1] for r in con.execute("SELECT sha256, texto FROM trechos")}
    fila = [p for p in amostra(rot)
            if f"{p['tarefa']}|{p['item']}|{p['sha']}" not in feitos]
    total = len(amostra(rot))
    print(f"{total - len(fila)}/{total} já julgados. s = sim, n = não, "
          "p = pular, q = sair (salva).\n")
    for i, p in enumerate(fila, total - len(fila) + 1):
        t = texto.get(p["sha"])
        if t is None:  # trecho saiu do índice desde a rotulagem
            continue
        rotulo = "PERGUNTA" if p["tarefa"] == "consulta" else "TÓPICO"
        print(f"─── {i}/{total} ── {rotulo}: {p['item']}")
        print(f"    {CRITERIO[p['tarefa']]}\n")
        print(t[:MOSTRAR] + (" …" if len(t) > MOSTRAR else ""))
        while True:
            r = input("\n[s/n/p/q] ").strip().lower()
            if r in ("s", "n", "p", "q"):
                break
        if r == "q":
            break
        if r in ("s", "n"):
            feitos[f"{p['tarefa']}|{p['item']}|{p['sha']}"] = r
            SAIDA.parent.mkdir(parents=True, exist_ok=True)
            SAIDA.write_text(json.dumps(feitos, ensure_ascii=False, indent=1))
        print()
    print(f"salvo em {SAIDA}. Resultado: .venv/bin/python -m avaliacao.conferir --resultado")


def resultado() -> str:
    rot = json.loads(ROTULOS.read_text())
    feitos = _ler()
    linhas, todos = [], []
    for tarefa in ("consulta", "documento"):
        pares = []
        for chave, humano in feitos.items():
            t, item, sha = chave.split("|", 2)
            if t != tarefa:
                continue
            assistente = rot[tarefa].get(item, {}).get(sha)
            if assistente:
                pares.append((assistente, humano))
        todos += pares
        if pares:
            conc = sum(a == b for a, b in pares) / len(pares)
            so_eu = sum(a == "s" and b == "n" for a, b in pares)
            so_voce = sum(a == "n" and b == "s" for a, b in pares)
            linhas.append(f"{tarefa:9} {len(pares):3} pares  concordância {conc:.0%}  "
                          f"kappa {kappa(pares):.2f}  (assistente s/você n: {so_eu}; "
                          f"assistente n/você s: {so_voce})")
    if not todos:
        return "nenhum par julgado ainda"
    k = kappa(todos)
    veredito = ("a régua serve" if k >= 0.6 else
                "concordância fraca: revise os rótulos antes de medir mais")
    return "\n".join(linhas + [f"total     {len(todos):3} pares  kappa {k:.2f} — {veredito}"])


if __name__ == "__main__":
    if "--resultado" in sys.argv:
        print(resultado())
    else:
        julgar(db.conectar())

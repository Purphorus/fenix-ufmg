"""Simulado montado a partir do material da própria disciplina.

O caminho de questionário deste projeto sempre foi de fora para dentro: o
Moodle tem um quiz, `companion.py` lê, o modelo propõe, você revisa. Faltava o
contrário — não há quiz no Moodle para a maior parte da matéria, e a prova é
presencial.

Três decisões que este módulo NÃO delega ao modelo, porque em Python custam
zero token e não esquecem:

1. **Questão sem fonte é recusada na gravação.** É a invariante 7 onde ela mais
   importa: questão inventada sobre matéria que o material não cobre é o pior
   resultado possível aqui, e é exatamente o que ninguém percebe ao estudar por
   ela. `fonte_trecho` obrigatório, e o sha do trecho junto.
2. **O aviso de cobertura vem antes do material.** `busca.aviso_cobertura` pega
   6 de 6 consultas sobre assunto ausente; se ele disparar, o material vai
   junto com o aviso e a decisão é sua, não do modelo em silêncio.
3. **O que já foi perguntado é mostrado sem ser pedido.** Um segundo simulado
   do mesmo tópico repetia as mesmas questões, porque nada lembrava o modelo do
   que já existia — e pedir essa contabilidade a ele custa token e falha.

O gabarito fica separado do enunciado, e não intercalado: você resolve antes de
ler a resposta, e resposta ao lado do enunciado estraga isso.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass

import busca
import memoria

# Quantos trechos sustentam um simulado. Seis dá material para cinco ou seis
# questões sem estourar o orçamento de token da chamada.
TRECHOS_PADRAO = 6

# Orçamento maior que o da busca pontual: aqui o modelo precisa do enunciado
# inteiro para não inventar número, e não de uma amostra para responder.
ORCAMENTO = 9000

ESCOPO = "simulado"


def agora_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


@dataclass
class Guardadas:
    salvas: int
    recusadas: list[str]
    ids: list[int]

    def __str__(self) -> str:
        s = f"✓ {self.salvas} questão(ões) guardada(s)"
        if self.recusadas:
            s += "\n" + "\n".join(f"✗ recusada: {m}" for m in self.recusadas)
        return s


def ja_perguntado(
    con: sqlite3.Connection, courseid: int, topico: str
) -> list[sqlite3.Row]:
    """Questões já geradas sobre este tópico, da mais nova para a mais velha.

    `attemptid IS NULL` é o que separa questão gerada do material de questão
    capturada de um questionário real do Moodle — as duas moram na mesma
    tabela porque são a mesma coisa para estudar.
    """
    return con.execute(
        """SELECT id, enunciado, correta, fonte_trecho, criada_em
             FROM questoes
            WHERE courseid = ? AND topico = ? AND attemptid IS NULL
         ORDER BY id DESC""",
        (courseid, topico),
    ).fetchall()


def material(
    con: sqlite3.Connection,
    courseid: int,
    topico: str,
    limite: int = TRECHOS_PADRAO,
    novidade: bool = False,
) -> tuple[list[dict], str]:
    """(trechos com fonte, aviso de cobertura).

    `ponte=True` porque montar simulado é tarefa de COBERTURA, não de resposta
    pontual: vale atravessar para o documento vizinho no grafo, que é onde
    costuma estar o exercício resolvido.
    """
    ja = memoria.shas_cobertos(con, courseid, ESCOPO, topico) if novidade else set()
    pedido = limite * 3 if novidade else limite
    achados = busca.buscar(
        con, topico, courseid=courseid, limite=pedido,
        orcamento_chars=ORCAMENTO, ponte=True,
    )
    aviso = busca.aviso_cobertura(con, topico, achados, courseid)
    if novidade:
        achados = [a for a in achados if a["sha256"] not in ja][:limite]
    if achados:
        memoria.registrar_cobertura(
            con, courseid, ESCOPO, topico, [a["sha256"] for a in achados]
        )
    return achados, aviso


def guardar(
    con: sqlite3.Connection,
    courseid: int,
    topico: str,
    questoes: list[dict],
    site: str | None = None,
) -> Guardadas:
    """Grava as questões. Recusa, uma a uma, o que não tem fonte.

    Recusar em vez de gravar com fonte vazia é o ponto: uma questão sem fonte
    no banco é indistinguível de uma com fonte na hora de estudar, e é aí que
    a alucinação passa.
    """
    salvas, recusadas, ids = 0, [], []
    for q in questoes:
        enunciado = (q.get("enunciado") or "").strip()
        fonte = (q.get("fonte_trecho") or "").strip()
        if not enunciado:
            recusadas.append("questão sem enunciado")
            continue
        if not fonte:
            recusadas.append(f"sem fonte_trecho: “{enunciado[:60]}…”")
            continue
        alternativas = q.get("alternativas")
        cur = con.execute(
            """INSERT INTO questoes
               (site, courseid, arquivo_id, topico, enunciado, alternativas,
                correta, explicacao, fonte_trecho, criada_em, revisada)
               VALUES (?,?,?,?,?,?,?,?,?,?,0)""",
            (
                site, courseid, q.get("arquivo_id"), topico, enunciado,
                json.dumps(alternativas, ensure_ascii=False) if alternativas else None,
                q.get("correta"), q.get("explicacao"), fonte, agora_iso(),
            ),
        )
        ids.append(int(cur.lastrowid))
        salvas += 1
    con.commit()
    return Guardadas(salvas, recusadas, ids)


def listar(
    con: sqlite3.Connection, courseid: int, topico: str = "", com_gabarito: bool = False
) -> str:
    """O simulado para resolver. Gabarito só quando pedido, e no fim."""
    sql = """SELECT id, topico, enunciado, alternativas, correta, explicacao,
                    fonte_trecho
               FROM questoes
              WHERE courseid = ? AND attemptid IS NULL"""
    params: list = [courseid]
    if topico:
        sql += " AND topico = ?"
        params.append(topico)
    linhas = con.execute(sql + " ORDER BY topico, id", params).fetchall()
    if not linhas:
        return (
            f"Nenhuma questão gerada ainda{f' sobre “{topico}”' if topico else ''}. "
            "Monte com `preparar_simulado` e grave com `guardar_simulado`."
        )
    L, gabarito = [], []
    for i, q in enumerate(linhas, start=1):
        L.append(f"\n**{i}.** [{q['id']}] {q['enunciado']}")
        if q["alternativas"]:
            try:
                for alt in json.loads(q["alternativas"]):
                    L.append(f"   {alt.get('id')}) {alt.get('texto')}")
            except (ValueError, AttributeError, TypeError):
                L.append(f"   {q['alternativas']}")
        g = f"**{i}.** {q['correta'] or '—'}"
        if q["explicacao"]:
            g += f" — {q['explicacao']}"
        # A fonte anda com a resposta: é com ela que se confere o que o
        # simulado afirma, sem reler a aula inteira.
        g += f"  ({q['fonte_trecho']})"
        gabarito.append(g)
    if com_gabarito:
        L.append("\n\n---\n\n### Gabarito\n")
        L.extend(gabarito)
    else:
        L.append(
            f"\n\n{len(linhas)} questão(ões). O gabarito sai em "
            "`simulado(..., com_gabarito=True)` — resolva antes."
        )
    return "\n".join(L)


def responder(
    con: sqlite3.Connection, questao_id: int, resposta: str
) -> str:
    """Registra a resposta e devolve a correção com a fonte."""
    q = con.execute(
        """SELECT id, enunciado, correta, explicacao, fonte_trecho
             FROM questoes WHERE id = ? AND attemptid IS NULL""",
        (questao_id,),
    ).fetchone()
    if q is None:
        return f"Questão {questao_id} não existe entre as geradas do material."
    dada = (resposta or "").strip()
    gabarito = (q["correta"] or "").strip()
    # Sem gabarito não há acerto nem erro: gravar 0 diria que você errou.
    acertou = None if not gabarito else int(dada.lower() == gabarito.lower())
    con.execute(
        """INSERT INTO respostas (questao_id, resposta_dada, acertou, respondida_em)
           VALUES (?,?,?,?)""",
        (questao_id, dada, acertou, agora_iso()),
    )
    con.commit()
    if acertou is None:
        return f"Registrado. Esta questão não tem gabarito guardado.\nFonte: {q['fonte_trecho']}"
    cabeca = "✓ certo" if acertou else f"✗ errado — era {gabarito}"
    partes = [cabeca]
    if q["explicacao"]:
        partes.append(q["explicacao"])
    partes.append(f"Fonte: {q['fonte_trecho']}")
    return "\n".join(partes)


def desempenho(con: sqlite3.Connection, courseid: int) -> list[sqlite3.Row]:
    """Acertos por tópico, para o roteiro de estudo saber onde insistir."""
    return con.execute(
        """SELECT q.topico,
                  COUNT(r.id) respondidas,
                  SUM(CASE WHEN r.acertou = 1 THEN 1 ELSE 0 END) acertos
             FROM questoes q JOIN respostas r ON r.questao_id = q.id
            WHERE q.courseid = ? AND q.attemptid IS NULL
         GROUP BY q.topico
         ORDER BY (SUM(CASE WHEN r.acertou = 1 THEN 1 ELSE 0 END) * 1.0 /
                   MAX(COUNT(r.id), 1))""",
        (courseid,),
    ).fetchall()

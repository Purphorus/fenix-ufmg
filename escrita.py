"""Escrita no Moodle: fórum, tarefa e questionário.

Tudo que sai no seu nome sai por este módulo — é a fronteira, e está isolada
num arquivo só de propósito, para que auditar "o que este programa é capaz de
publicar" seja ler um arquivo.

Três garantias, iguais para toda função daqui:

1. Nenhuma escrita acontece sem `confirmar=True`. Sem ele a função devolve o
   payload que *seria* enviado e não chama o Moodle. O default é o ensaio
   porque estas ações não têm desfazer.
2. Toda tentativa vira linha em `log_escrita`, inclusive as que falham.
3. As funções enviam o conteúdo que você passa. Nenhuma delas gera texto.

Sobre questionário: as funções abaixo cobrem o ciclo do app oficial (iniciar,
ler, salvar, enviar). O que não existe aqui é ponte com `companion.py` — nada
neste módulo chama o gerador de questões, e `enviar_questionario` recebe
respostas prontas, nunca um prompt.
"""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from db import registrar_escrita
from moodle_client import MoodleClient


@dataclass
class Resultado:
    """Devolução única de toda operação de escrita.

    `ok=False` com `ensaio=True` não é erro: é o modo de ensaio devolvendo o
    que faria. Quem chama distingue pelos dois campos, nunca só por `ok`.
    """

    acao: str
    alvo: str
    ok: bool = False
    ensaio: bool = False
    detalhe: str = ""
    payload: dict = field(default_factory=dict)
    dados: Any = None

    def __str__(self) -> str:
        if self.ensaio:
            return f"[ensaio] {self.acao} em {self.alvo}: {self.detalhe}"
        marca = "✓" if self.ok else "✗"
        return f"{marca} {self.acao} em {self.alvo}: {self.detalhe}"


def _executar(
    con: sqlite3.Connection,
    cli: MoodleClient,
    acao: str,
    alvo: str,
    resumo: str,
    funcao: str,
    params: dict,
    confirmar: bool,
) -> Resultado:
    """Caminho único de toda escrita: ensaia, envia, registra.

    Centralizado para que nenhuma função nova possa esquecer o log ou o gate de
    confirmação — quem adicionar uma escrita futura passa por aqui.
    """
    payload = {"wsfunction": funcao, **params}

    # Trava estrutural para agente automático. O ouvinte do WhatsApp roda um
    # Claude headless que alcança estas ferramentas por `executar`, e uma
    # instrução plantada no material lido poderia mandar publicar com
    # confirmar=True. Aqui a publicação é impossível, não improvável: o
    # desenho é o agente PROPOR e o dono confirmar por fora.
    if os.environ.get("FENIX_SOMENTE_LEITURA"):
        return Resultado(
            acao=acao,
            alvo=alvo,
            detalhe=(f"{resumo} — recusado: esta sessão é somente leitura "
                     "(agente automático). Publique pelo terminal."),
            payload=payload,
        )

    if not confirmar:
        return Resultado(
            acao=acao,
            alvo=alvo,
            ensaio=True,
            detalhe=f"{resumo} — nada foi enviado",
            payload=payload,
        )

    try:
        resposta = cli.call(funcao, **params)
    except Exception as e:  # MoodleError, rede, timeout — tudo vira log + Resultado
        registrar_escrita(
            con, cli.alias, acao, alvo=alvo, resumo=resumo,
            payload=payload, erro=str(e),
        )
        return Resultado(acao=acao, alvo=alvo, ok=False, detalhe=f"falhou: {e}", payload=payload)

    registrar_escrita(
        con, cli.alias, acao, alvo=alvo, resumo=resumo,
        payload=payload, resposta=resposta,
    )
    return Resultado(
        acao=acao, alvo=alvo, ok=True, detalhe=resumo, payload=payload, dados=resposta
    )


# --------------------------------------------------------------------------
# Fórum
# --------------------------------------------------------------------------


def nova_discussao(
    con: sqlite3.Connection,
    cli: MoodleClient,
    forumid: int,
    assunto: str,
    mensagem: str,
    *,
    confirmar: bool = False,
) -> Resultado:
    """Abre uma discussão nova num fórum. `mensagem` aceita HTML simples."""
    return _executar(
        con, cli,
        acao="forum_discussao",
        alvo=f"forumid={forumid}",
        resumo=f"discussão “{assunto}” ({len(mensagem)} caracteres)",
        funcao="mod_forum_add_discussion",
        params={"forumid": forumid, "subject": assunto, "message": mensagem},
        confirmar=confirmar,
    )


def responder_post(
    con: sqlite3.Connection,
    cli: MoodleClient,
    postid: int,
    assunto: str,
    mensagem: str,
    *,
    confirmar: bool = False,
) -> Resultado:
    """Responde a um post existente. `postid` vem de `discussoes`/`posts_discussao`."""
    return _executar(
        con, cli,
        acao="forum_resposta",
        alvo=f"postid={postid}",
        resumo=f"resposta “{assunto}” ({len(mensagem)} caracteres)",
        funcao="mod_forum_add_discussion_post",
        params={"postid": postid, "subject": assunto, "message": mensagem},
        confirmar=confirmar,
    )


def posts_discussao(cli: MoodleClient, discussionid: int) -> list[dict]:
    """Lê os posts de uma discussão (leitura; serve para achar o postid)."""
    r = cli.call("mod_forum_get_discussion_posts", discussionid=discussionid)
    return r.get("posts", [])


# --------------------------------------------------------------------------
# Tarefa (assignment)
# --------------------------------------------------------------------------


def anexar_arquivo(
    con: sqlite3.Connection,
    cli: MoodleClient,
    assignid: int,
    arquivo: Path | str,
    *,
    itemid: int = 0,
    confirmar: bool = False,
) -> Resultado:
    """Sobe um arquivo para a área de rascunho e devolve o `itemid`.

    Duas etapas separadas de propósito: subir arquivo não entrega tarefa. O
    itemid devolvido é o que `salvar_tarefa` consome. Subir vários arquivos
    para o mesmo itemid os agrupa na mesma entrega.
    """
    arquivo = Path(arquivo)
    alvo = f"assignid={assignid}"
    resumo = f"anexar {arquivo.name}"

    if not confirmar:
        return Resultado(
            acao="tarefa_anexo", alvo=alvo, ensaio=True,
            detalhe=f"{resumo} — nada foi enviado",
            payload={"arquivo": str(arquivo), "itemid": itemid},
        )

    try:
        desc = cli.upload(arquivo, itemid=itemid)
    except Exception as e:
        registrar_escrita(
            con, cli.alias, "tarefa_anexo", alvo=alvo, resumo=resumo,
            payload={"arquivo": str(arquivo)}, erro=str(e),
        )
        return Resultado(acao="tarefa_anexo", alvo=alvo, detalhe=f"falhou: {e}")

    registrar_escrita(
        con, cli.alias, "tarefa_anexo", alvo=alvo, resumo=resumo,
        payload={"arquivo": str(arquivo)}, resposta=desc,
    )
    return Resultado(
        acao="tarefa_anexo", alvo=alvo, ok=True,
        detalhe=f"{arquivo.name} no rascunho (itemid={desc.get('itemid')})",
        dados=desc,
    )


def salvar_tarefa(
    con: sqlite3.Connection,
    cli: MoodleClient,
    assignid: int,
    *,
    itemid: int | None = None,
    texto: str | None = None,
    confirmar: bool = False,
) -> Resultado:
    """Salva o rascunho da entrega (arquivos e/ou texto online).

    Ainda NÃO entrega: o professor não vê como submetido até
    `enviar_tarefa`. Salvar é reversível, enviar geralmente não.
    """
    plugindata: dict[str, Any] = {}
    partes = []
    if itemid is not None:
        plugindata["files_filemanager"] = itemid
        partes.append(f"anexos itemid={itemid}")
    if texto is not None:
        plugindata["onlinetext_editor"] = {
            "text": texto, "format": 1, "itemid": itemid or 0,
        }
        partes.append(f"texto ({len(texto)} caracteres)")
    if not plugindata:
        return Resultado(
            acao="tarefa_rascunho", alvo=f"assignid={assignid}",
            detalhe="nada para salvar: informe itemid e/ou texto",
        )

    return _executar(
        con, cli,
        acao="tarefa_rascunho",
        alvo=f"assignid={assignid}",
        resumo="salvar rascunho: " + ", ".join(partes),
        funcao="mod_assign_save_submission",
        params={"assignmentid": assignid, "plugindata": plugindata},
        confirmar=confirmar,
    )


def enviar_tarefa(
    con: sqlite3.Connection,
    cli: MoodleClient,
    assignid: int,
    *,
    aceitar_declaracao: bool = False,
    confirmar: bool = False,
) -> Resultado:
    """Entrega a tarefa para avaliação. Normalmente irreversível.

    `aceitar_declaracao` corresponde ao aceite da declaração de autoria que
    alguns professores exigem. Fica explícito como parâmetro porque é uma
    afirmação sua sobre a autoria do trabalho, não um detalhe de protocolo.
    """
    return _executar(
        con, cli,
        acao="tarefa_enviar",
        alvo=f"assignid={assignid}",
        resumo="entregar para avaliação"
        + (" (declaração de autoria aceita)" if aceitar_declaracao else ""),
        funcao="mod_assign_submit_for_grading",
        params={
            "assignmentid": assignid,
            "acceptsubmissionstatement": 1 if aceitar_declaracao else 0,
        },
        confirmar=confirmar,
    )


# --------------------------------------------------------------------------
# Questionário (quiz)
# --------------------------------------------------------------------------

# Nomes de campo do Moodle: q<slot>:<seq>_answer, ..._sub0_answer, _sequencecheck
_CAMPO = re.compile(r'name="(q\d+:\d+_[^"]*)"')


def iniciar_questionario(
    con: sqlite3.Connection,
    cli: MoodleClient,
    quizid: int,
    *,
    confirmar: bool = False,
) -> Resultado:
    """Inicia uma tentativa. Consome uma das tentativas permitidas."""
    return _executar(
        con, cli,
        acao="quiz_iniciar",
        alvo=f"quizid={quizid}",
        resumo="iniciar tentativa (consome uma tentativa permitida)",
        funcao="mod_quiz_start_attempt",
        params={"quizid": quizid},
        confirmar=confirmar,
    )


def tentativas(cli: MoodleClient, quizid: int) -> list[dict]:
    """Lista suas tentativas num questionário (leitura)."""
    r = cli.call("mod_quiz_get_user_attempts", quizid=quizid, status="all")
    return r.get("attempts", [])


def dados_tentativa(cli: MoodleClient, attemptid: int, pagina: int = 0) -> dict:
    """Lê uma página da tentativa: questões, HTML e nomes de campo (leitura).

    Acrescenta `campos` a cada questão. O Moodle não expõe os nomes de input
    de forma estruturada — eles só existem no HTML renderizado, então saem daqui
    por regex. É frágil por natureza: se o tema do site mudar a renderização,
    é aqui que quebra.
    """
    r = cli.call("mod_quiz_get_attempt_data", attemptid=attemptid, page=pagina)
    for q in r.get("questions", []):
        q["campos"] = sorted(set(_CAMPO.findall(q.get("html", "") or "")))
    return r


def _pares(respostas: dict[str, Any]) -> list[dict[str, str]]:
    """{'q1:1_answer': '2'} -> [{'name': 'q1:1_answer', 'value': '2'}]"""
    return [{"name": k, "value": str(v)} for k, v in respostas.items()]


def salvar_questionario(
    con: sqlite3.Connection,
    cli: MoodleClient,
    attemptid: int,
    respostas: dict[str, Any],
    *,
    confirmar: bool = False,
) -> Resultado:
    """Salva respostas sem finalizar (equivale ao autossave do navegador).

    `respostas` mapeia nome de campo (veja `dados_tentativa`) para valor.
    """
    return _executar(
        con, cli,
        acao="quiz_salvar",
        alvo=f"attemptid={attemptid}",
        resumo=f"salvar {len(respostas)} resposta(s), sem finalizar",
        funcao="mod_quiz_save_attempt",
        params={"attemptid": attemptid, "data": _pares(respostas)},
        confirmar=confirmar,
    )


def enviar_questionario(
    con: sqlite3.Connection,
    cli: MoodleClient,
    attemptid: int,
    respostas: dict[str, Any] | None = None,
    *,
    finalizar: bool = True,
    confirmar: bool = False,
) -> Resultado:
    """Processa a tentativa; com `finalizar=True`, encerra e manda corrigir.

    Recebe respostas prontas — as suas. Nenhum gerador é chamado aqui.
    """
    return _executar(
        con, cli,
        acao="quiz_enviar",
        alvo=f"attemptid={attemptid}",
        resumo=(
            f"{'finalizar' if finalizar else 'processar'} tentativa"
            f" com {len(respostas or {})} resposta(s)"
        ),
        funcao="mod_quiz_process_attempt",
        params={
            "attemptid": attemptid,
            "data": _pares(respostas or {}),
            "finishattempt": 1 if finalizar else 0,
        },
        confirmar=confirmar,
    )


def revisar_tentativa(cli: MoodleClient, attemptid: int) -> dict:
    """Resumo da tentativa: o que está respondido e o que ficou em branco."""
    return cli.call("mod_quiz_get_attempt_summary", attemptid=attemptid)


# --------------------------------------------------------------------------


def historico(con: sqlite3.Connection, limite: int = 30) -> list[sqlite3.Row]:
    """Últimas escritas feitas no Moodle por este programa."""
    return con.execute(
        "SELECT * FROM log_escrita ORDER BY id DESC LIMIT ?", (limite,)
    ).fetchall()

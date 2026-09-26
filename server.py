"""Servidor MCP para o Moodle da UFMG (UFMG Virtual).

Rodar:  python server.py
Requer: pelo menos um site configurado via `python get_token.py`.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable

try:  # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server

import apostila
import busca
import agenda_mac
import calendario
import companion
import db
import memoria
import programa
import simulado
import escrita
import extract
from moodle_client import MoodleError, get_client, load_sites

mcp = _Server("ufmg-moodle")

DOWNLOAD_DIR = Path.home() / "Downloads" / "moodle"


_schema_pronto = False


def _con():
    """Conexão para uma chamada de tool.

    O schema é aplicado uma vez por processo, não por chamada: `init_db` roda o
    script inteiro mais os PRAGMAs, e fazer isso a cada pergunta era puro
    desperdício. A conexão em si continua por chamada — SQLite não é thread-safe
    e o servidor pode atender em threads.
    """
    global _schema_pronto
    con = db.conectar()
    if not _schema_pronto:
        db.init_db(con)
        _schema_pronto = True
    return con


# --------------------------------------------------------------------------
# Ferramentas frias: registradas, não expostas como esquema.
#
# O esquema de cada ferramenta entra no contexto A CADA MENSAGEM. Com 45
# ferramentas isso custava ~6,4k tokens sempre, o que é mais que qualquer
# chamada isolada que elas façam. As sete mais usadas continuam tipadas; o
# resto sai por `indice()` (sob demanda) e `executar()` (uma chamada).
#
# O que se perde: validação de argumento pelo cliente. Em troca, o erro de
# `executar` diz a assinatura certa, então o conserto custa um turno.
# --------------------------------------------------------------------------

_FRIAS: dict[str, "Callable"] = {}

# Irreversível ou com efeito externo: aparece marcado no índice, para a
# escolha não depender de o modelo lembrar.
_PERIGOSAS = {
    "postar_forum", "responder_forum", "entregar_tarefa", "salvar_tarefa",
    "anexar_tarefa", "iniciar_questionario", "salvar_questionario",
    "responder_questionario", "enviar_revisao",
    "sincronizar_agenda", "marcar_estudos", "desmarcar_agenda",
}


# As tipadas continuam expostas com esquema; `executar` precisa saber disso
# para dizer "chame direto" em vez de "não existe", que manda o modelo
# procurar no índice algo que está na cara dele.
# O conjunto quente é MEDIDO, não escolhido por gosto. Simulando três
# conversas reais com o histórico sendo reenviado a cada turno:
#
#   9 tipadas (como era)      742 tokens de esquema    89.813 no total
#   2 tipadas (tudo frio)     209                     102.398   PIOR
#   4 tipadas (este)          332                      79.973   melhor
#
# "Tudo frio" perde porque cada ferramenta que sai do prefixo custa uma ida ao
# índice, e uma ida a mais reenvia todo o histórico acumulado. Economizar 533
# tokens por mensagem não paga um turno que custa milhares.
#
# `buscar_material` e `memoria_contexto` ficam porque entram em quase toda
# conversa e são baratas (76 e 47 tokens). As outras saem.
_QUENTES = {"indice", "executar", "buscar_material", "memoria_contexto"}

# Falam com o Calendário do Mac (AppleScript e o banco dele). Fora do macOS
# continuam em `_FRIAS` — o cardápio da skill é conferido contra ele em
# qualquer máquina —, mas o índice não as oferece e `executar` recusa dizendo
# o que usar no lugar. Esconder só do índice não bastava: a skill cita o nome,
# e o modelo chamaria direto.
SO_MAC = {"horarios_livres", "sincronizar_agenda", "marcar_estudos",
          "agenda_marcados", "desmarcar_agenda"}


def _no_mac() -> bool:
    return sys.platform == "darwin"


def _disponiveis() -> list[str]:
    return sorted(n for n in _FRIAS if _no_mac() or n not in SO_MAC)


def fria():
    """Registra sem expor o esquema."""
    def dec(f):
        _FRIAS[f.__name__] = f
        return f
    return dec


def _chave(t: str) -> str:
    """Minúsculo e sem acento: filtrar por 'forum' tem que achar 'fóruns'."""
    import unicodedata
    t = unicodedata.normalize("NFKD", t or "").lower()
    return "".join(c for c in t if not unicodedata.combining(c))


def _resolver_curso(con, termo: str) -> tuple[int, str]:
    """'6095', apelido memorizado ('macro') ou parte do nome da turma -> courseid.

    Devolve (0, motivo) quando não resolve ou quando mais de uma turma casa:
    turma errada numa consulta de nota é pior que pergunta de volta.
    """
    t = (termo or "").strip()
    if not t:
        return 0, "Diga a turma: courseid, apelido ou parte do nome."
    if t.isdigit():
        return int(t), ""
    site = db.site_atual(con)
    r = memoria.resolver(con, site, t, "curso")
    if r and r["alvo_id"]:
        return int(r["alvo_id"]), ""
    chave = _chave(t)
    achados = [(x["courseid"], x["fullname"]) for x in con.execute(
        "SELECT courseid, fullname, shortname FROM cursos WHERE site = ?", (site,))
        if chave in _chave(x["fullname"]) or chave in _chave(x["shortname"])]
    if len(achados) == 1:
        return achados[0][0], ""
    if achados:
        return 0, "Mais de uma turma casa: " + "; ".join(f"{n} ({i})" for i, n in achados)
    return 0, f"Não achei a turma {t!r}. `listar_turmas` mostra as suas."


def _assinatura(f) -> str:
    """nome(obrigatorio, opcional?) — primeira linha da docstring."""
    import inspect
    sig = inspect.signature(f)
    partes = []
    for nome, prm in sig.parameters.items():
        partes.append(nome if prm.default is inspect.Parameter.empty else f"{nome}?")
    doc = (f.__doc__ or "").strip().split("\n")[0]
    marca = " [ESCREVE]" if f.__name__ in _PERIGOSAS else ""
    return f"{f.__name__}({', '.join(partes)}){marca} — {doc}"


# Quantas ferramentas o índice devolve. Cinco, não quarenta.
#
# A literatura de recuperação de ferramenta mede isto: com K grande, a
# recuperação acerta quase sempre MAS a acurácia do modelo cai de 10 a 16
# pontos e o custo sobe dez vezes ("The 99% Success Paradox", arXiv 2605.18857).
# Conjunto pequeno e preciso ganha de conjunto grande otimizado para cobertura.
INDICE_TOPO = 5

_vetores_ferramenta: dict[str, bytes] = {}


def _texto_da_ferramenta(nome: str, f) -> str:
    """O que se embute: o nome mais a primeira linha da docstring."""
    doc = (f.__doc__ or "").strip().split("\n")[0]
    return f"{nome.replace('_', ' ')}: {doc}"


def _ranquear(filtro: str) -> list[str] | None:
    """Nomes das ferramentas mais próximas do pedido, por cosseno.

    None quando não há modelo — aí o índice cai para o casamento de substring,
    que é o que sempre fez. Os vetores são calculados uma vez por processo:
    são 41 descrições estáticas, uns 0,3 s, e o servidor MCP é longevo.
    """
    import vetor

    if not vetor.disponivel():
        return None
    if not _vetores_ferramenta:
        nomes = sorted(_FRIAS)
        vs = vetor.embutir([_texto_da_ferramenta(n, _FRIAS[n]) for n in nomes])
        if not vs:
            return None
        _vetores_ferramenta.update(zip(nomes, vs))
    q = vetor.embutir_um(filtro)
    if not q:
        return None
    nomes = list(_vetores_ferramenta)
    scores = vetor.similaridades(q, [_vetores_ferramenta[n] for n in nomes])
    if not scores:
        return None
    return [n for _, n in sorted(zip(scores, nomes), reverse=True)]


@mcp.tool()
def indice(filtro: str = "", tudo: bool = False) -> str:
    """Acha a ferramenta certa para um pedido. Descreva o pedido em `filtro`.

    Ex.: indice("ver o que errei na prova"). Devolve as 5 mais próximas, com
    assinatura, para chamar com `executar`. `tudo=True` lista todas.
    """
    alvo = filtro.strip()
    if not alvo and not tudo:
        return (
            "Diga o que você quer fazer: indice(\"postar no fórum\"), "
            "indice(\"o que errei na prova\"), indice(\"datas de prova\").\n"
            "indice(tudo=True) lista as 41, mas é caro e escolher fica pior."
        )

    disponiveis = _disponiveis()
    if tudo:
        escolhidos = disponiveis
    else:
        ordem = _ranquear(alvo)
        if ordem is None:  # sem modelo: casamento de substring, como antes
            chave = _chave(alvo)
            escolhidos = [
                n for n in disponiveis
                if chave in _chave(n) or chave in _chave(_FRIAS[n].__doc__ or "")
            ][:INDICE_TOPO]
        else:
            escolhidos = [n for n in ordem if n in disponiveis][:INDICE_TOPO]

    linhas = [_assinatura(_FRIAS[n]) for n in escolhidos]
    if not linhas:
        return f"Nada casa com '{filtro}'. Tente descrever de outro jeito."
    return (
        f"{len(linhas)} ferramenta(s) — use executar(nome, argumentos_json).\n"
        "'?' = opcional. [ESCREVE] = efeito externo; ensaia por padrão.\n\n"
        + "\n".join(linhas)
    )


@mcp.tool()
def executar(ferramenta: str, argumentos_json: str = "{}") -> str:
    """Chama uma ferramenta listada em `indice()`.

    Ex.: executar("notas", '{"courseid": 6095}')
    """
    if ferramenta in SO_MAC and not _no_mac():
        return (
            f"'{ferramenta}' só funciona no macOS: usa o Calendário do Mac. "
            "Aqui, exportar_calendario gera um .ics com as provas e prazos "
            "confirmados, que o Google Agenda e o Outlook importam."
        )
    f = _FRIAS.get(ferramenta)
    if f is None:
        if ferramenta in _QUENTES:
            return (
                f"'{ferramenta}' é ferramenta tipada — chame direto, "
                "não por executar."
            )
        alvo = _chave(ferramenta)
        perto = [n for n in _FRIAS if alvo in _chave(n) or _chave(n) in alvo]
        return (
            f"'{ferramenta}' não existe."
            + (f" Você quis dizer: {', '.join(sorted(perto)[:5])}?" if perto else "")
            + " Chame indice() para ver o que há."
        )
    try:
        args = json.loads(argumentos_json or "{}")
    except json.JSONDecodeError as e:
        return f"argumentos_json inválido: {e}"
    if not isinstance(args, dict):
        return "argumentos_json precisa ser um objeto JSON."
    try:
        return str(f(**args))
    except TypeError as e:
        # Assinatura errada é o erro mais provável aqui: devolva a certa, para
        # o conserto custar um turno em vez de uma rodada de tentativa e erro.
        return f"argumentos errados: {e}\nAssinatura: {_assinatura(f)}"
    except Exception as e:
        return f"{type(e).__name__}: {e}"


def _ts(epoch: Any) -> str:
    if not epoch:
        return ""
    return time.strftime("%d/%m/%Y %H:%M", time.localtime(int(epoch)))


# --------------------------------------------------------------------------


@fria()
def listar_sites() -> str:
    """Lista os sites Moodle configurados (um por semestre) e seus apelidos."""
    sites = load_sites()
    if not sites:
        return "Nenhum site configurado. Rode `python get_token.py`."
    return "\n".join(f"{alias}: {cfg['url']}" for alias, cfg in sorted(sites.items()))


@fria()
def info_site(site: str = "") -> str:
    """Mostra dados do usuário logado e do site (útil para testar o token)."""
    c = get_client(site or None)
    i = c.site_info()
    return (
        f"site={i.get('sitename')}\nusuario={i.get('fullname')} ({i.get('username')})\n"
        f"userid={i.get('userid')}\nversao={i.get('release')}\n"
        f"funcoes_disponiveis={len(i.get('functions', []))}"
    )


@fria()
def listar_turmas(site: str = "") -> str:
    """Lista as turmas (cursos) em que você está inscrito, com id e progresso."""
    c = get_client(site or None)
    cursos = c.call("core_enrol_get_users_courses", userid=c.user_id())
    if not cursos:
        return "Nenhuma turma encontrada."
    linhas = []
    for curso in cursos:
        # `progress` vem como 14.285714285714285; ninguém lê 15 casas decimais
        prog = curso.get("progress")
        prog_txt = f" — {prog:.0f}%" if isinstance(prog, (int, float)) else ""
        linhas.append(
            f"[{curso['id']}] {_curto(curso.get('shortname'))} "
            f"{curso.get('fullname')}{prog_txt}"
        )
    return "\n".join(linhas)


@fria()
def conteudo_turma(courseid: int, site: str = "") -> str:
    """Estrutura da turma: seções, atividades, arquivos. Longa — para achar
    material específico, buscar_material é mais barato."""
    c = get_client(site or None)
    secoes = c.call("core_course_get_contents", courseid=courseid)
    out = []
    for secao in secoes:
        out.append(f"\n## {secao.get('name')}")
        for mod in secao.get("modules", []):
            out.append(f"- [{mod.get('modname')}] {mod.get('name')} (cmid={mod['id']})")
            for arq in mod.get("contents", []) or []:
                if arq.get("fileurl"):
                    out.append(f"    arquivo: {arq.get('filename')} -> {arq['fileurl']}")
    return "\n".join(out) or "Turma sem conteúdo visível."


@fria()
def listar_tarefas(courseids: list[int] = [], site: str = "") -> str:
    """Lista as tarefas (assignments) das turmas, com prazo de entrega."""
    c = get_client(site or None)
    if not courseids:
        cursos = c.call("core_enrol_get_users_courses", userid=c.user_id())
        courseids = [curso["id"] for curso in cursos]
    dados = c.call("mod_assign_get_assignments", courseids=courseids)
    out = []
    for curso in dados.get("courses", []):
        out.append(f"\n## {curso.get('fullname')}")
        for a in curso.get("assignments", []):
            out.append(
                f"- [{a['id']}] {a.get('name')} — entrega: {_ts(a.get('duedate'))}"
                + (f" | corte: {_ts(a.get('cutoffdate'))}" if a.get("cutoffdate") else "")
            )
    return "\n".join(out) or "Nenhuma tarefa encontrada."


@fria()
def questionarios(courseids: list[int] = [], site: str = "") -> str:
    """Lista os questionários das turmas: quizid, abertura, fechamento e tentativas."""
    # Faltava: o índice foi consultado seis vezes por "listar questionários da
    # turma" e a saída foi `chamar_ws` com JSON cru. O quizid daqui é o que
    # `tentativas_questionario` pede.
    c = get_client(site or None)
    cursos = c.call("core_enrol_get_users_courses", userid=c.user_id())
    nomes = {x["id"]: x.get("fullname") for x in cursos}
    ids = list(courseids) or list(nomes)
    dados = c.call("mod_quiz_get_quizzes_by_courses", courseids=ids)
    por_curso: dict[int, list[str]] = {}
    for q in dados.get("quizzes", []):
        tent = q.get("attempts") or 0
        por_curso.setdefault(q.get("course"), []).append(
            f"- [quizid {q['id']} | cmid {q.get('coursemodule')}] {q.get('name')} — "
            f"abre: {_ts(q.get('timeopen'))} | fecha: {_ts(q.get('timeclose'))} | "
            f"tentativas: {tent or 'ilimitadas'}")
    out = []
    for cid, linhas in por_curso.items():
        out.append(f"\n## {nomes.get(cid, cid)}")
        out.extend(linhas)
    return "\n".join(out) or "Nenhum questionário encontrado."


# Tamanho do enunciado devolvido: o bastante para ler a tarefa, não a ponto de
# virar um `texto_material`.
ENUNCIADO_CHARS = 1500


@fria()
def atividade(cmid: int = 0, busca: str = "", site: str = "") -> str:
    """Enunciado e datas de uma tarefa ou questionário, pelo cmid ou parte do nome."""
    # Faltava: "ver o enunciado da atividade" terminava em
    # `core_course_get_course_module`, que não traz o enunciado, ou em
    # `conteudo_turma`, que traz a turma inteira. A busca por nome é no banco
    # local (tabela `modulos`, do sync): sem rede e sem listar tudo.
    import sync
    if not cmid:
        if not busca.strip():
            return "Passe cmid ou busca (parte do nome da atividade)."
        con = _con()
        achados = con.execute(
            """SELECT cmid, modname, nome, courseid FROM modulos
               WHERE nome LIKE ? ORDER BY courseid, nome LIMIT 8""",
            (f"%{busca.strip()}%",)).fetchall()
        if not achados:
            return f"Nenhuma atividade com {busca!r} no nome. `conteudo_turma` lista a turma."
        if len(achados) > 1:
            return "Mais de uma; escolha o cmid:\n" + "\n".join(
                f"- cmid {r['cmid']} [{r['modname']}] {r['nome']} (curso {r['courseid']})"
                for r in achados)
        cmid = achados[0]["cmid"]
    c = get_client(site or None)
    cm = c.call("core_course_get_course_module", cmid=cmid).get("cm", {})
    tipo, inst, curso = cm.get("modname"), cm.get("instance"), cm.get("course")
    linhas = [f"{cm.get('name')} [{tipo}] — cmid {cmid}, curso {curso}"]
    intro = ""
    if tipo == "assign":
        for cur in c.call("mod_assign_get_assignments", courseids=[curso]).get("courses", []):
            for x in cur.get("assignments", []):
                if x["id"] == inst:
                    intro = x.get("intro", "")
                    linhas.append(f"assignid {inst} | abre: {_ts(x.get('allowsubmissionsfromdate'))} | "
                                  f"entrega: {_ts(x.get('duedate'))}"
                                  + (f" | corte: {_ts(x.get('cutoffdate'))}" if x.get("cutoffdate") else ""))
    elif tipo == "quiz":
        for x in c.call("mod_quiz_get_quizzes_by_courses", courseids=[curso]).get("quizzes", []):
            if x["id"] == inst:
                intro = x.get("intro", "")
                linhas.append(f"quizid {inst} | abre: {_ts(x.get('timeopen'))} | "
                              f"fecha: {_ts(x.get('timeclose'))} | tentativas: {x.get('attempts') or 'ilimitadas'}")
    else:
        linhas.append("Enunciado só é lido de tarefa e questionário; para arquivo, use buscar_material.")
    if intro:
        linhas.append(sync.texto_limpo(intro, ENUNCIADO_CHARS))
    return "\n".join(linhas)


@fria()
def status_tarefa(assignid: int, site: str = "") -> str:
    """Mostra o status da sua entrega numa tarefa (entregue, nota, feedback)."""
    c = get_client(site or None)
    s = c.call("mod_assign_get_submission_status", assignid=assignid)
    last = s.get("lastattempt", {}) or {}
    sub = last.get("submission", {}) or {}
    fb = s.get("feedback", {}) or {}
    grade = fb.get("grade", {}) or {}
    return json.dumps(
        {
            "status": sub.get("status"),
            "modificado": _ts(sub.get("timemodified")),
            "pode_editar": last.get("canedit"),
            "nota": grade.get("grade"),
            "nota_formatada": fb.get("gradefordisplay"),
        },
        ensure_ascii=False,
        indent=2,
    )


@fria()
def proximos_prazos(dias: int = 14, site: str = "") -> str:
    """Lista os próximos eventos/prazos do calendário nos próximos N dias."""
    c = get_client(site or None)
    agora = int(time.time())
    ev = c.call(
        "core_calendar_get_action_events_by_timesort",
        timesortfrom=agora,
        timesortto=agora + dias * 86400,
        limitnum=50,
    )
    linhas = [
        f"{_ts(e.get('timesort'))} — {e.get('name')} "
        f"({(e.get('course') or {}).get('shortname', '')})"
        for e in ev.get("events", [])
    ]
    return "\n".join(linhas) or f"Nada nos próximos {dias} dias."


@fria()
def notas(courseid: int = 0, curso: str = "", site: str = "") -> str:
    """Suas notas lançadas numa turma, pelo courseid ou pelo nome ("macro")."""
    # `curso` existe para a sequência mais repetida do histórico,
    # memoria_contexto → notas, virar uma chamada só.
    if not courseid:
        courseid, erro = _resolver_curso(_con(), curso)
        if erro:
            return erro
    c = get_client(site or None)
    r = c.call("gradereport_user_get_grade_items", courseid=courseid, userid=c.user_id())
    out = []
    for user in r.get("usergrades", []):
        for item in user.get("gradeitems", []):
            out.append(
                f"- {item.get('itemname') or item.get('itemtype')}: "
                f"{item.get('gradeformatted')} "
                f"(peso {item.get('weightformatted')})"
            )
    return "\n".join(out) or "Sem notas lançadas."


@fria()
def foruns(courseid: int, site: str = "") -> str:
    """Lista os fóruns de uma turma e o número de discussões."""
    c = get_client(site or None)
    fs = c.call("mod_forum_get_forums_by_courses", courseids=[courseid])
    return "\n".join(
        f"[{f['id']}] {f.get('name')} — {f.get('numdiscussions')} discussões" for f in fs
    ) or "Nenhum fórum."


@fria()
def discussoes(forumid: int, limite: int = 10, site: str = "") -> str:
    """Lista as discussões mais recentes de um fórum, com o texto da mensagem inicial."""
    c = get_client(site or None)
    d = c.call(
        "mod_forum_get_forum_discussions", forumid=forumid, page=0, perpage=limite
    )
    out = []
    for disc in d.get("discussions", []):
        out.append(
            f"\n### {disc.get('name')} — {disc.get('userfullname')} "
            f"({_ts(disc.get('timemodified'))})\n{(disc.get('message') or '')[:800]}"
        )
    return "\n".join(out) or "Nenhuma discussão."


@fria()
def baixar_arquivo(fileurl: str, nome: str, site: str = "") -> str:
    """Baixa um arquivo do Moodle (use a fileurl que aparece em conteudo_turma)."""
    c = get_client(site or None)
    dest = c.download(fileurl, DOWNLOAD_DIR / nome)
    return f"Salvo em {dest}"


@fria()
def chamar_ws(funcao: str, parametros_json: str = "{}", site: str = "") -> str:
    """Escape hatch: qualquer função de web service. Use por último — JSON cru,
    caro. Ex.: funcao='core_course_get_contents', parametros_json='{"courseid":123}'
    """
    c = get_client(site or None)
    try:
        params = json.loads(parametros_json)
    except json.JSONDecodeError as e:
        return f"JSON inválido: {e}"
    try:
        return json.dumps(c.call(funcao, **params), ensure_ascii=False, indent=2)[:20000]
    except MoodleError as e:
        return f"Erro do Moodle: {e}"


# --------------------------------------------------------------------------
# Material extraído (texto local, não bate no servidor da UFMG)
# --------------------------------------------------------------------------


@fria()
def extrair_textos(site: str = "", refazer: bool = False) -> str:
    """Extrai texto dos materiais baixados e indexa a busca. Não usa rede."""
    con = _con()
    try:
        r = extract.extrair_pendentes(con, site=site, refazer=refazer)
        return (
            "\n".join(r.detalhes)
            + f"\n\n{r.extraidos} extraído(s), {r.ocr} precisa(m) de OCR, {r.erros} erro(s)"
        ) if r.detalhes else "Nada pendente de extração."
    finally:
        con.close()


def _escopo_semestre(con, curso: int, semestre: str) -> str | None:
    """Em que semestre buscar. `""` = o corrente, `"*"` = todos.

    Com `curso` informado o semestre já está implícito nele, e filtrar de novo
    só criaria a chance de os dois discordarem.

    O padrão é o semestre CORRENTE e não o corpus inteiro: enquanto existe um
    semestre só os dois são a mesma coisa, mas no dia em que houver dois, a
    resposta para "o que o material diz sobre X" traria o slide do ano passado
    sem avisar. Quem quer o semestre velho pede.
    """
    if curso or semestre == "*":
        return None
    return semestre or db.site_atual(con)


@mcp.tool()
def buscar_material(
    termo: str, curso: int = 0, limite: int = 8, semestre: str = ""
) -> str:
    """Busca nos materiais (termo + sentido). Devolve trecho, arquivo e página.

    Só no semestre corrente; semestre="*" busca em todos.
    """
    con = _con()
    try:
        if not busca.indexado(con):
            return (
                "Índice de busca vazio. Rode `cli.py extrair --reindexar` "
                "para construí-lo a partir dos textos já extraídos."
            )
        # rerank ligado aqui e em nenhum outro lugar: é a única chamada em que
        # o corte é no topo (o usuário lê os primeiros). Em `cobrir_topico`,
        # que leva uma fatia larga, medido: resultado idêntico a 20x o custo.
        alvo = _escopo_semestre(con, curso, semestre)
        achados = busca.buscar(
            con, termo, courseid=curso or None, site=alvo, limite=limite, rerank=True
        )
        if not achados:
            extra = ""
            if alvo and db.tem_outro_semestre(con, alvo):
                # Sem isto o material do semestre passado fica inalcançável na
                # prática: ninguém adivinha que existe um parâmetro.
                extra = (
                    f" Procurei só em {alvo}; há material de outro semestre — "
                    'repita com semestre="*" para incluir.'
                )
            return (
                f"“{termo}” não aparece nos materiais extraídos.{extra} "
                "(6 PDFs escaneados não entram na busca — veja `cli.py materiais`.)"
            )
        partes = []
        for a in achados:
            onde = f"[{a['arquivo_id']}] {a['nome']}"
            if a["rotulo"]:
                onde += f", {a['rotulo']}"
            if a["secao"]:
                onde += f" — {a['secao']}"
            abrir = f" · {a['modname']}/{a['cmid']}" if a["cmid"] else ""
            partes.append(f"\n### {onde} (curso {a['courseid']}{abrir})\n{a['trecho']}")
        aviso = busca.aviso_cobertura(con, termo, achados, curso or None)
        return (
            (aviso + "\n" if aviso else "")
            + _linha_de_link({a["site"] for a in achados if a["cmid"]})
            + "\n".join(partes)
        )
    finally:
        con.close()


@fria()
def cobrir_topico(
    termo: str, curso: int = 0, limite: int = 12,
    documento: str = "", novidade: bool = False, semestre: str = "",
) -> str:
    """Material de vários arquivos sobre um tópico, para montar resumo ou apostila.

    Mais caro que buscar_material e menos preciso: atravessa para os arquivos
    vizinhos no grafo, então traz material que a busca por termo não alcança.
    Use quando precisar COBRIR um assunto, não para responder uma pergunta.

    `documento` registra o que foi devolvido sob esse nome ("Prova 2").
    `novidade` devolve só o que ainda não entrou nesse documento.
    """
    con = _con()
    try:
        if not busca.indexado(con):
            return "Índice vazio. Rode `cli.py extrair --reindexar`."
        cid = curso or None
        ja = memoria.shas_cobertos(con, cid, "topico", documento) if documento else set()
        if novidade and not ja:
            return (
                f"Nada registrado ainda em “{documento}”. Chame sem `novidade` "
                "para montar a primeira versão."
            )
        pedido = limite * 3 if novidade else limite
        achados = busca.buscar(
            con, termo, courseid=cid, site=_escopo_semestre(con, curso, semestre),
            limite=pedido,
            orcamento_chars=8000 if not novidade else 24000, ponte=True,
        )
        if novidade:
            achados = [a for a in achados if a["sha256"] not in ja][:limite]
            if not achados:
                return (
                    f"Nada novo sobre “{termo}” além do que já está em "
                    f"“{documento}” ({len(ja)} trecho(s) cobertos)."
                )
        if not achados:
            return f"Nada sobre “{termo}” nos materiais extraídos."
        if documento:
            memoria.registrar_cobertura(
                con, cid, "topico", documento, [a["sha256"] for a in achados]
            )
        partes = []
        for a in achados:
            onde = f"[{a['arquivo_id']}] {a['nome']}"
            if a["rotulo"]:
                onde += f", {a['rotulo']}"
            partes.append(f"\n### {onde} (curso {a['courseid']})\n{a['trecho']}")
        aviso = busca.aviso_cobertura(con, termo, achados, curso or None)
        return (aviso + "\n" if aviso else "") + "\n".join(partes)
    finally:
        con.close()


@fria()
def cobertura(curso: int = 0) -> str:
    """O que já foi resumido ou montado, e o que herdaria resumo pelo grafo."""
    con = _con()
    try:
        linhas = []
        res = memoria.listar_resumos(con, curso or None)
        linhas.append(f"{len(res)} resumo(s) guardado(s) e válido(s):")
        for r in res:
            linhas.append(f"  [{r['arquivo_id']}] {r['nome'][:52]} ({r['tamanho']} chars)")
        if curso:
            herd = memoria.herdeiros_de_resumo(con, curso)
            if herd:
                linhas.append(f"\n{len(herd)} arquivo(s) herdariam resumo por conteúdo compartilhado:")
                for h in herd:
                    linhas.append(f"  [{h['arquivo_id']}] {h['nome'][:44]} <- do arquivo {h['fonte']}")
        prod = memoria.listar_produzido(con, curso or None)
        if prod:
            linhas.append(f"\n{len(prod)} documento(s) registrado(s):")
            for p in prod:
                linhas.append(f"  {p['escopo']}:{p['rotulo']} — {p['n']} trecho(s), {p['criado_em']}")
        return "\n".join(linhas) or "Nada registrado ainda."
    finally:
        con.close()


@fria()
def trecho_material(arquivo_id: int, pagina: int = 0, vizinhos: int = 1) -> str:
    """Lê uma página do material e as vizinhas. Barato; prefira a texto_material."""
    con = _con()
    try:
        linhas = busca.trechos_do_arquivo(
            con, arquivo_id, pagina or None, vizinhos
        )
        if not linhas:
            return "Sem trecho indexado para esse arquivo/página."
        return "\n\n".join(
            f"--- {t['rotulo'] or 'trecho ' + str(t['ordinal'])} ---\n{t['texto']}"
            for t in linhas
        )
    finally:
        con.close()


@fria()
def texto_material(arquivo_id: int, limite_chars: int = 20000) -> str:
    """PDF inteiro (até 20k chars). CARA — para achar assunto, use buscar_material."""
    con = _con()
    try:
        t = extract.texto_de(con, arquivo_id)
        return t[:limite_chars] or "Sem texto extraído para esse arquivo."
    finally:
        con.close()


# --------------------------------------------------------------------------
# Companion de estudo: revisão assistida de questionário
# --------------------------------------------------------------------------


@mcp.tool()
def memoria_contexto(site: str = "") -> str:
    """Apelidos memorizados. Chame UMA VEZ, na primeira pergunta da conversa."""
    con = _con()
    try:
        alias = site
        if alias is None:
            try:
                alias = get_client(None).alias
            except MoodleError:
                alias = None
        return memoria.contexto(con, alias or None)
    finally:
        con.close()


@fria()
def memorizar(
    termo: str, alvo_tipo: str, alvo_id: int,
    rotulo: str = "", site: str = "",
) -> str:
    """Memoriza um apelido: "macro" = curso 6095.

    alvo_tipo: curso | quiz | arquivo | forum | tarefa | secao.
    Nota, prazo e status são recusados: memorize o caminho, não o valor.
    """
    con = _con()
    try:
        alias = site or get_client(site or None).alias
        return memoria.memorizar(con, alias, termo, alvo_tipo, alvo_id, rotulo or None)
    finally:
        con.close()


@fria()
def esquecer(termo: str, site: str = "") -> str:
    """Apaga um apelido memorizado."""
    con = _con()
    try:
        alias = site or get_client(site or None).alias
        return memoria.esquecer(con, alias, termo)
    finally:
        con.close()


@fria()
def guardar_resumo(
    arquivo_id: int, resumo: str, site: str = ""
) -> str:
    """Guarda resumo de material (chave: sha256 — morre se o arquivo mudar)."""
    con = _con()
    try:
        r = db.query(con, "SELECT sha256 FROM arquivos WHERE id = ?", (arquivo_id,))
        if not r or not r[0]["sha256"]:
            return f"Arquivo {arquivo_id} não tem sha256 (não foi baixado?)."
        return memoria.guardar_resumo(con, r[0]["sha256"], resumo, arquivo_id)
    finally:
        con.close()


@fria()
def resumo_material(arquivo_id: int) -> str:
    """Resumo já guardado deste material, se o arquivo não mudou desde então."""
    con = _con()
    try:
        estado, r = memoria.estado_do_resumo(con, arquivo_id)
        if estado == "valido":
            return r or ""
        if estado == "desatualizado":
            return (
                "Havia um resumo deste arquivo, mas o professor republicou com "
                "conteúdo diferente e ele não vale mais. Refazer é barato: o "
                "assunto já é conhecido."
            )
        herdeiros = memoria.fonte_de_resumo_irmao(con, arquivo_id)
        if herdeiros:
            h = herdeiros[0]
            return (
                f"Nunca resumi este arquivo, mas o arquivo {h['fonte']} "
                f"({h['nome'][:44]}) compartilha conteúdo com ele e TEM resumo. "
                f"Chame resumo_material({h['fonte']}) antes de ler o material."
            )
        return "Nunca resumi este arquivo."
    finally:
        con.close()


def _linha_de_link(sites: set) -> str:
    """Uma linha com o molde do link, em vez de oito URLs inteiras.

    Cada resultado leva só `resource/61661`; a base sai aqui uma vez. Com URL
    completa por resultado eram ~60 caracteres a mais em cada um, e o orçamento
    de `buscar_material` é contado em caracteres.
    """
    urls = {c["url"] for s, c in load_sites().items() if s in sites}
    if len(urls) != 1:
        return ""
    return f"Abrir no Moodle: {urls.pop()}/mod/<tipo>/view.php?id=<n> (tipo/n em cada fonte)\n"


def _curto(shortname: str | None) -> str:
    """20262_1000070_DIG_ECN054_TC -> ECN054. O código inteiro só ocupa espaço."""
    if not shortname:
        return "?"
    for parte in shortname.split("_"):
        if len(parte) >= 6 and parte[:3].isalpha() and any(c.isdigit() for c in parte):
            return parte
    return shortname[:14]


@fria()
def extrair_programa(
    curso: int = 0, tudo: bool = False, site: str = ""
) -> str:
    """Lê o plano de ensino e propõe datas de avaliação como PENDENTES.

    Nada é confirmado sozinho: o usuário confere o trecho e confirma.
    """
    con = _con()
    try:
        alias = site or get_client(site or None).alias
        if curso:
            rs = [programa.extrair_curso(con, alias, curso, so_avaliacoes=not tudo)]
        else:
            rs = programa.extrair_todos(con, alias, so_avaliacoes=not tudo)
        saida = []
        for r in rs:
            if r.erro:
                saida.append(f"{r.nome_arquivo}: {r.erro}")
                continue
            saida.append(f"\n{r.nome_arquivo} — {r.criados} novas, {r.ignorados} já existentes")
            for e in r.entradas:
                saida.append(f"  {e.data.strftime('%d/%m/%Y')} {e.titulo[:70]}")
            for a in r.fora_de_ordem:
                saida.append(f"  ! fora de ordem: {a}")
        saida.append("\nTodas PENDENTES — confirme com o usuário antes de tratar como certas.")
        return "\n".join(saida)
    finally:
        con.close()


# Semanas à frente em que a carga é comparada. Quatro cobre o intervalo entre
# duas provas numa matéria de três provas por semestre.
SEMANAS_CARGA = 4
# Peso de uma prova (3) ou de três entregas: abaixo disso não há semana cheia.
PESO_SEMANA_CHEIA = 3
AVISOS_NO_BRIEFING = 3

# Mudança em arquivo já aparece como MATERIAL NOVO; rótulo não é conteúdo.
_SEM_MUDANCA = "'resource', 'folder', 'label'"
# O que exige ação e cuja conclusão o sync não falseia. Arquivo, página e
# livro ficam de fora: baixar pela API não marca visualização, e medido, 50 de
# 50 arquivos das três matérias apareciam como "não concluído".
_EXIGE_ACAO = "'quiz', 'assign', 'lesson', 'h5pactivity', 'feedback', 'choice'"


@fria()
def briefing(dias: int = 7, site: str = "") -> str:
    """Prazos, material novo e o que estudar — uma chamada no lugar de quatro.

    Use para "o que tem essa semana?", "tenho entrega?", "o que estudo hoje?".
    """
    agora = int(time.time())
    con = _con()
    try:
        limite = agora + dias * 86400
        prazos = db.query(
            con,
            """SELECT e.titulo, e.data_inicio, e.tipo, c.shortname
               FROM eventos e LEFT JOIN cursos c
                 ON c.site = e.site AND c.courseid = e.courseid
               WHERE e.cancelado = 0 AND e.data_inicio BETWEEN ? AND ?
               ORDER BY e.data_inicio""",
            (agora, limite),
        )
        novos = db.query(
            con,
            """SELECT a.nome, c.shortname FROM arquivos a
               LEFT JOIN cursos c ON c.site = a.site AND c.courseid = a.courseid
               WHERE a.baixado_em >= date('now', ?) ORDER BY a.baixado_em DESC""",
            (f"-{dias} day",),
        )
        pendentes = db.query(
            con,
            """SELECT COUNT(*) n FROM questoes q
               LEFT JOIN respostas r
                 ON r.id = (SELECT MAX(id) FROM respostas WHERE questao_id = q.id)
               WHERE q.revisada = 1 AND (r.acertou = 0
                     OR (q.proposta_ia IS NOT NULL AND q.proposta_ia <> ''
                         AND q.proposta_ia <> q.correta))""",
        )[0]["n"]
        # `ignorado` tira do aviso o que você já disse não importar — senão
        # o alerta vira ruído permanente e você para de ler os avisos.
        ocr = db.query(
            con,
            "SELECT COUNT(*) n FROM arquivos WHERE precisa_ocr = 1 AND ignorado = 0",
        )[0]["n"]
        avisos_ = db.query(
            con,
            """SELECT v.criado_em, v.autor, v.assunto, v.texto, v.prioridade,
                      v.origem, c.shortname
               FROM avisos v LEFT JOIN cursos c
                 ON c.site = v.site AND c.courseid = v.courseid
               WHERE v.criado_em >= ? ORDER BY v.prioridade DESC, v.criado_em DESC""",
            (agora - dias * 86400,),
        )
        # Só o que o próprio Moodle ainda lista como ação sua: entrega vencida
        # que você já fez não é atraso, e mostrá-la ensinaria a ignorar a linha.
        atrasados = db.query(
            con,
            """SELECT e.titulo, e.data_inicio, c.shortname FROM eventos e
               LEFT JOIN cursos c ON c.site = e.site AND c.courseid = e.courseid
               WHERE e.cancelado = 0 AND e.pendente_acao = 1
                 AND e.data_inicio BETWEEN ? AND ? ORDER BY e.data_inicio""",
            (agora - 14 * 86400, agora),
        )
        carga_ev = db.query(
            con,
            """SELECT data_inicio, tipo FROM eventos
               WHERE cancelado = 0 AND tipo IN ('prova', 'entrega')
                 AND data_inicio BETWEEN ? AND ?""",
            (agora, agora + SEMANAS_CARGA * 7 * 86400),
        )
        mudou = db.query(
            con,
            f"""SELECT m.modname, m.nome, c.shortname FROM modulos m
                JOIN cursos c ON c.site = m.site AND c.courseid = m.courseid
                WHERE c.acompanhar = 1 AND m.mudou_em >= ?
                  AND m.modname NOT IN ({_SEM_MUDANCA})
                ORDER BY m.mudou_em DESC""",
            (agora - dias * 86400,),
        )
        pendentes_moodle = db.query(
            con,
            f"""SELECT m.modname, m.nome, c.shortname FROM modulos m
                JOIN cursos c ON c.site = m.site AND c.courseid = m.courseid
                WHERE c.acompanhar = 1 AND m.concluido = 0
                  AND m.modname IN ({_EXIGE_ACAO})""",
        )
    finally:
        con.close()

    linhas = []
    if prazos:
        linhas.append(f"PRAZOS ({dias}d):")
        for r in prazos:
            linhas.append(
                f"  {_ts(r['data_inicio'])} [{r['tipo']}] {r['titulo'][:70]}"
                f" ({_curto(r['shortname'])})"
            )
    else:
        linhas.append(f"PRAZOS ({dias}d):   nenhum")

    if novos:
        por_curso: dict[str, int] = {}
        for r in novos:
            c = _curto(r["shortname"])
            por_curso[c] = por_curso.get(c, 0) + 1
        resumo = ", ".join(f"{n} em {c}" for c, n in por_curso.items())
        linhas.append(f"MATERIAL NOVO: {resumo}")
    else:
        linhas.append("MATERIAL NOVO: nada nos últimos %d dias" % dias)

    linhas.append(
        f"ESTUDO:        {pendentes} questão(ões) no roteiro"
        if pendentes else "ESTUDO:        roteiro vazio"
    )
    if ocr:
        linhas.append(f"ATENÇÃO:       {ocr} PDF(s) escaneado(s) invisíveis à busca")

    for r in atrasados:
        linhas.append(
            f"ATRASADO:      {r['titulo'][:60]} ({_curto(r['shortname'])}), "
            f"venceu {_ts(r['data_inicio'])[:5]}"
        )

    semana = _semana_mais_pesada(carga_ev)
    if semana:
        linhas.append(f"CARGA:         {semana}")

    # Aviso de pessoa entra com texto; o automático só conta. Recibo de envio e
    # resumo de fórum em lista fariam o usuário parar de ler esta seção — e é
    # por ela que chega "prova adiada".
    gente = [r for r in avisos_ if r["prioridade"] >= 1]
    sistema = len(avisos_) - len(gente)
    if gente:
        linhas.append(
            f"AVISOS ({dias}d):   {len(gente)}"
            + (f" (+{sistema} automáticos omitidos)" if sistema else "")
        )
        for r in gente[:AVISOS_NO_BRIEFING]:
            # Quem posta no fórum de avisos nem sempre é professor (o monitor
            # de ECN300 posta lá); "prof." só quando o id bateu com a lista de
            # professores da turma.
            quem = "prof. " if r["prioridade"] >= 2 and r["origem"] != "forum_avisos" else ""
            curso = f" {_curto(r['shortname'])}" if r["shortname"] else ""
            linhas.append(
                f"  {_ts(r['criado_em'])[:5]}{curso} {quem}{(r['autor'] or '?')[:28]}: "
                f"{(r['texto'] or r['assunto'] or '')[:140]}"
            )
        if len(gente) > AVISOS_NO_BRIEFING:
            linhas.append(f"  … mais {len(gente) - AVISOS_NO_BRIEFING}: avisos()")

    if mudou:
        linhas.append("MUDOU NO MOODLE: " + _agrupar(mudou))
    if pendentes_moodle:
        linhas.append("NÃO CONCLUÍDO:  " + _agrupar(pendentes_moodle, so_contar=True))
    return "\n".join(linhas)


def _agrupar(linhas_: list, so_contar: bool = False) -> str:
    """'ECN054: 2 quiz (Lista 1, Lista 2); ECN231: 1 assign' — uma linha só."""
    por: dict[str, list] = {}
    for r in linhas_:
        por.setdefault(_curto(r["shortname"]), []).append(r)
    partes = []
    for curso, rs in por.items():
        tipos: dict[str, int] = {}
        for r in rs:
            tipos[r["modname"]] = tipos.get(r["modname"], 0) + 1
        txt = ", ".join(f"{n} {t}" for t, n in tipos.items())
        if not so_contar:
            nomes = [(r["nome"] or "")[:30] for r in rs[:3]]
            txt += f" ({'; '.join(nomes)}{'; …' if len(rs) > 3 else ''})"
        partes.append(f"{curso}: {txt}")
    return " | ".join(partes)


def _semana_mais_pesada(eventos_: list) -> str:
    """A semana que merece aviso, por conta: prova pesa 3, entrega pesa 1.

    Só aparece se a mais pesada tiver peso de uma prova ou de três entregas —
    abaixo disso toda semana seria "a mais pesada" e a linha não diria nada.
    """
    import datetime as _dt

    pesos: dict[_dt.date, list[int]] = {}
    for r in eventos_:
        d = _dt.date.fromtimestamp(int(r["data_inicio"]))
        seg = d - _dt.timedelta(days=d.weekday())
        p = pesos.setdefault(seg, [0, 0])
        p[0 if r["tipo"] == "prova" else 1] += 1
    if not pesos:
        return ""
    seg, (prov, ent) = max(pesos.items(), key=lambda kv: (3 * kv[1][0] + kv[1][1], -kv[0].toordinal()))
    if 3 * prov + ent < PESO_SEMANA_CHEIA:
        return ""
    partes = []
    if prov:
        partes.append(f"{prov} prova{'s' if prov > 1 else ''}")
    if ent:
        partes.append(f"{ent} entrega{'s' if ent > 1 else ''}")
    return (f"semana de {seg.strftime('%d/%m')}: {' + '.join(partes)} "
            f"(a mais pesada das próximas {SEMANAS_CARGA})")


@fria()
def avisos(dias: int = 14, curso: int = 0, automaticos: bool = False) -> str:
    """Mensagens e avisos de professores e colegas, com texto. Sem rede: lê o sync."""
    con = _con()
    try:
        sql = """SELECT v.*, c.shortname FROM avisos v LEFT JOIN cursos c
                   ON c.site = v.site AND c.courseid = v.courseid
                 WHERE v.criado_em >= ?"""
        params: list = [int(time.time()) - dias * 86400]
        if curso:
            sql += " AND v.courseid = ?"
            params.append(curso)
        if not automaticos:
            sql += " AND v.prioridade >= 1"
        linhas = db.query(con, sql + " ORDER BY v.criado_em DESC LIMIT 30", params)
        if not linhas:
            return f"Nenhum aviso nos últimos {dias} dias." + (
                "" if automaticos else " (automaticos=True inclui os do sistema)")
        con.execute(
            f"UPDATE avisos SET visto_em = ? WHERE id IN ({','.join('?' * len(linhas))})",
            [time.strftime("%Y-%m-%dT%H:%M:%S"), *[r["id"] for r in linhas]],
        )
        con.commit()
        out = []
        for r in linhas:
            quem = {2: "professor", 1: "pessoa", 0: "sistema"}[r["prioridade"]]
            curso_ = f" ({_curto(r['shortname'])})" if r["shortname"] else ""
            out.append(
                f"\n### {_ts(r['criado_em'])} — {r['autor'] or '?'} [{quem}, {r['origem']}]{curso_}"
                + (f"\n{r['assunto']}" if r["assunto"] and r["origem"] != "mensagem" else "")
                + f"\n{r['texto'] or ''}"
                + (f"\n{r['url']}" if r["url"] else "")
            )
        return "\n".join(out)
    finally:
        con.close()


@fria()
def preparar_questionario(attemptid: int, site: str = "") -> str:
    """Monta a sessão de revisão: uma linha por questão, sessão salva em disco.

    Detalhe sai por `questao_detalhe`, só nos slots que interessam.
    Tentativa finalizada: o gabarito é do Moodle, você só explica.
    Tentativa aberta: preencha proposta_ia/explicacao e chame salvar_revisao.
    """
    con = _con()
    try:
        sessao = companion.preparar_revisao(con, get_client(site or None), attemptid)
        liberado, motivo = companion.pode_enviar(sessao, con)
    finally:
        con.close()

    destino = companion.gravar_sessao(sessao)
    return companion.resumo_sessao(sessao, liberado, motivo) + f"\nSessão: {destino}"


@fria()
def questao_detalhe(attemptid: int, slots: list[int], site: str = "") -> str:
    """Detalhe das questões pedidas: alternativas, correção e fonte.

    Depois de preparar_questionario, só nos slots que importam.
    """
    try:
        sessao = companion.carregar_sessao(attemptid)
    except FileNotFoundError:
        return (
            f"Sessão {attemptid} não preparada ainda. "
            "Chame preparar_questionario primeiro."
        )
    return companion.detalhe_questoes(sessao, slots)


@fria()
def salvar_revisao(sessao_json: str) -> str:
    """Grava a sessão revisada em questoes/respostas (proposta do modelo e a
    resposta aprovada, separadas)."""
    try:
        sessao = companion.Sessao.de_json(sessao_json)
    except (ValueError, TypeError) as e:
        return f"JSON da sessão inválido: {e}"
    con = _con()
    try:
        ids = companion.salvar_revisao(con, sessao)
        divergentes = [i.slot for i in sessao.itens if i.divergiu]
        msg = f"{len(ids)} questão(ões) salvas para estudo."
        if divergentes:
            msg += f" Você discordou do modelo nos slots {divergentes} — vão para o topo do roteiro."
        return msg + "\n\n" + companion.gabarito(sessao, con)
    finally:
        con.close()


@fria()
def enviar_revisao(
    sessao_json: str, finalizar: bool = True,
    confirmar: bool = False, site: str = "",
) -> str:
    """Envia as respostas aprovadas, conforme a política do curso.

    Bloqueado devolve o gabarito em vez de enviar.
    """
    try:
        sessao = companion.Sessao.de_json(sessao_json)
    except (ValueError, TypeError) as e:
        return f"JSON da sessão inválido: {e}"
    con = _con()
    try:
        r = companion.enviar_aprovado(
            con, get_client(site or None), sessao, finalizar=finalizar, confirmar=confirmar
        )
        if not r.ok and "não enviado" in r.detalhe:
            return str(r) + "\n\n" + companion.gabarito(sessao, con)
        return str(r)
    finally:
        con.close()


@fria()
def declarar_politica_envio(
    escopo: str, motivo: str, alvo: int = 0,
    permitir: bool = True, site: str = "",
) -> str:
    """Declara se pode enviar questionário que vale nota no Moodle.

    escopo: quiz (alvo=quizid) | curso (alvo=courseid) | global.
    Mais específico vence. motivo é obrigatório e fica registrado.
    """
    con = _con()
    try:
        alias = site or (get_client(site or None).alias)
        companion.declarar_politica(
            con, alias, escopo, alvo, permitir, motivo
        )
        onde = escopo if escopo == "global" else f"{escopo} {alvo}"
        acao = "liberado" if permitir else "bloqueado"
        return f"Política registrada: envio {acao} para {onde} em {alias}.\nMotivo: {motivo}"
    except ValueError as e:
        return f"Não registrei: {e}"
    finally:
        con.close()


@fria()
def politicas_envio(site: str = "") -> str:
    """Lista as políticas de envio declaradas, com motivo e data."""
    con = _con()
    try:
        sql = "SELECT * FROM politicas_envio"
        params: list = []
        if site:
            sql += " WHERE site = ?"
            params.append(site)
        linhas = db.query(con, sql + " ORDER BY site, escopo", params)
        return "\n".join(
            f"[{r['site']}] {r['escopo']}"
            f"{'' if r['escopo'] == 'global' else ' ' + str(r['alvo'])}: "
            f"{'LIBERADO' if r['permitir'] else 'bloqueado'} "
            f"({r['declarada_em']}) — {r['motivo']}"
            for r in linhas
        ) or "Nenhuma política declarada. Vale a heurística: só prática envia."
    finally:
        con.close()


@fria()
def corrigir_tentativa(attemptid: int, site: str = "") -> str:
    """Puxa a correção do Moodle para `respostas`. Rode após a correção."""
    con = _con()
    try:
        n = companion.registrar_correcao(con, get_client(site or None), attemptid)
        return f"{n} questão(ões) atualizadas com a correção do Moodle."
    finally:
        con.close()


@fria()
def roteiro_estudo(
    curso: int = 0, limite: int = 20, site: str = ""
) -> str:
    """Roteiro de estudo: o que você errou primeiro, depois onde discordou do modelo."""
    con = _con()
    try:
        return companion.roteiro_estudo(con, site=site or None, courseid=curso or None, limite=limite)
    finally:
        con.close()


# --------------------------------------------------------------------------
# Escrita. Todas passam por escrita.py: ensaio por padrão, log sempre.
# --------------------------------------------------------------------------


@fria()
def postar_forum(
    forumid: int, assunto: str, mensagem: str,
    confirmar: bool = False, site: str = "",
) -> str:
    """Abre uma discussão nova num fórum.

    Sem confirmar=True apenas mostra o que seria postado, sem enviar.
    """
    con = _con()
    try:
        return str(escrita.nova_discussao(
            con, get_client(site or None), forumid, assunto, mensagem, confirmar=confirmar
        ))
    finally:
        con.close()


@fria()
def responder_forum(
    postid: int, assunto: str, mensagem: str,
    confirmar: bool = False, site: str = "",
) -> str:
    """Responde a um post de fórum. Use posts_discussao para achar o postid."""
    con = _con()
    try:
        return str(escrita.responder_post(
            con, get_client(site or None), postid, assunto, mensagem, confirmar=confirmar
        ))
    finally:
        con.close()


@fria()
def posts_discussao(discussionid: int, site: str = "") -> str:
    """Lista os posts de uma discussão com seus ids (para responder)."""
    posts = escrita.posts_discussao(get_client(site or None), discussionid)
    return "\n".join(
        f"[{p.get('id')}] {p.get('author', {}).get('fullname', '?')}: "
        f"{(p.get('message') or '')[:200]}"
        for p in posts
    ) or "Sem posts."


@fria()
def anexar_tarefa(
    assignid: int, caminho: str, itemid: int = 0,
    confirmar: bool = False, site: str = "",
) -> str:
    """Sobe arquivo para o rascunho da tarefa. Devolve o itemid. Não entrega."""
    con = _con()
    try:
        return str(escrita.anexar_arquivo(
            con, get_client(site or None), assignid, caminho,
            itemid=itemid, confirmar=confirmar,
        ))
    finally:
        con.close()


@fria()
def salvar_tarefa(
    assignid: int, itemid: int = 0, texto: str = "",
    confirmar: bool = False, site: str = "",
) -> str:
    """Salva rascunho da entrega (anexos e/ou texto). NÃO entrega."""
    con = _con()
    try:
        return str(escrita.salvar_tarefa(
            con, get_client(site or None), assignid,
            itemid=itemid, texto=texto, confirmar=confirmar,
        ))
    finally:
        con.close()


@fria()
def entregar_tarefa(
    assignid: int, aceitar_declaracao: bool = False,
    confirmar: bool = False, site: str = "",
) -> str:
    """Entrega a tarefa. IRREVERSÍVEL — confira antes com status_tarefa."""
    con = _con()
    try:
        return str(escrita.enviar_tarefa(
            con, get_client(site or None), assignid,
            aceitar_declaracao=aceitar_declaracao, confirmar=confirmar,
        ))
    finally:
        con.close()


@fria()
def tentativas_questionario(quizid: int, site: str = "") -> str:
    """Lista suas tentativas num questionário, com estado e nota."""
    ts = escrita.tentativas(get_client(site or None), quizid)
    return "\n".join(
        f"[{t.get('id')}] tentativa {t.get('attempt')} — {t.get('state')} "
        f"iniciada {_ts(t.get('timestart'))} nota={t.get('sumgrades')}"
        for t in ts
    ) or "Nenhuma tentativa."


@fria()
def iniciar_questionario(quizid: int, confirmar: bool = False, site: str = "") -> str:
    """Inicia uma tentativa de questionário. Consome uma tentativa permitida."""
    con = _con()
    try:
        return str(escrita.iniciar_questionario(
            con, get_client(site or None), quizid, confirmar=confirmar
        ))
    finally:
        con.close()


@fria()
def ler_questionario(attemptid: int, pagina: int = 0, site: str = "") -> str:
    """Página da tentativa: enunciados e nomes de campo (q42:1_answer)."""
    d = escrita.dados_tentativa(get_client(site or None), attemptid, pagina)
    out = []
    for q in d.get("questions", []):
        out.append(
            f"\n## slot {q.get('slot')} — {q.get('type')} — {q.get('state')}"
            f"\ncampos: {', '.join(q.get('campos') or []) or '(nenhum encontrado)'}"
            f"\n{re.sub(r'<[^>]+>', ' ', q.get('html') or '')[:1200]}"
        )
    return "\n".join(out) or "Sem questões nesta página."


@fria()
def salvar_questionario(
    attemptid: int, respostas_json: str,
    confirmar: bool = False, site: str = "",
) -> str:
    """Salva respostas sem finalizar. respostas_json: {"q42:1_answer":"2"}."""
    con = _con()
    try:
        respostas = json.loads(respostas_json)
    except json.JSONDecodeError as e:
        return f"JSON inválido: {e}"
    try:
        return str(escrita.salvar_questionario(
            con, get_client(site or None), attemptid, respostas, confirmar=confirmar
        ))
    finally:
        con.close()


@fria()
def responder_questionario(
    attemptid: int, respostas_json: str = "{}", finalizar: bool = True,
    confirmar: bool = False, site: str = "",
) -> str:
    """Processa a tentativa; finalizar=True encerra e manda corrigir."""
    con = _con()
    try:
        respostas = json.loads(respostas_json)
    except json.JSONDecodeError as e:
        return f"JSON inválido: {e}"
    try:
        return str(escrita.enviar_questionario(
            con, get_client(site or None), attemptid, respostas,
            finalizar=finalizar, confirmar=confirmar,
        ))
    finally:
        con.close()


@fria()
def revisar_questionario(attemptid: int, site: str = "") -> str:
    """Resumo da tentativa: o que está respondido e o que ficou em branco."""
    d = escrita.revisar_tentativa(get_client(site or None), attemptid)
    return "\n".join(
        f"slot {q.get('slot')}: {q.get('state')} — {q.get('status')}"
        for q in d.get("questions", [])
    ) or "Sem dados."


@fria()
def historico_escrita(limite: int = 20) -> str:
    """Mostra tudo que este programa já publicou no Moodle em seu nome."""
    con = _con()
    try:
        linhas = escrita.historico(con, limite)
        return "\n".join(
            f"{r['em']} [{r['site']}] {r['acao']} {r['alvo']}: {r['resumo']}"
            + (f"  ERRO: {r['erro'][:120]}" if r["erro"] else "")
            for r in linhas
        ) or "Nada foi escrito no Moodle ainda."
    finally:
        con.close()


# --------------------------------------------------------------------------
# Apostila. E-mail vive em `server_correio.py`, servidor separado: o esquema
# das ferramentas entra no contexto a cada mensagem, e e-mail é caro e pouco
# frequente. Registre aquele servidor quando precisar.
# --------------------------------------------------------------------------





@fria()
def montar_apostila(
    diretorio: str, saida: str = "", titulo: str = "Apostila de estudo",
    curso: int = 0,
) -> str:
    """Monta as partes HTML numeradas (NN_*.html) de um diretório num PDF.

    Registra a cobertura a partir das citações do próprio documento. `curso`
    só é preciso quando as citações não bastam para identificá-lo.

    O formato esperado do conteúdo está na skill (referencias/enviar.md).
    """
    r = apostila.construir(diretorio, saida or None, titulo)
    # `apostila.py` é puro disco de propósito (não importa `db`), então o
    # registro é feito aqui, onde a conexão já existe.
    #
    # A cobertura sai das CITAÇÕES do documento montado, não de um parâmetro:
    # a apostila é obrigada a citar arquivo e página em toda afirmação, então
    # ela própria declara o material que usou. Pedir isso ao modelo custaria
    # token e ele esqueceria — e foi o que aconteceu: todas as apostilas já
    # montadas estavam gravadas com cobertura vazia e sem curso.
    extra = ""
    if r.pdf and not r.erro:
        con = _con()
        try:
            _, n, perdidos = memoria.registrar_apostila(
                con, titulo, r.citacoes, caminho=str(r.pdf),
                courseid=curso or None,
            )
            extra = f"\nCobertura registrada: {n} trecho(s)."
            if perdidos:
                # Dizer o que não casou é o que permite corrigir a citação; em
                # silêncio, o registro parece completo e não está.
                extra += (
                    " Não consegui casar com arquivo do banco: "
                    + ", ".join(f"“{x}”" for x in perdidos[:5])
                    + (f" (+{len(perdidos) - 5})" if len(perdidos) > 5 else "")
                )
        except Exception as e:  # noqa: BLE001 — registro não derruba a montagem
            extra = f"\n(cobertura não registrada: {type(e).__name__})"
        finally:
            con.close()
    return (
        str(r)
        + (f"\nPartes: {', '.join(r.partes)}" if r.partes else "")
        + extra
    )


# --------------------------------------------------------------------------
# Simulado a partir do material
# --------------------------------------------------------------------------


@fria()
def preparar_simulado(
    curso: int, topico: str, limite: int = 6, novidade: bool = False
) -> str:
    """Monta simulado: material com fonte para criar exercícios de treino.

    Traz o aviso de cobertura e o que já foi perguntado antes, para não
    repetir. `novidade=True` traz só o material ainda não usado neste tópico.
    """
    con = _con()
    try:
        if not busca.indexado(con):
            return "Índice vazio. Rode `cli.py extrair --reindexar`."
        achados, aviso = simulado.material(con, curso, topico, limite, novidade)
        if not achados:
            if novidade:
                return (
                    f"Nada de novo sobre “{topico}” além do que já virou questão. "
                    "Chame sem `novidade` para reusar o material."
                )
            return f"Nada sobre “{topico}” nos materiais extraídos do curso {curso}."
        L = []
        if aviso:
            # Antes do material, não depois: é a decisão de montar ou não que
            # depende dele.
            L.append(aviso)
            L.append(
                "Se for montar assim mesmo, diga ao usuário que o material é "
                "fraco neste ponto."
            )
        feitas = simulado.ja_perguntado(con, curso, topico)
        if feitas:
            L.append(f"\nJÁ PERGUNTADO neste tópico ({len(feitas)}), não repita:")
            for q in feitas[:8]:
                L.append(f"  [{q['id']}] {(q['enunciado'] or '')[:100]}")
        L.append("\nMATERIAL (cite arquivo e página em fonte_trecho):")
        for a in achados:
            onde = f"[{a['arquivo_id']}] {a['nome']}"
            if a["rotulo"]:
                onde += f", {a['rotulo']}"
            L.append(f"\n### {onde}\n{a['trecho']}")
        L.append(
            "\nEscreva as questões e grave com `guardar_simulado`. Cada uma "
            "precisa de fonte_trecho (arquivo e página) — sem isso é recusada."
        )
        return "\n".join(L)
    finally:
        con.close()


@fria()
def guardar_simulado(curso: int, topico: str, questoes_json: str) -> str:
    """Grava as questões de treino do simulado. Recusa questão sem fonte.

    questoes_json: [{"enunciado": "...", "alternativas": [{"id":"a","texto":"..."}],
    "correta": "a", "explicacao": "...", "fonte_trecho": "Aula 3.pdf, página 12",
    "arquivo_id": 23}]
    """
    try:
        questoes = json.loads(questoes_json)
    except ValueError as e:
        return f"questoes_json inválido: {e}"
    if isinstance(questoes, dict):
        questoes = [questoes]
    if not isinstance(questoes, list):
        return "questoes_json tem de ser uma lista de questões."
    con = _con()
    try:
        r = simulado.guardar(con, curso, topico, questoes)
        return str(r)
    finally:
        con.close()


@fria()
def simulado_ver(curso: int, topico: str = "", com_gabarito: bool = False) -> str:
    """Mostra o simulado/lista de exercícios para resolver, sem o gabarito.

    O gabarito só sai com com_gabarito=True.
    """
    con = _con()
    try:
        return simulado.listar(con, curso, topico, com_gabarito)
    finally:
        con.close()


@fria()
def responder_simulado(questao: int, resposta: str) -> str:
    """Corrige sua resposta a um exercício do simulado e mostra a fonte."""
    con = _con()
    try:
        return simulado.responder(con, questao, resposta)
    finally:
        con.close()


@fria()
def exportar_calendario(saida: str = "", curso: int = 0, todos: bool = False) -> str:
    """Grava os eventos confirmados num .ics para importar no calendário.

    Escreve um arquivo no disco. Só evento confirmado sai; `todos=True` inclui
    os automáticos, marcados como não confirmados no próprio título.
    """
    destino = Path(saida).expanduser() if saida else Path.home() / "Downloads" / "ufmg.ics"
    con = _con()
    try:
        r = calendario.exportar(con, destino, courseid=curso or None, todos=todos)
        if r.total == 0 and not todos:
            return (
                "Nenhum evento confirmado para exportar. Confirme com "
                "`evento confirmar`, ou chame de novo com todos=True para levar "
                "também os automáticos (vão marcados como não confirmados)."
            )
        return str(r)
    finally:
        con.close()


@fria()
def ajustar_evento(busca: str, data: str = "", titulo: str = "") -> str:
    """Muda data ou título de prova/prazo do banco local e o marca confirmado.

    `busca`: trecho do título ou #id; ambíguo é recusado. `data`: 2026-10-23T09:20.
    Não mexe no Moodle nem na agenda do Mac — depois, sincronizar_agenda.
    """
    if agenda_mac.so_leitura():
        return "Recusado: por aqui só se consulta. Ajuste a prova pelo terminal."
    if not data and not titulo:
        return "Diga a nova data, o novo título, ou os dois."
    con = _con()
    try:
        if busca.strip().startswith("#") and busca.strip()[1:].isdigit():
            achados = con.execute("SELECT * FROM eventos WHERE id = ?",
                                  (int(busca.strip()[1:]),)).fetchall()
        else:
            # Só o que ainda vai acontecer: "prova 2" casaria com a de 2025.
            achados = con.execute(
                """SELECT * FROM eventos WHERE cancelado = 0 AND data_inicio > ?
                     AND titulo LIKE ? ORDER BY data_inicio""",
                (int(time.time()) - 86400, f"%{busca.strip()}%"),
            ).fetchall()
        if not achados:
            return f"Nenhum evento futuro com “{busca}”."
        if len(achados) > 1:
            return "Mais de um evento casa — diga qual pelo #id:\n" + "\n".join(
                f"#{e['id']} {time.strftime('%d/%m %H:%M', time.localtime(e['data_inicio']))} {e['titulo']}"
                for e in achados)
        e = achados[0]
        agora = time.strftime("%Y-%m-%dT%H:%M:%S")
        mudou = []
        if data:
            try:
                nova = agenda_mac._instante(data)
            except ValueError as err:
                return str(err)
            con.execute("""INSERT INTO historico_eventos
                           (evento_id, campo, valor_antigo, valor_novo, origem, em)
                           VALUES (?,?,?,?,?,?)""",
                        (e["id"], "data_inicio", e["data_inicio"], nova, "manual", agora))
            mudou.append(f"{time.strftime('%d/%m %H:%M', time.localtime(e['data_inicio']))}"
                         f" → {time.strftime('%d/%m %H:%M', time.localtime(nova))}")
        if titulo:
            con.execute("""INSERT INTO historico_eventos
                           (evento_id, campo, valor_antigo, valor_novo, origem, em)
                           VALUES (?,?,?,?,?,?)""",
                        (e["id"], "titulo", e["titulo"], titulo, "manual", agora))
            mudou.append(f"título → {titulo}")
        # origem manual + confirmado: é o dono dizendo a data, e fonte
        # automática nunca mais passa por cima (invariante 2).
        con.execute(
            """UPDATE eventos SET data_inicio = COALESCE(?, data_inicio),
                   titulo = COALESCE(?, titulo), origem = 'manual', confirmado = 1,
                   atualizado_em = ? WHERE id = ?""",
            (agenda_mac._instante(data) if data else None, titulo or None, agora, e["id"]),
        )
        con.commit()
        return f"Evento #{e['id']} ({e['titulo']}): " + "; ".join(mudou) + ". Confirmado."
    finally:
        con.close()


@fria()
def horarios_livres(
    dia: str = "", dias: int = 7, das: str = "08:00", ate: str = "22:00",
    minimo_min: int = 60,
) -> str:
    """Janelas livres na agenda do Mac, dia a dia. Só lê; recorrência já expandida."""
    try:
        janelas = agenda_mac.livres(dia, dias, das, ate, minimo_min)
    except Exception as e:
        return f"Não consegui ler a agenda: {e}"
    if not janelas:
        return "Nenhuma janela livre nesse intervalo."
    return "\n".join(
        f"{agenda_mac._fmt(a)}–{time.strftime('%H:%M', time.localtime(b))} ({(b - a) // 60} min)"
        for a, b in janelas
    )


@fria()
def sincronizar_agenda(dias: int = 90, confirmar: bool = False) -> str:
    """Põe provas e prazos confirmados na agenda do Mac; tira os cancelados.

    Sem confirmar, só mostra o que faria. Só mexe em evento criado pelo Fênix.
    """
    con = _con()
    try:
        return str(agenda_mac.sincronizar_prazos(con, dias=dias, confirmar=confirmar))
    finally:
        con.close()


@fria()
def marcar_estudos(blocos: list, confirmar: bool = False, sobrepor: bool = False) -> str:
    """Marca blocos de estudo na agenda do Mac: [{titulo, inicio, fim|minutos, notas}].

    `inicio` como 2026-09-22T14:00. Sem confirmar, só mostra. Bloco que bate
    com compromisso é pulado, a não ser com sobrepor.
    """
    con = _con()
    try:
        try:
            itens = agenda_mac.itens_de_estudo(blocos)
        except (KeyError, ValueError) as e:
            return f"Bloco inválido: {e}"
        return str(agenda_mac.aplicar(con, itens, confirmar=confirmar, sobrepor=sobrepor))
    finally:
        con.close()


@fria()
def agenda_marcados(todos: bool = False) -> str:
    """O que o Fênix pôs na agenda do Mac, com a chave para desmarcar."""
    con = _con()
    try:
        linhas = agenda_mac.marcados(con, futuros=not todos)
        if not linhas:
            return "Nada marcado pelo Fênix."
        return "\n".join(
            f"{agenda_mac._fmt(r['inicio'])}  {r['titulo']}  [{r['chave']}]" for r in linhas
        )
    finally:
        con.close()


@fria()
def desmarcar_agenda(chaves: list, confirmar: bool = False) -> str:
    """Apaga da agenda do Mac eventos que o Fênix criou, pela chave. Irreversível."""
    con = _con()
    try:
        return str(agenda_mac.aplicar(con, [], apagar=list(chaves), confirmar=confirmar))
    finally:
        con.close()


@fria()
def novo_projeto_apostila(diretorio: str, titulo: str = "Apostila") -> str:
    """Cria diretório de apostila com o cabeçalho de estilo pronto."""
    d = apostila.novo_projeto(diretorio, titulo)
    return (
        f"Projeto em {d} — 00_head.html copiado do modelo.\n"
        "Acrescente as partes como 01_capa.html, 02_topico.html, "
        "…, e por último o gabarito. Depois chame montar_apostila."
    )


# --------------------------------------------------------------------------
# Prompts e recursos: a skill, para clientes que não leem `.claude/skills/`.
#
# No Claude Code a skill do repositório dá o procedimento — qual ferramenta,
# em que ordem, quando parar. O Claude Desktop não tem esse conceito, mas o
# protocolo MCP tem prompts, e eles chegam em qualquer cliente. É o mesmo
# conteúdo por outro caminho.
# --------------------------------------------------------------------------

_DISCIPLINA = """
Regras ao usar as ferramentas ufmg-moodle:
- Chame memoria_contexto() uma vez, na primeira pergunta, e não repita.
- Uma pergunta factual (nota, prazo) = uma ferramenta. Não enriqueça sem pedido.
- buscar_material ANTES de texto_material: a busca já devolve o trecho.
  texto_material traz o PDF inteiro e é a chamada mais cara que existe aqui.
- preparar_questionario devolve a listagem; o detalhe sai de questao_detalhe
  nos slots que interessam, não em todos.
- Toda afirmação sobre a matéria vem com arquivo + trecho. Quando o assunto não
  estiver no material, diga isso explicitamente antes de responder de
  conhecimento geral — a diferença é o que permite conferir.
- Escrita ensaia por padrão. Só passe confirmar=True se o usuário mandou
  enviar nesta mensagem; pedir para redigir não é pedir para publicar.
"""


@mcp.prompt(title="Situação atual")
def situacao() -> str:
    """Prazos, material novo e o que estudar — em uma chamada."""
    return (
        "Chame briefing() e me diga o que exige atenção agora, em poucas linhas. "
        "Se houver prova próxima, diga quantos dias faltam. Não chame outras "
        "ferramentas para 'completar' — o briefing já junta o que importa."
        + _DISCIPLINA
    )


@mcp.prompt(title="O que estudar")
def estudar(assunto: str = "") -> str:
    """Roteiro de estudo a partir do material e dos erros anteriores."""
    alvo = f" sobre {assunto}" if assunto else ""
    return (
        f"Monte um roteiro de estudo{alvo}.\n"
        "1. roteiro_estudo() para ver o que eu errei ou fiquei em dúvida.\n"
        "2. Para os assuntos do topo, buscar_material() e traga o trecho.\n"
        "3. Explique cada ponto citando o arquivo de onde veio.\n"
        "Não despeje material inteiro: o trecho da busca costuma bastar."
        + _DISCIPLINA
    )


@mcp.prompt(title="Revisar questionário")
def revisar(attemptid: str = "") -> str:
    """Revisão de uma tentativa já corrigida, com o gabarito do Moodle."""
    alvo = f" da tentativa {attemptid}" if attemptid else ""
    return (
        f"Vamos revisar o questionário{alvo}.\n"
        "1. Se eu não passei o id, use tentativas_questionario() para achar.\n"
        "2. preparar_questionario(attemptid) — listagem compacta, já mostra "
        "quais slots eu errei.\n"
        "3. questao_detalhe(attemptid, slots=[...]) SÓ nos slots errados.\n"
        "4. Explique cada erro citando o trecho do material que veio junto.\n"
        "Em tentativa finalizada o gabarito é o do Moodle, não sua proposta: "
        "o campo 'Moodle:' no detalhe é a autoridade."
        + _DISCIPLINA
    )


@mcp.prompt(title="Pesquisar no material")
def pesquisar(pergunta: str = "") -> str:
    """Busca nos slides e PDFs baixados, com citação obrigatória."""
    return (
        f"Pesquise nos meus materiais: {pergunta}\n"
        "Use buscar_material() com termos variados (sinônimo, termo em inglês, "
        "nome do modelo ou do autor). Duas ou três tentativas sem resultado "
        "significam que não está no material — diga isso em vez de responder "
        "como se estivesse."
        + _DISCIPLINA
    )


@mcp.prompt(title="Datas de prova do plano de ensino")
def provas() -> str:
    """Extrai do PDF do programa as datas que o calendário do Moodle não tem."""
    return (
        "Chame extrair_programa() e me mostre as avaliações encontradas.\n"
        "Elas entram como PENDENTES: são datas lidas de uma tabela em PDF e "
        "podem estar erradas. Mostre o que foi encontrado e me pergunte antes "
        "de tratar qualquer uma como confirmada. Se houver aviso de data fora "
        "de ordem, destaque — costuma ser erro de digitação no próprio programa."
        + _DISCIPLINA
    )


@mcp.resource("ufmg://guia", title="Como usar o companion")
def guia() -> str:
    """O procedimento completo, para anexar à conversa quando quiser."""
    return _DISCIPLINA + """
Fluxos principais:
- prazo/agenda            -> briefing()
- conteúdo de matéria     -> buscar_material(), depois texto_material se preciso
- nota                    -> notas(curso="macro")
- revisar questionário    -> preparar_questionario() + questao_detalhe()
- datas de prova          -> extrair_programa()
- postar/entregar         -> ensaio primeiro, confirmar=True só com autorização
- o que já publiquei      -> historico_escrita()
"""


# --------------------------------------------------------------------------
# Enxugar o esquema que vai no prefixo
# --------------------------------------------------------------------------


def _sem_titulo(o):
    """Remove `title` recursivamente de um JSON Schema."""
    if isinstance(o, dict):
        return {k: _sem_titulo(v) for k, v in o.items() if k != "title"}
    if isinstance(o, list):
        return [_sem_titulo(x) for x in o]
    return o


def enxugar_esquemas() -> int:
    """Tira do esquema o que o Pydantic gera e não informa nada.

    Ele emite `"title": "Termo"` para um parâmetro que já se chama `termo`, e
    `"title": "buscar_materialArguments"` para o objeto inteiro. É 35% do
    esquema de parâmetros e 21% de tudo que vai no prefixo a cada mensagem,
    sem mudar comportamento nenhum.

    Devolve quantos tokens (aproximados) saíram.
    """
    import json

    antes = depois = 0
    for info in mcp._tool_manager.list_tools():
        a = info.parameters
        antes += len(json.dumps(a, ensure_ascii=False)) // 4
        info.parameters = _sem_titulo(a)
        depois += len(json.dumps(info.parameters, ensure_ascii=False)) // 4
    return antes - depois


_ECONOMIA_ESQUEMA = enxugar_esquemas()


if __name__ == "__main__":
    mcp.run()

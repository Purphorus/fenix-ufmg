"""Agenda no Calendário do Mac: provas, prazos e blocos de estudo.

Por que o Calendar.app e não a API do Google: a conta já está configurada nele,
então **nenhuma credencial passa por aqui** — o mesmo desenho do `correio.py`
com o Mail.app. O Mac sincroniza com o Google sozinho. A API exigiria um app
OAuth, e app OAuth em modo "teste" tem o token revogado a cada 7 dias.

Duas vias, cada uma para o que ela faz bem:

- **escrita pelo AppleScript**, com o conteúdo em argv (aspas no título
  quebrariam a compilação, como no e-mail);
- **leitura do banco local do Calendário**, só leitura. O `whose start date`
  do AppleScript NÃO expande recorrência: uma reunião semanal apareceria só no
  dia em que foi criada, e o horário livre sairia errado. O `OccurrenceCache`
  do banco já traz cada ocorrência.

O Fênix só mexe no que ele criou. Todo evento dele leva nas notas uma linha
`fenix://<chave>`, e alterar ou apagar confere essa linha — a chave exata
daquele evento — no próprio AppleScript, no instante do toque. A tabela
`agenda_itens` diz o que procurar; a marca diz se pode.

A marca fica nas notas, e não no campo `url`, porque a conta Google (CalDAV)
descarta o `url` em silêncio: testado contra a conta real, o evento voltou com
`missing value`, e a trava recusou mover e apagar o próprio evento do Fênix.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import db
from db import registrar_escrita

MARCA = "fenix://"
CONFIG = db.BASE_DIR / "agenda_mac.json"
CALENDARIO_DB = (
    Path.home() / "Library/Group Containers/group.com.apple.calendar/Calendar.sqlitedb"
)
# O banco do Calendário conta segundos a partir de 2001-01-01 UTC.
_EPOCA_APPLE = 978307200

# Prazo de entrega é um instante (23:59), não um intervalo. No calendário ele
# vira um bloco curto que TERMINA no prazo, para não atravessar a meia-noite.
# Prova é no horário da aula: 2 horas-aula = 1h40. Com 2h, a prova de ERU
# das 07:30 de sexta invadia a aula de Micro das 09:20.
DURACAO_PROVA = 100 * 60
DURACAO_PRAZO = 30 * 60


# Agenda achada pelo nome, e nome repetido é recusado: o AppleScript do
# Calendário não devolve id de agenda (`calendarIdentifier` e `uid` dão
# -10000 em todas), e escolher "a primeira" com dois nomes iguais é adivinhar.
_AS_GRAVAR = """
on run argv
  set nomeAgenda to item 1 of argv
  set u to item 2 of argv
  set titulo to item 3 of argv
  set ini to my quando(item 4 of argv)
  set fim to my quando(item 5 of argv)
  set diaInteiro to ((item 6 of argv) is "sim")
  set notas to item 7 of argv
  set marca to item 8 of argv
  set regra to item 9 of argv
  set lugar to item 10 of argv
  tell application "Calendar"
    set cs to (every calendar whose name is nomeAgenda)
    if (count of cs) is not 1 then error "agenda '" & nomeAgenda & "': " & (count of cs) & " com esse nome"
    set cal to item 1 of cs
    set ev to missing value
    if u is not "" then
      -- `event id` é acesso direto (~2 s). `first event whose uid is` varre a
      -- agenda inteira e passou de um minuto na conta de trabalho.
      try
        set ev to event id u of cal
      end try
    end if
    if ev is missing value then
      set ev to make new event at end of events of cal with properties ¬
        {summary:titulo, start date:ini, end date:fim, allday event:diaInteiro, description:notas, location:lugar}
      if regra is not "" then set recurrence of ev to regra
      return "criado|" & (uid of ev)
    end if
    set atual to description of ev
    if atual is missing value then error "evento " & u & " não é do Fênix"
    if atual does not contain marca then error "evento " & u & " não é do Fênix"
    -- Início depois do fim atual é recusado pelo Calendário; a ordem das
    -- duas atribuições depende de para onde o evento anda.
    if ini > (end date of ev) then
      set end date of ev to fim
      set start date of ev to ini
    else
      set start date of ev to ini
      set end date of ev to fim
    end if
    set allday event of ev to diaInteiro
    set summary of ev to titulo
    set description of ev to notas
    set location of ev to lugar
    if regra is not "" then set recurrence of ev to regra
    return "atualizado|" & (uid of ev)
  end tell
end run

-- Data montada por partes, em hora local. `date "22/09/2026"` depende do
-- formato regional do Mac. O dia vai a 1 antes do mês: dia 31 num mês de 30
-- transbordaria para o mês seguinte.
on quando(t)
  set AppleScript's text item delimiters to "|"
  set p to text items of t
  set AppleScript's text item delimiters to ""
  set d to current date
  set day of d to 1
  set year of d to (item 1 of p) as integer
  set month of d to (item 2 of p) as integer
  set day of d to (item 3 of p) as integer
  set time of d to ((item 4 of p) as integer) * 3600 + ((item 5 of p) as integer) * 60
  return d
end quando
"""

_AS_APAGAR = """
on run argv
  tell application "Calendar"
    set cs to (every calendar whose name is (item 1 of argv))
    if (count of cs) is not 1 then error "agenda '" & (item 1 of argv) & "': " & (count of cs) & " com esse nome"
    set cal to item 1 of cs
    try
      set ev to event id (item 2 of argv) of cal
    on error
      return "ausente"
    end try
    set atual to description of ev
    if atual is missing value then error "evento não é do Fênix"
    if atual does not contain (item 3 of argv) then error "evento não é do Fênix"
    delete ev
    return "apagado"
  end tell
end run
"""

_AS_AGENDAS = """
tell application "Calendar"
  set linhas to {}
  repeat with c in every calendar
    set end of linhas to (name of c) & tab & ((writable of c) as text)
  end repeat
  set AppleScript's text item delimiters to linefeed
  return linhas as text
end tell
"""


def agora_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _osascript(script: str, *args: str, timeout: int = 60) -> str:
    if sys.platform != "darwin":
        raise RuntimeError(
            "a agenda do Fênix usa o Calendário do Mac e só funciona no macOS; "
            "fora dele, `cli.py calendario` exporta um .ics")
    for tentativa in range(2):
        r = subprocess.run(
            ["osascript", "-", *args],
            input=script, capture_output=True, text=True, timeout=timeout,
        )
        if r.returncode == 0:
            return r.stdout.strip()
        # -600: com o Calendário fechado, `tell` a partir de `on run argv` não
        # o abre — o script falha em vez de esperar. Abrir e tentar uma vez.
        if tentativa == 0 and "(-600)" in (r.stderr or ""):
            subprocess.run(["open", "-g", "-a", "Calendar"], capture_output=True)
            time.sleep(3)
            continue
        break
    raise RuntimeError((r.stderr or "erro no AppleScript").strip())


def _partes(epoch: int) -> str:
    d = datetime.fromtimestamp(epoch)
    return f"{d.year}|{d.month}|{d.day}|{d.hour}|{d.minute}"


# Nome do dia à mão: `%a` segue o locale do processo, que no launchd e no
# servidor MCP é C, e sairia "Tue".
_DIAS = ("seg", "ter", "qua", "qui", "sex", "sáb", "dom")


def _fmt(epoch: int, hora: bool = True) -> str:
    d = datetime.fromtimestamp(epoch)
    return f"{_DIAS[d.weekday()]} {d:%d/%m}" + (f" {d:%H:%M}" if hora else "")


# --------------------------------------------------------------------------
# Configuração: qual agenda recebe, quais contam como ocupado
# --------------------------------------------------------------------------


_RE_MARCA = re.compile(re.escape(MARCA) + r"(\S+)")


def _marca_em(notas: str | None) -> str:
    """A marca `fenix://<chave>` das notas, ou "" se o evento não é do Fênix."""
    m = _RE_MARCA.search(notas or "")
    return m.group(0) if m else ""


def config() -> dict:
    try:
        return json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        return {}


def agendas() -> list[dict]:
    """Agendas do Calendário, com se aceitam escrita."""
    saida = _osascript(_AS_AGENDAS)
    linhas = []
    for l in saida.splitlines():
        nome, _, gravavel = l.partition("\t")
        linhas.append({"nome": nome, "gravavel": gravavel == "true"})
    return linhas


def definir(
    nome: str, ocupado: list[str] | None = None, nao_ocupa: list[str] | None = None
) -> str:
    """Escolhe a agenda que recebe os eventos. Recusa nome repetido ou só leitura.

    `nao_ocupa` são trechos de título de evento que é aviso para os outros, e
    não compromisso seu — na agenda de trabalho, "Último ônibus (não estarei
    presencialmente)" ocupa das 18h à meia-noite todo dia e zeraria a noite.
    """
    achadas = [a for a in agendas() if a["nome"] == nome]
    if len(achadas) != 1:
        return f"'{nome}': {len(achadas)} agenda(s) com esse nome no Calendário. Preciso de exatamente uma."
    if not achadas[0]["gravavel"]:
        return f"'{nome}' é só leitura."
    anterior = config()
    cfg = {
        "agenda": nome,
        "ocupado": ocupado if ocupado is not None else anterior.get("ocupado", [nome]),
        "nao_ocupa": nao_ocupa if nao_ocupa is not None else anterior.get("nao_ocupa", []),
    }
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
    extra = f" Não contam: {', '.join(cfg['nao_ocupa'])}." if cfg["nao_ocupa"] else ""
    return f"Agenda do Fênix: {nome}. Ocupado vem de: {', '.join(cfg['ocupado'])}.{extra}"


def _destino() -> str:
    nome = config().get("agenda")
    if not nome:
        raise RuntimeError(
            "nenhuma agenda escolhida — rode `cli.py calendario-mac definir \"NOME\"`"
        )
    return nome


# --------------------------------------------------------------------------
# Leitura: ocupado e livre
# --------------------------------------------------------------------------


def ocupados(inicio: int, fim: int, contas: list[str] | None = None) -> list[dict]:
    """Compromissos entre dois instantes, com recorrência expandida.

    `contas` são nomes de conta (Store) do Calendário: o padrão é o que
    `definir` gravou. Fica de fora o que não ocupa: dia inteiro (feriado,
    aniversário), cancelado e marcado como "livre".
    """
    contas = contas if contas is not None else config().get("ocupado", [])
    if not contas:
        return []
    if not CALENDARIO_DB.exists():
        raise RuntimeError(f"banco do Calendário não encontrado em {CALENDARIO_DB}")
    marcas = ",".join("?" * len(contas))
    # mode=ro: o Calendário está com o banco aberto, e escrever nele por fora
    # corromperia a sincronização. Aqui só se lê.
    con = sqlite3.connect(f"file:{CALENDARIO_DB}?mode=ro", uri=True, timeout=5)
    try:
        linhas = con.execute(
            f"""SELECT o.occurrence_date + {_EPOCA_APPLE} AS ini,
                       o.occurrence_end_date + {_EPOCA_APPLE} AS fim,
                       i.summary, i.description, c.title
                  FROM OccurrenceCache o
                  JOIN CalendarItem i ON i.ROWID = o.event_id
                  JOIN Calendar c ON c.ROWID = o.calendar_id
                  JOIN Store s ON s.ROWID = c.store_id
                 WHERE s.name IN ({marcas})
                   AND i.all_day = 0
                   AND i.hidden = 0
                   AND COALESCE(i.status, 0) != 3        -- cancelado
                   AND COALESCE(i.availability, 0) != 1  -- "livre"
                   AND o.occurrence_end_date + {_EPOCA_APPLE} > ?
                   AND o.occurrence_date + {_EPOCA_APPLE} < ?
              ORDER BY ini""",
            (*contas, inicio, fim),
        ).fetchall()
    finally:
        con.close()
    nao_ocupa = [x.casefold() for x in config().get("nao_ocupa", [])]
    return [
        {"inicio": int(a), "fim": int(b), "titulo": t or "(sem título)",
         "marca": _marca_em(n), "agenda": cal}
        for a, b, t, n, cal in linhas
        if not any(x in (t or "").casefold() for x in nao_ocupa)
    ]


def livres(
    dia: str = "",
    dias: int = 7,
    das: str = "08:00",
    ate: str = "22:00",
    minimo_min: int = 60,
) -> list[tuple[int, int]]:
    """Janelas livres de pelo menos `minimo_min`, dia a dia, entre `das` e `ate`."""
    base = datetime.strptime(dia, "%Y-%m-%d") if dia else datetime.now()
    h0, m0 = (int(x) for x in das.split(":"))
    h1, m1 = (int(x) for x in ate.split(":"))
    agora = int(time.time())
    saida: list[tuple[int, int]] = []
    for k in range(dias):
        d = datetime.fromordinal(base.toordinal() + k)
        ini = int(d.replace(hour=h0, minute=m0).timestamp())
        fim = int(d.replace(hour=h1, minute=m1).timestamp())
        ini = max(ini, agora)
        if ini >= fim:
            continue
        cursor = ini
        for o in ocupados(ini, fim):
            if o["inicio"] - cursor >= minimo_min * 60:
                saida.append((cursor, o["inicio"]))
            cursor = max(cursor, o["fim"])
        if fim - cursor >= minimo_min * 60:
            saida.append((cursor, fim))
    return saida


# --------------------------------------------------------------------------
# Escrita
# --------------------------------------------------------------------------


@dataclass
class Item:
    """Um evento que o Fênix quer ver na agenda."""

    chave: str
    titulo: str
    inicio: int
    fim: int
    dia_inteiro: bool = False
    notas: str = ""
    origem: str = "estudo"
    # RRULE do iCalendar, sem o "RRULE:" — ex. FREQ=WEEKLY;UNTIL=20261205T025959Z.
    # Vazia = evento único.
    recorrencia: str = ""
    local: str = ""

    @property
    def marca(self) -> str:
        return MARCA + self.chave

    def notas_com_marca(self) -> str:
        return (self.notas + "\n" if self.notas else "") + self.marca

    def assinatura(self) -> str:
        # Mudou qualquer coisa visível → regravar. Igual → não tocar, e
        # rodar duas vezes não cria nem mexe em nada.
        base = (f"{self.titulo}|{self.inicio}|{self.fim}|{int(self.dia_inteiro)}|"
                f"{self.notas}|{self.recorrencia}|{self.local}")
        return hashlib.sha256(base.encode()).hexdigest()[:16]


@dataclass
class Resultado:
    ensaio: bool
    criar: list[Item] = field(default_factory=list)
    atualizar: list[Item] = field(default_factory=list)
    apagar: list[dict] = field(default_factory=list)
    iguais: int = 0
    conflitos: list[tuple[Item, list[dict]]] = field(default_factory=list)
    erros: list[str] = field(default_factory=list)
    agenda: str = ""
    recusa: str = ""

    def __str__(self) -> str:
        if self.recusa:
            return self.recusa
        verbo = "Faria" if self.ensaio else "Feito"
        L = [f"{verbo} em “{self.agenda}”:"]
        for rot, itens in (("criar", self.criar), ("atualizar", self.atualizar)):
            for it in itens:
                quando = (
                    f"{_fmt(it.inicio, hora=False)} (dia inteiro)"
                    if it.dia_inteiro
                    else f"{_fmt(it.inicio)}–{datetime.fromtimestamp(it.fim):%H:%M}"
                )
                repete = "  (toda semana)" if it.recorrencia else ""
                L.append(f"  {rot:<9} {quando}  {it.titulo}{repete}")
        for a in self.apagar:
            L.append(f"  {'apagar':<9} {a['titulo']}")
        if self.iguais:
            L.append(f"  ({self.iguais} já estava(m) certo(s), sem mexer)")
        for it, com in self.conflitos:
            nomes = "; ".join(f"{c['titulo']} {_fmt(c['inicio'])}" for c in com[:3])
            L.append(f"  CONFLITO {_fmt(it.inicio)} {it.titulo} × {nomes} — pulado")
        for e in self.erros:
            L.append(f"  ERRO {e}")
        if len(L) == 1:
            L.append("  nada a fazer")
        if self.ensaio and (self.criar or self.atualizar or self.apagar):
            L.append("Nada foi gravado. Repita com confirmar=True para gravar.")
        return "\n".join(L)


def so_leitura() -> bool:
    """Agenda só para consulta: é como o agente do WhatsApp roda.

    Decisão do usuário: pelo WhatsApp se lê a agenda, não se escreve. Sem
    esta trava o agente alcançaria `marcar_estudos` com confirmar=True, porque
    o ouvinte sobe o servidor do Fênix inteiro.
    """
    return bool(os.environ.get("FENIX_AGENDA_SO_LEITURA"))


def _registrado(con: sqlite3.Connection, chave: str) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM agenda_itens WHERE chave = ?", (chave,)).fetchone()


def aplicar(
    con: sqlite3.Connection,
    itens: list[Item],
    *,
    apagar: list[str] | None = None,
    confirmar: bool = False,
    sobrepor: bool = False,
) -> Resultado:
    """Caminho único de escrita na agenda: ensaia, grava, registra.

    `apagar` são chaves de `agenda_itens`. Bloco de estudo que bate com
    compromisso é pulado, a não ser com `sobrepor` — marcar estudo em cima de
    reunião é o erro que a leitura de ocupado existe para evitar.
    """
    try:
        agenda = _destino()
    except RuntimeError as e:
        return Resultado(ensaio=not confirmar, recusa=str(e))
    r = Resultado(ensaio=not confirmar, agenda=agenda)

    for it in itens:
        reg = _registrado(con, it.chave)
        if reg and reg["assinatura"] == it.assinatura() and reg["agenda"] == agenda:
            r.iguais += 1
            continue
        if it.origem == "estudo" and not sobrepor:
            try:
                # O próprio bloco (já gravado antes) não conflita consigo;
                # uma prova que o Fênix pôs na agenda conflita, sim.
                com = [o for o in ocupados(it.inicio, it.fim) if o["marca"] != it.marca]
            except Exception as e:  # banco ilegível não pode virar "livre"
                r.erros.append(f"não consegui ler os ocupados: {e}")
                return r
            if com:
                r.conflitos.append((it, com))
                continue
        (r.atualizar if reg else r.criar).append(it)
    for chave in apagar or []:
        reg = _registrado(con, chave)
        if reg:
            r.apagar.append(dict(reg))

    if not confirmar:
        return r
    if so_leitura():
        r.recusa = ("Recusado: por aqui a agenda é só para consulta. "
                    "Para gravar, use o terminal (cli.py calendario-mac).")
        return r
    # Mesma trava estrutural de `escrita._executar`: o agente automático
    # PROPÕE, e o dono confirma por fora.
    if os.environ.get("FENIX_SOMENTE_LEITURA"):
        r.recusa = "Recusado: esta sessão é somente leitura (agente automático)."
        return r

    for it in r.criar + r.atualizar:
        reg = _registrado(con, it.chave)
        payload = {
            "agenda": agenda, "chave": it.chave, "titulo": it.titulo,
            "inicio": it.inicio, "fim": it.fim, "dia_inteiro": it.dia_inteiro,
            "recorrencia": it.recorrencia, "local": it.local,
        }
        try:
            saida = _osascript(
                _AS_GRAVAR, agenda, reg["uid"] if reg and reg["agenda"] == agenda else "",
                it.titulo, _partes(it.inicio), _partes(it.fim),
                "sim" if it.dia_inteiro else "nao", it.notas_com_marca(), it.marca,
                it.recorrencia, it.local,
            )
        except Exception as e:
            registrar_escrita(con, "calendar.app", "agenda_gravar", alvo=it.chave,
                              resumo=it.titulo, payload=payload, erro=str(e))
            r.erros.append(f"{it.titulo}: {e}")
            continue
        estado, _, uid = saida.partition("|")
        con.execute(
            """INSERT INTO agenda_itens
                 (chave, agenda, uid, titulo, inicio, fim, dia_inteiro,
                  assinatura, origem, criado_em, atualizado_em)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(chave) DO UPDATE SET
                 agenda=excluded.agenda, uid=excluded.uid, titulo=excluded.titulo,
                 inicio=excluded.inicio, fim=excluded.fim,
                 dia_inteiro=excluded.dia_inteiro, assinatura=excluded.assinatura,
                 atualizado_em=excluded.atualizado_em""",
            (it.chave, agenda, uid, it.titulo, it.inicio, it.fim, int(it.dia_inteiro),
             it.assinatura(), it.origem, agora_iso(), agora_iso()),
        )
        con.commit()
        registrar_escrita(con, "calendar.app", f"agenda_{estado}", alvo=it.chave,
                          resumo=it.titulo, payload=payload, resposta={"uid": uid})

    for a in r.apagar:
        try:
            saida = _osascript(_AS_APAGAR, a["agenda"], a["uid"] or "", MARCA + a["chave"])
        except Exception as e:
            registrar_escrita(con, "calendar.app", "agenda_apagar", alvo=a["chave"],
                              resumo=a["titulo"], payload=a, erro=str(e))
            r.erros.append(f"{a['titulo']}: {e}")
            continue
        con.execute("DELETE FROM agenda_itens WHERE chave = ?", (a["chave"],))
        con.commit()
        registrar_escrita(con, "calendar.app", "agenda_apagar", alvo=a["chave"],
                          resumo=a["titulo"], payload=a, resposta={"calendar": saida})
    return r


# --------------------------------------------------------------------------
# O que vai para a agenda
# --------------------------------------------------------------------------


def itens_de_prazos(con: sqlite3.Connection, dias: int = 90) -> list[Item]:
    """Eventos do banco que viram compromisso: só os confirmados (invariante 2)."""
    agora = int(time.time())
    linhas = con.execute(
        """SELECT e.*, c.shortname AS curso
             FROM eventos e
        LEFT JOIN cursos c ON c.site = e.site AND c.courseid = e.courseid
            WHERE e.cancelado = 0 AND e.confirmado = 1
              AND e.data_inicio BETWEEN ? AND ?
         ORDER BY e.data_inicio""",
        (agora, agora + dias * 86400),
    ).fetchall()
    itens = []
    for e in linhas:
        ini = int(e["data_inicio"])
        # "SEGUNDA AVALIAÇÃO" sozinho, no celular, não diz de que matéria é.
        m = re.search(r"_(ECN\d+)_", e["curso"] or "")
        titulo = f"[{m.group(1)}] {e['titulo']}" if m else e["titulo"]
        d = datetime.fromtimestamp(ini)
        notas = [f"Fênix · evento #{e['id']} · origem: {e['origem']}"]
        if e["trecho_origem"]:
            notas.append(f"trecho: {e['trecho_origem']}")
        if d.hour == 0 and d.minute == 0:
            # 00:00 é "hora não informada", não meia-noite: vira dia inteiro
            # em vez de um compromisso de madrugada.
            it = Item(f"evento:{e['site']}:{e['id']}", titulo, ini, ini + 86400,
                      dia_inteiro=True, origem="prazo")
        elif e["tipo"] == "prova":
            it = Item(f"evento:{e['site']}:{e['id']}", titulo, ini,
                      ini + DURACAO_PROVA, origem="prazo")
        else:
            it = Item(f"evento:{e['site']}:{e['id']}", f"Prazo: {titulo}",
                      ini - DURACAO_PRAZO, ini, origem="prazo")
        it.notas = "\n".join(notas)
        itens.append(it)
    return itens


def sincronizar_prazos(
    con: sqlite3.Connection, dias: int = 90, confirmar: bool = False
) -> Resultado:
    """Provas e prazos confirmados → agenda. Cancelado ou desconfirmado sai."""
    itens = itens_de_prazos(con, dias)
    vivas = {it.chave for it in itens}
    agora = int(time.time())
    # Só apaga o que ainda vai acontecer: prova passada fica no histórico.
    sair = [
        r["chave"] for r in con.execute(
            "SELECT chave FROM agenda_itens WHERE origem = 'prazo' AND fim > ?",
            (agora,),
        )
        if r["chave"] not in vivas
    ]
    return aplicar(con, itens, apagar=sair, confirmar=confirmar)


def _instante(texto: str) -> int:
    for f in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M"):
        try:
            return int(datetime.strptime(texto, f).timestamp())
        except ValueError:
            continue
    raise ValueError(f"data não reconhecida: {texto!r} (use 2026-09-22T14:00)")


def itens_de_estudo(blocos: list[dict]) -> list[Item]:
    """Blocos {titulo, inicio, fim | minutos, notas?} → itens.

    A chave sai de título + início: remarcar o mesmo bloco para outro horário
    é um bloco novo, e o antigo se desmarca com `desmarcar`.
    """
    itens = []
    for b in blocos:
        ini = _instante(b["inicio"])
        fim = _instante(b["fim"]) if b.get("fim") else ini + int(b.get("minutos", 60)) * 60
        if fim <= ini:
            raise ValueError(f"bloco '{b.get('titulo')}' termina antes de começar")
        titulo = b["titulo"]
        chave = "estudo:" + hashlib.sha256(f"{titulo}|{ini}".encode()).hexdigest()[:12]
        notas = (b.get("notas") or "").strip()
        itens.append(Item(chave, titulo, ini, fim,
                          notas=(notas + "\n" if notas else "") + "Fênix · bloco de estudo"))
    return itens


def marcados(con: sqlite3.Connection, futuros: bool = True) -> list[sqlite3.Row]:
    sql = "SELECT * FROM agenda_itens"
    if futuros:
        sql += f" WHERE fim > {int(time.time())}"
    return con.execute(sql + " ORDER BY inicio").fetchall()

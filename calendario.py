"""Exporta os eventos para .ics, o formato que todo calendário lê.

Por que existe: nove dos dezenove eventos do banco foram confirmados na mão,
um a um, e depois copiados na mão para o calendário do sistema. O trabalho de
conferir a data é o que tem valor; o de redigitar, não.

**Só evento confirmado sai por padrão.** É a mesma disciplina da invariante 2:
data que o Moodle deu sozinho, ou que saiu por regex de uma tabela em PDF,
ainda não é compromisso. Exportar tudo colocaria no seu calendário uma prova
que talvez não exista naquele dia, e um calendário em que não se confia é pior
que calendário nenhum. `--todos` inclui os não confirmados, e aí cada um sai
marcado no próprio título — se vai para o calendário, vai dizendo o que é.

O arquivo é texto puro e não depende de biblioteca: a RFC 5545 é simples para
o subconjunto que interessa (VEVENT com data, título e descrição).
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

# Quanto dura um evento que não declara duração. Uma prova de 2h é o caso
# comum; para entrega o que importa é o horário-limite, e 2h de bloco não
# atrapalha.
DURACAO_PADRAO = 2 * 60 * 60

PRODID = "-//Projeto Fenix//UFMG Moodle Companion//PT"


def _escapar(texto: str) -> str:
    """RFC 5545: vírgula, ponto e vírgula e barra invertida são separadores.

    Sem isto, um título com vírgula ("Prova 1, unidade 3") quebra a linha em
    dois campos e o calendário recusa o arquivo inteiro.
    """
    return (
        (texto or "")
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def _dobrar(linha: str) -> str:
    """Linha de .ics tem limite de 75 octetos; o resto continua indentado.

    Título de evento do Moodle passa disso com facilidade ("1a Atividade
    Prática: criando Mapas Temáticos no QGIS está marcado(a) para esta data").
    """
    if len(linha.encode("utf-8")) <= 75:
        return linha
    # A quebra é por CARACTERE, contando octetos: cortar no meio de um "ã"
    # produziria byte inválido, e o `errors="ignore"` que consertasse isso
    # comeria a letra em silêncio. A continuação começa com um espaço, que
    # também conta no limite — daí 74 a partir da segunda linha.
    partes: list[str] = []
    atual, tamanho = "", 0
    for ch in linha:
        n = len(ch.encode("utf-8"))
        teto = 75 if not partes else 74
        if tamanho + n > teto:
            partes.append(atual)
            atual, tamanho = "", 0
        atual += ch
        tamanho += n
    if atual:
        partes.append(atual)
    return "\r\n ".join(partes)


def _utc(epoch: int) -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(epoch))


@dataclass
class Resultado:
    caminho: Path | None
    total: int
    confirmados: int
    pulados: int

    def __str__(self) -> str:
        if not self.total:
            return "Nenhum evento para exportar."
        extra = f", {self.total - self.confirmados} não confirmado(s)" if self.total > self.confirmados else ""
        pulo = f" ({self.pulados} sem data, fora)" if self.pulados else ""
        return f"✓ {self.total} evento(s){extra}{pulo} → {self.caminho}"


def eventos_para_exportar(
    con: sqlite3.Connection,
    site: str | None = None,
    courseid: int | None = None,
    todos: bool = False,
) -> list[sqlite3.Row]:
    sql = """SELECT e.*, c.fullname AS curso
               FROM eventos e
          LEFT JOIN cursos c ON c.site = e.site AND c.courseid = e.courseid
              WHERE e.cancelado = 0"""
    params: list = []
    if not todos:
        sql += " AND e.confirmado = 1"
    if site:
        sql += " AND e.site = ?"
        params.append(site)
    if courseid:
        sql += " AND e.courseid = ?"
        params.append(courseid)
    return con.execute(sql + " ORDER BY e.data_inicio", params).fetchall()


def montar(eventos: list[sqlite3.Row]) -> tuple[str, int, int]:
    """Devolve (texto do .ics, quantos entraram, quantos ficaram de fora)."""
    L = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:UFMG",
    ]
    entraram = pulados = 0
    agora = _utc(int(time.time()))
    for e in eventos:
        if not e["data_inicio"]:
            # Evento sem data não é compromisso; exportar como "hoje" seria
            # inventar.
            pulados += 1
            continue
        entraram += 1
        inicio = int(e["data_inicio"])
        titulo = e["titulo"] or "Evento"
        if not e["confirmado"]:
            # Vai marcado no título, não só na descrição: no celular só o
            # título aparece.
            titulo = f"[não confirmado] {titulo}"
        descricao = []
        if e["curso"]:
            descricao.append(e["curso"])
        descricao.append(f"origem: {e['origem']}")
        if e["trecho_origem"]:
            # A invariante 7 vale aqui também: a data vai com a linha de onde
            # foi lida, para você conferir sem abrir o PDF.
            descricao.append(f"trecho: {e['trecho_origem']}")
        if e["origem_ref"]:
            descricao.append(f"fonte: {e['origem_ref']}")
        if not e["confirmado"]:
            descricao.append(
                "NÃO CONFIRMADO — data automática, confira antes de contar com ela"
            )
        L += [
            "BEGIN:VEVENT",
            _dobrar(f"UID:fenix-{e['site']}-{e['id']}@ufmg"),
            f"DTSTAMP:{agora}",
            f"DTSTART:{_utc(inicio)}",
            f"DTEND:{_utc(inicio + DURACAO_PADRAO)}",
            _dobrar(f"SUMMARY:{_escapar(titulo)}"),
            _dobrar(f"DESCRIPTION:{_escapar(chr(10).join(descricao))}"),
            f"CATEGORIES:{_escapar((e['tipo'] or 'outro').upper())}",
            # Evento confirmado é compromisso; o automático entra como
            # tentativo, e o calendário mostra isso.
            f"STATUS:{'CONFIRMED' if e['confirmado'] else 'TENTATIVE'}",
            "END:VEVENT",
        ]
    L.append("END:VCALENDAR")
    return "\r\n".join(L) + "\r\n", entraram, pulados


def exportar(
    con: sqlite3.Connection,
    destino: Path | str,
    site: str | None = None,
    courseid: int | None = None,
    todos: bool = False,
) -> Resultado:
    eventos = eventos_para_exportar(con, site, courseid, todos)
    texto, entraram, pulados = montar(eventos)
    p = Path(destino).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(texto, encoding="utf-8")
    return Resultado(
        caminho=p,
        total=entraram,
        confirmados=sum(1 for e in eventos if e["confirmado"] and e["data_inicio"]),
        pulados=pulados,
    )

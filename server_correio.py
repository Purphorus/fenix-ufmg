"""Servidor MCP separado, só para e-mail.

Por que separado do `server.py`: o esquema de toda ferramenta registrada entra
no contexto **a cada mensagem**, e as três de e-mail somam ~440 tokens. Numa
conversa sobre matéria, que é a maioria, isso é peso morto.

Como não existe ligar/desligar servidor MCP, a separação é o próprio
mecanismo: enquanto não registrado, custa zero.

    claude mcp add --scope user ufmg-correio -- \\
        "<projeto>/.venv/bin/python" "<projeto>/server_correio.py"

    claude mcp remove ufmg-correio      # desliga

A disciplina é a mesma do `server.py`: ensaio por padrão (aqui, rascunho de
verdade no Mail.app), destinatário não confirmado é recusado, e toda tentativa
— inclusive falha — vai para `log_escrita`, no mesmo banco.
"""

from __future__ import annotations

try:  # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server

import apostila
import correio
import db

mcp = _Server("ufmg-correio")

_schema_pronto = False


def _con():
    """Mesmo banco do server.py — o log de escrita é um só."""
    global _schema_pronto
    con = db.conectar()
    if not _schema_pronto:
        db.init_db(con)
        _schema_pronto = True
    return con


@mcp.tool()
def enviar_email(
    para: list[str], assunto: str, corpo: str,
    de: str = "", anexos: list[str] = [],
    confirmar: bool = False, aceitar_novo: bool = False,
) -> str:
    """Escreve e-mail. Sem confirmar: rascunho no Mail.app. Com: envia.

    `de` só é preciso com várias contas. `anexos` são caminhos de arquivo.
    Destinatário não confirmado é recusado — confirme com o usuário e repita
    com aceitar_novo. confirmar=True só se ele mandou enviar nesta mensagem.
    """
    con = _con()
    try:
        return str(correio.compor(
            con, para, assunto, corpo, de=de or None, anexos=anexos or [],
            confirmar=confirmar, aceitar_novos=aceitar_novo,
        ))
    finally:
        con.close()


@mcp.tool()
def remetentes_email() -> str:
    """Contas que o Mail.app pode usar como remetente."""
    try:
        cs = correio.remetentes()
    except RuntimeError as e:
        return f"Erro: {e}"
    return "\n".join(f"{c['conta']}: {c['endereco']}" for c in cs) or "Nenhuma conta ativa."


@mcp.tool()
def contatos_email(lembrar: str = "", nome: str = "", esquecer: str = "") -> str:
    """Lista destinatários confirmados; `lembrar`/`esquecer` alteram a lista.

    Só confirme um endereço depois que o usuário disser que está certo.
    """
    con = _con()
    try:
        saida = []
        if lembrar:
            saida.append(correio.lembrar_contato(con, lembrar, nome or None))
        if esquecer:
            saida.append(correio.esquecer_contato(con, esquecer))
        linhas = correio.contatos(con)
        saida.append("\n".join(
            f"{r['endereco']} {('(' + r['nome'] + ')') if r['nome'] else ''} — usos: {r['usos']}"
            for r in linhas
        ) or "Nenhum contato confirmado.")
        return "\n".join(saida)
    finally:
        con.close()


@mcp.tool()
def anexar_apostila(diretorio: str, saida: str = "", titulo: str = "Apostila de estudo") -> str:
    """Monta um PDF das partes HTML de um diretório, para anexar ao e-mail.

    Atalho para quem já está neste servidor; o `server.py` tem a mesma coisa.
    """
    r = apostila.construir(diretorio, saida or None, titulo)
    return str(r)


@mcp.prompt(title="Enviar material por e-mail")
def enviar_material(para: str = "", assunto: str = "") -> str:
    """Escreve e manda um material de estudo, com revisão antes."""
    alvo = f" para {para}" if para else ""
    return (
        f"Vamos mandar um material por e-mail{alvo}.\n"
        "1. Se eu não disse o destinatário, pergunte antes de qualquer coisa.\n"
        "2. contatos_email() mostra quem já está confirmado; endereço novo "
        "precisa da minha confirmação explícita.\n"
        "3. Escreva assunto claro e corpo curto dizendo o que vai anexado e "
        "por quê — não copie o material inteiro no corpo se ele está no PDF.\n"
        "4. enviar_email SEM confirmar: abre rascunho no Mail.app para eu ler.\n"
        "5. Só com meu 'pode mandar' explícito, repita com confirmar=True.\n"
        + (f"\nAssunto sugerido: {assunto}" if assunto else "")
    )


if __name__ == "__main__":
    mcp.run()

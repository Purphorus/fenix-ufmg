"""Escrever e enviar e-mail pelo Mail.app do macOS.

O nome do módulo é `correio` e não `email` de propósito: `email` é pacote da
biblioteca padrão, e um arquivo com esse nome na raiz do projeto sombreia o
original — quebrando qualquer coisa que dependa dele, com erro difícil de ler.

Por que Mail.app e não SMTP: ele já tem a conta configurada, então **nenhuma
credencial passa por aqui**. Não há senha, app password nem arquivo de
configuração com segredo. O script conversa com um aplicativo que já está
autenticado.

O ensaio, que no resto do projeto é "mostrar o payload sem enviar", aqui fica
melhor: cria um **rascunho de verdade** no Mail.app e abre na tela. Você lê o
e-mail como ele vai sair, e só então manda enviar.

Destinatário desconhecido exige confirmação. E-mail para o endereço errado não
tem desfazer — post em fórum dá para apagar, mensagem na caixa de outra pessoa
não.
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from db import registrar_escrita

# Rascunho fica em `outgoing message`; enviar é o mesmo objeto com `send`.
_APPLESCRIPT_CONTAS = """
tell application "Mail"
  set linhas to {}
  repeat with c in every account
    try
      if enabled of c then
        -- a lista precisa ser atribuída antes de iterar; iterar a expressão
        -- direto levanta erro de coerção que o `try` engole em silêncio
        set ends to email addresses of c
        repeat with e in ends
          set end of linhas to (name of c) & tab & (e as text)
        end repeat
      end if
    end try
  end repeat
  set AppleScript's text item delimiters to linefeed
  return linhas as text
end tell
"""

# Conteúdo entra por argv, nunca interpolado no script: corpo com aspas ou
# acento quebraria a compilação do AppleScript.
_APPLESCRIPT_COMPOR = """
on run argv
  set remetente to item 1 of argv
  set destinos to item 2 of argv
  set assunto to item 3 of argv
  set corpo to item 4 of argv
  set anexos to item 5 of argv
  set enviar to item 6 of argv

  tell application "Mail"
    -- Janela visível só no rascunho, que existe para ser lido. Enviando
    -- direto (cron), abrir janela é estorvo e pode falhar em sessão sem GUI.
    set mostrar to (enviar is not "sim")
    set msg to make new outgoing message with properties ¬
      {subject:assunto, content:corpo, visible:mostrar}
    tell msg
      if remetente is not "" then set sender to remetente
      repeat with d in (my separar(destinos))
        if d is not "" then
          make new to recipient at end of to recipients with properties {address:d}
        end if
      end repeat
      repeat with a in (my separar(anexos))
        set caminho to a as text
        if caminho is not "" then
          -- a forma importa: `at after the last paragraph of content`.
          -- `tell content to make new attachment ... at after last paragraph`
          -- não levanta erro e não anexa nada — falha silenciosa.
          make new attachment with properties ¬
            {file name:(POSIX file caminho)} at after the last paragraph of content
        end if
      end repeat
    end tell

    -- Conferir em vez de supor: o anexo pode falhar sem erro, e dizer
    -- "1 anexo" sem ter verificado é pior que não dizer nada.
    delay 1
    set grudados to 0
    try
      set grudados to count of (attachments of content of msg)
    end try

    if enviar is "sim" then
      send msg
      return "enviado|" & (grudados as text)
    end if
    return "rascunho|" & (grudados as text)
  end tell
end run

on separar(t)
  set AppleScript's text item delimiters to ","
  set partes to text items of t
  set AppleScript's text item delimiters to ""
  return partes
end separar
"""

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def agora_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def normalizar(endereco: str) -> str:
    return (endereco or "").strip().lower()


def valido(endereco: str) -> bool:
    return bool(_EMAIL.match(normalizar(endereco)))


@dataclass
class Resultado:
    """Devolução de toda operação de e-mail, no molde de `escrita.Resultado`."""

    acao: str
    alvo: str
    ok: bool = False
    ensaio: bool = False
    detalhe: str = ""
    payload: dict = field(default_factory=dict)

    def __str__(self) -> str:
        if self.ensaio:
            return f"[rascunho] {self.acao} para {self.alvo}: {self.detalhe}"
        marca = "✓" if self.ok else "✗"
        return f"{marca} {self.acao} para {self.alvo}: {self.detalhe}"


def _osascript(script: str, *args: str, timeout: int = 60) -> str:
    """Roda AppleScript passando conteúdo por argv."""
    if sys.platform != "darwin":
        raise RuntimeError(
            "o e-mail do Fênix usa o Mail.app e só funciona no macOS")
    r = subprocess.run(
        ["osascript", "-", *args],
        input=script, capture_output=True, text=True, timeout=timeout,
    )
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "erro no AppleScript").strip())
    return r.stdout.strip()


# --------------------------------------------------------------------------
# Remetentes (contas do Mail.app)
# --------------------------------------------------------------------------


def remetentes() -> list[dict]:
    """Endereços que o Mail.app pode usar como remetente."""
    try:
        saida = _osascript(_APPLESCRIPT_CONTAS)
    except Exception as e:
        raise RuntimeError(
            f"não consegui falar com o Mail.app: {e}. "
            "Ele precisa estar instalado e com pelo menos uma conta ativa."
        )
    contas = []
    for linha in saida.splitlines():
        if "\t" in linha:
            nome, endereco = linha.split("\t", 1)
            contas.append({"conta": nome.strip(), "endereco": endereco.strip()})
    return contas


def resolver_remetente(pedido: str | None) -> str:
    """Escolhe o remetente: o pedido, se existir; senão a única conta.

    Com mais de uma conta e nenhum pedido, exige escolha em vez de adivinhar —
    mandar do endereço errado é o tipo de erro que só se descobre depois.
    """
    contas = remetentes()
    if not contas:
        raise RuntimeError("nenhuma conta ativa no Mail.app")

    if pedido:
        alvo = normalizar(pedido)
        for c in contas:
            if normalizar(c["endereco"]) == alvo or normalizar(c["conta"]) == alvo:
                return c["endereco"]
        disponiveis = ", ".join(c["endereco"] for c in contas)
        raise RuntimeError(f"remetente '{pedido}' não configurado. Há: {disponiveis}")

    if len(contas) == 1:
        return contas[0]["endereco"]
    disponiveis = ", ".join(c["endereco"] for c in contas)
    raise RuntimeError(
        f"há {len(contas)} contas — diga qual usar com --de. Disponíveis: {disponiveis}"
    )


# --------------------------------------------------------------------------
# Contatos conhecidos
# --------------------------------------------------------------------------


def conhecido(con: sqlite3.Connection, endereco: str) -> bool:
    r = con.execute(
        "SELECT confirmado FROM memoria_contato WHERE endereco = ?",
        (normalizar(endereco),),
    ).fetchone()
    return bool(r and r["confirmado"])


def lembrar_contato(
    con: sqlite3.Connection, endereco: str, nome: str | None = None
) -> str:
    e = normalizar(endereco)
    if not valido(e):
        return f"'{endereco}' não parece um endereço de e-mail."
    con.execute(
        """INSERT INTO memoria_contato (endereco, nome, confirmado, criado_em, usos)
           VALUES (?,?,1,?,0)
           ON CONFLICT(endereco) DO UPDATE SET
             nome = COALESCE(excluded.nome, memoria_contato.nome),
             confirmado = 1""",
        (e, nome, agora_iso()),
    )
    con.commit()
    return f"Contato confirmado: {e}" + (f" ({nome})" if nome else "")


def esquecer_contato(con: sqlite3.Connection, endereco: str) -> str:
    cur = con.execute(
        "DELETE FROM memoria_contato WHERE endereco = ?", (normalizar(endereco),)
    )
    con.commit()
    return (
        f"Esqueci {normalizar(endereco)}." if cur.rowcount
        else f"{endereco} não estava na lista."
    )


def contatos(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT * FROM memoria_contato ORDER BY usos DESC, endereco"
    ).fetchall()


def _marcar_uso(con: sqlite3.Connection, enderecos: list[str]) -> None:
    for e in enderecos:
        con.execute(
            """UPDATE memoria_contato SET usos = usos + 1, ultimo_uso = ?
               WHERE endereco = ?""",
            (agora_iso(), normalizar(e)),
        )
    con.commit()


# --------------------------------------------------------------------------
# Compor e enviar
# --------------------------------------------------------------------------


def compor(
    con: sqlite3.Connection,
    para: list[str] | str,
    assunto: str,
    corpo: str,
    *,
    de: str | None = None,
    anexos: list[str] | None = None,
    confirmar: bool = False,
    aceitar_novos: bool = False,
) -> Resultado:
    """Cria o e-mail. Sem `confirmar`, para no rascunho aberto no Mail.app.

    `aceitar_novos` libera endereço que não está na lista de contatos — é a
    confirmação explícita que o destinatário novo exige, e ele fica conhecido
    depois.
    """
    destinos = [normalizar(x) for x in ([para] if isinstance(para, str) else para)]
    destinos = [d for d in destinos if d]
    anexos = [str(Path(a).expanduser().resolve()) for a in (anexos or [])]
    alvo = ", ".join(destinos)

    invalidos = [d for d in destinos if not valido(d)]
    if not destinos or invalidos:
        return Resultado(
            acao="email", alvo=alvo,
            detalhe=f"endereço inválido: {invalidos or 'nenhum destinatário'}",
        )

    faltando = [a for a in anexos if not Path(a).is_file()]
    if faltando:
        return Resultado(acao="email", alvo=alvo, detalhe=f"anexo não existe: {faltando}")

    novos = [d for d in destinos if not conhecido(con, d)]
    if novos and not aceitar_novos:
        return Resultado(
            acao="email", alvo=alvo,
            detalhe=(
                f"destinatário novo: {', '.join(novos)}. "
                "E-mail enviado não tem desfazer — confirme o endereço e repita "
                "com aceitar_novos (CLI: --aceitar-novo)."
            ),
        )

    try:
        remetente = resolver_remetente(de)
    except RuntimeError as e:
        return Resultado(acao="email", alvo=alvo, detalhe=str(e))

    payload = {
        "de": remetente, "para": destinos, "assunto": assunto,
        "corpo_chars": len(corpo or ""), "anexos": anexos,
    }
    acao = "email_enviar" if confirmar else "email_rascunho"
    resumo = (
        f"{'enviar' if confirmar else 'rascunho'} “{assunto}” "
        f"de {remetente} ({len(corpo or '')} chars"
        + (f", {len(anexos)} anexo(s)" if anexos else "") + ")"
    )

    try:
        saida = _osascript(
            _APPLESCRIPT_COMPOR,
            remetente, ",".join(destinos), assunto or "", corpo or "",
            ",".join(anexos), "sim" if confirmar else "nao",
        )
    except Exception as e:
        registrar_escrita(
            con, "mail.app", acao, alvo=alvo, resumo=resumo,
            payload=payload, erro=str(e),
        )
        return Resultado(acao=acao, alvo=alvo, detalhe=f"falhou: {e}", payload=payload)

    # O AppleScript devolve "rascunho|N" / "enviado|N" com N = anexos que
    # realmente grudaram.
    estado, _, contagem = saida.partition("|")
    grudados = int(contagem) if contagem.isdigit() else 0
    aviso = ""
    if anexos and grudados != len(anexos):
        aviso = (
            f" ATENÇÃO: {len(anexos)} anexo(s) pedido(s), {grudados} anexado(s)"
        )

    payload["anexos_confirmados"] = grudados
    registrar_escrita(
        con, "mail.app", acao, alvo=alvo, resumo=resumo + aviso,
        payload=payload, resposta={"mail_app": saida},
    )
    for d in novos:
        lembrar_contato(con, d)
    _marcar_uso(con, destinos)

    if confirmar:
        return Resultado(
            acao=acao, alvo=alvo, ok=not aviso,
            detalhe=f"enviado de {remetente}"
            + (f" com {grudados} anexo(s)" if anexos else "") + aviso,
            payload=payload,
        )
    return Resultado(
        acao=acao, alvo=alvo, ensaio=True,
        detalhe=(
            f"aberto no Mail.app, de {remetente}"
            + (f", {grudados} anexo(s)" if anexos else "")
            + "." + aviso + " Revise e envie por lá, ou repita com --confirmar."
        ),
        payload=payload,
    )

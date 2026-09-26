"""Instala e remove o agendamento do sync (e do resumo por e-mail).

Usa **launchd**, não cron. No macOS o cron é legado: roda, mas fica fora do
contexto de sessão do usuário, o que dá dois problemas — pedir permissão de
Automação (TCC) para falar com o Mail.app, e não rodar quando a máquina
acordou depois da hora marcada. O launchd resolve os dois: `RunAtLoad` e
`StartInterval` recuperam execução perdida, e o agente vive na sessão.

    python cli.py agendar instalar          # sync de hora em hora
    python cli.py agendar instalar --email voce@gmail.com
    python cli.py agendar status
    python cli.py agendar remover

Os logs ficam em `~/.ufmg-moodle-mcp/logs/`.
"""

from __future__ import annotations

import plistlib
import subprocess
from dataclasses import dataclass
from pathlib import Path

from db import BASE_DIR

AGENTES = Path.home() / "Library" / "LaunchAgents"
LOGS = BASE_DIR / "logs"

ROTULO_SYNC = "br.ufmg.companion.sync"
ROTULO_EMAIL = "br.ufmg.companion.briefing"
ROTULO_AVALIACAO = "br.ufmg.companion.avaliacao"

PROJETO = Path(__file__).resolve().parent
PYTHON = PROJETO / ".venv" / "bin" / "python"


@dataclass
class Agente:
    rotulo: str
    plist: Path
    carregado: bool
    descricao: str

    def __str__(self) -> str:
        return f"{'✓' if self.carregado else '·'} {self.rotulo} — {self.descricao}"


def _plist(rotulo: str) -> Path:
    return AGENTES / f"{rotulo}.plist"


def _carregados() -> set[str]:
    r = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
    return {
        linha.split("\t")[-1]
        for linha in r.stdout.splitlines()
        if "ufmg.companion" in linha
    }


def _escrever(rotulo: str, argumentos: list[str], intervalo: int | None = None,
              hora: tuple[int, int] | None = None) -> Path:
    """Grava o plist. `intervalo` em segundos, ou `hora` = (hora, minuto) semanal."""
    LOGS.mkdir(parents=True, exist_ok=True)
    conf: dict = {
        "Label": rotulo,
        "ProgramArguments": [str(PYTHON), *argumentos],
        "WorkingDirectory": str(PROJETO),
        "StandardOutPath": str(LOGS / f"{rotulo}.log"),
        "StandardErrorPath": str(LOGS / f"{rotulo}.err"),
        # Sem isto, o agente não recupera a execução perdida enquanto a
        # máquina estava dormindo — que é metade das vezes, num laptop.
        "RunAtLoad": True,
    }
    if intervalo:
        conf["StartInterval"] = intervalo
    if hora:
        conf["StartCalendarInterval"] = {
            "Weekday": 1, "Hour": hora[0], "Minute": hora[1]
        }
    destino = _plist(rotulo)
    destino.parent.mkdir(parents=True, exist_ok=True)
    with destino.open("wb") as f:
        plistlib.dump(conf, f)
    return destino


def _carregar(rotulo: str) -> str:
    caminho = _plist(rotulo)
    subprocess.run(["launchctl", "unload", str(caminho)],
                   capture_output=True, text=True)
    r = subprocess.run(["launchctl", "load", str(caminho)],
                       capture_output=True, text=True)
    return (r.stderr or r.stdout).strip()


def instalar(intervalo_min: int = 60, email: str = "") -> list[str]:
    """Agenda o sync; com `email`, agenda também o resumo de segunda-feira."""
    saida = []

    _escrever(
        ROTULO_SYNC,
        [str(PROJETO / "agendar.py"), "--rodar-sync"],
        intervalo=intervalo_min * 60,
    )
    erro = _carregar(ROTULO_SYNC)
    saida.append(
        f"sync a cada {intervalo_min} min" + (f" — {erro}" if erro else " ✓")
    )

    if email:
        _escrever(
            ROTULO_EMAIL,
            [str(PROJETO / "cli.py"), "email", "briefing",
             "--para", email, "--confirmar"],
            hora=(7, 30),
        )
        erro = _carregar(ROTULO_EMAIL)
        saida.append(
            f"resumo para {email}, segunda 07:30" + (f" — {erro}" if erro else " ✓")
        )
    return saida


def instalar_avaliacao(email: str, hora: int = 20) -> str:
    """Agenda a avaliação diária da busca, com relatório por e-mail.

    Roda no fim do dia porque leva uns 100 segundos com o reranker ligado e
    compara sempre com a rodada anterior. `--confirmar` manda de verdade: sem
    ele o agente deixaria um rascunho por dia empilhado no Mail.app, o que é
    pior que não avisar.
    """
    _escrever(
        ROTULO_AVALIACAO,
        [str(PROJETO / "cli.py"), "avaliar", "--email", email, "--confirmar"],
        hora=(hora, 0),
    )
    erro = _carregar(ROTULO_AVALIACAO)
    return f"avaliação diária às {hora}:00 para {email}" + (f" — {erro}" if erro else " ✓")


def remover() -> list[str]:
    saida = []
    for rotulo in (ROTULO_SYNC, ROTULO_EMAIL, ROTULO_AVALIACAO):
        caminho = _plist(rotulo)
        if not caminho.is_file():
            continue
        subprocess.run(["launchctl", "unload", str(caminho)],
                       capture_output=True, text=True)
        caminho.unlink()
        saida.append(f"removido: {rotulo}")
    return saida or ["Nada agendado."]


def status() -> list[Agente]:
    ativos = _carregados()
    saida = []
    for rotulo, desc in ((ROTULO_SYNC, "sync + extrair"),
                         (ROTULO_AVALIACAO, "avaliação da busca"),
                         (ROTULO_EMAIL, "resumo por e-mail")):
        caminho = _plist(rotulo)
        if caminho.is_file():
            saida.append(Agente(rotulo, caminho, rotulo in ativos, desc))
    return saida


def rodar_sync() -> int:
    """O que o agente executa: sync, extrair e programa, em sequência.

    Concentrado aqui para o plist ter um comando só — encadear com `&&` num
    ProgramArguments não funciona, porque launchd não passa por shell.
    """
    import time

    import db
    import extract
    import programa
    from moodle_client import MoodleError
    from sync import sync_site

    print(f"=== {time.strftime('%Y-%m-%d %H:%M:%S')} ===", flush=True)
    con = db.conectar()
    db.init_db(con)
    try:
        res = sync_site(con)
        print(f"[{res.site}] {res.cursos} turmas, "
              f"{res.arquivos_novos} arquivos novos, "
              f"{res.eventos_novos} eventos novos", flush=True)
        for e in res.erros:
            print(f"  ! {e}", flush=True)
        ext = extract.extrair_pendentes(con)
        print(f"extraídos: {ext.extraidos}, OCR: {ext.ocr}, erros: {ext.erros}",
              flush=True)
        for r in programa.extrair_todos(con, res.site):
            if r.criados:
                print(f"programa {r.nome_arquivo}: {r.criados} evento(s) novo(s)",
                      flush=True)
        return 0
    except MoodleError as e:
        # Token expirado é o caso comum. Falhar com mensagem legível no log é
        # melhor que traceback: você vai ler isso semanas depois.
        print(f"ERRO: {e}", flush=True)
        return 1
    finally:
        con.close()


if __name__ == "__main__":
    import sys

    if "--rodar-sync" in sys.argv:
        sys.exit(rodar_sync())
    print(__doc__)

"""Monta apostila de estudo em PDF a partir de partes HTML numeradas.

O formato que funciona para este usuário, e por quê: explicação detalhada por
tópico, **pelo menos cinco exercícios por matéria em dificuldade crescente**, e
as **resoluções comentadas reunidas no fim** — não junto de cada exercício,
porque ele resolve antes de ler a resposta e gabarito intercalado estraga isso.
Toda afirmação cita onde está no slide, para conferência contra a fonte.

Por que HTML e não LaTeX ou Markdown: a máquina não tem pandoc, weasyprint nem
LaTeX, mas tem o chromium do playwright, que imprime PDF com CSS de impressão
(`@page`, quebras controladas, cabeçalho e rodapé). Escreva a matemática com
`<sup>`/`<sub>` e grego em unicode — MathJax depende de rede e falha em silêncio
na impressão.

As partes são arquivos `NN_nome.html` numerados; a ordem alfabética é a ordem do
documento. `00_head.html` abre o HTML e carrega o estilo; o fechamento
(`</body></html>`) é acrescentado na montagem.

    python cli.py apostila --partes ~/.ufmg-moodle-mcp/apostilas/macro-iii-prova-1
"""

from __future__ import annotations

import asyncio
import re
from html import unescape
from dataclasses import dataclass, field
from pathlib import Path

# Diretório com o modelo de estilo versionado no repositório.
MODELO = Path(__file__).parent / "apostila_modelo"

RODAPE = (
    '<div style="width:100%;font-size:8pt;color:#5a6472;'
    'font-family:Georgia,serif;padding:0 16mm;display:flex;'
    'justify-content:space-between">'
    "<span>{titulo}</span>"
    '<span>página <span class="pageNumber"></span> de '
    '<span class="totalPages"></span></span>'
    "</div>"
)


@dataclass
class Resultado:
    partes: list[str]
    html: Path | None = None
    pdf: Path | None = None
    erro: str = ""
    citacoes: list[tuple[str, int]] = field(default_factory=list)

    def __str__(self) -> str:
        if self.erro:
            return f"✗ {self.erro}"
        onde = f"→ {self.pdf}" if self.pdf else f"HTML montado: {self.html}"
        cit = f", {len(self.citacoes)} citação(ões)" if self.citacoes else ""
        return f"✓ {len(self.partes)} parte(s){cit} {onde}"


# --------------------------------------------------------------------------
# Citações: o que a apostila diz que usou
# --------------------------------------------------------------------------
#
# A apostila é obrigada a citar a fonte de cada afirmação, e o estilo do
# modelo marca a citação com `<span class="slide">Cap 06, p. 9</span>`. Isso
# faz do próprio documento a fonte de verdade sobre o material que entrou
# nele: em vez de pedir ao modelo que anote o que usou (ele esquece, e custa
# token), a montagem lê de volta o que ficou escrito.
#
# Este módulo só extrai o par (rótulo citado, página). Resolver o rótulo para
# um arquivo do banco é de `memoria.resolver_citacoes` — aqui não se importa
# `db`, de propósito.

_CITACAO = re.compile(r'<span class="slide">(.*?)</span>', re.S)

# Um grupo de página dentro da citação: "p. 9", "pp. 51-52", "p.10", "slide 4",
# "página 7", "slides 2 e 5".
#
# Procurar o MARCADOR, e não só o número, é o que separa "p.10, p.32" (duas
# páginas) de "p. 51-52" (um intervalo). Pegar todos os números soltos faria
# da primeira uma faixa de vinte e duas páginas.
_PAGINA = re.compile(
    r"(?:p{1,2}\.|págs?\.?|páginas?|slides?|f\.)\s*"
    r"(\d+(?:\s*(?:[–—-]|,|\be\b)\s*\d+)*)",
    re.I,
)

# Dentro da expressão de páginas: "51-52" é intervalo, "2 e 5" e "10, 32" são
# páginas soltas. Tratar tudo como intervalo transformaria "p.10, p.32" em
# vinte e duas páginas; tratar tudo como lista perderia o miolo de um
# intervalo legítimo.
_SEPARADOR = re.compile(r"\s*(?:,|\be\b)\s*", re.I)
_INTERVALO = re.compile(r"^(\d+)\s*[–—-]\s*(\d+)$")


def _paginas(expressao: str) -> list[int]:
    saida: list[int] = []
    for termo in _SEPARADOR.split(expressao):
        termo = termo.strip()
        if not termo:
            continue
        m = _INTERVALO.match(termo)
        if m:
            comeco, fim_ = int(m.group(1)), int(m.group(2))
            # Um "intervalo" de duzentas páginas é erro de leitura, não
            # citação: fica só a primeira.
            if fim_ >= comeco and fim_ - comeco <= 40:
                saida.extend(range(comeco, fim_ + 1))
            else:
                saida.append(comeco)
        elif termo.isdigit():
            saida.append(int(termo))
    return saida


def citacoes(html: str) -> list[tuple[str, int]]:
    """Pares (rótulo citado, página) que aparecem no HTML, na ordem do texto.

    O rótulo é o que vem antes do primeiro marcador de página: "Cap 07 rev,
    p. 26" cita o arquivo "Cap 07 rev" na página 26. A forma entre colchetes,
    "[Endógeno 1, p.11]", é a mesma coisa e também é aceita — as duas estão em
    uso nas apostilas já montadas.

    Intervalo ("p. 51–52") vira uma entrada por página, porque a cobertura é
    por trecho e cada página é um trecho.

    Citação sem número de página é descartada ("[Ellery (2011), Tabela 1]"):
    sem página não dá para dizer QUAL trecho entrou, e registrar o arquivo
    inteiro mentiria na contagem.
    """
    achadas: list[tuple[str, int]] = []
    for bruto in _CITACAO.findall(html):
        # `&ndash;` num intervalo de páginas e `&nbsp;` antes do número são
        # comuns no que o modelo escreve; sem desescapar, "p.6&ndash;7" perde
        # a página 7.
        texto = re.sub(r"\s+", " ", unescape(bruto)).strip().strip("[]").strip()
        grupos = list(_PAGINA.finditer(texto))
        if not grupos:
            continue
        rotulo = texto[: grupos[0].start()].strip().rstrip(",").strip()
        if not rotulo:
            continue
        for g in grupos:
            for pg in _paginas(g.group(1)):
                achadas.append((rotulo, pg))
    return achadas


def listar_partes(dir_partes: Path) -> list[Path]:
    """Partes na ordem do documento: `NN_nome.html`, ordenadas pelo número.

    Arquivos sem prefixo numérico são ignorados — é o que permite deixar
    rascunho e HTML montado no mesmo diretório sem entrar no PDF.
    """
    return sorted(
        p for p in dir_partes.glob("*.html")
        if p.name[:2].isdigit()
    )


def montar(dir_partes: Path, saida_html: Path | None = None) -> Path:
    """Concatena as partes num HTML só e devolve o caminho."""
    partes = listar_partes(dir_partes)
    if not partes:
        raise FileNotFoundError(
            f"nenhuma parte NN_*.html em {dir_partes}. "
            f"Comece copiando o modelo de {MODELO}/00_head.html"
        )
    destino = saida_html or (dir_partes / "apostila.html")
    html = "\n".join(p.read_text(encoding="utf-8") for p in partes)
    html += "\n</body>\n</html>\n"
    destino.write_text(html, encoding="utf-8")
    return destino


async def _imprimir(caminho_html: Path, saida_pdf: Path, titulo: str) -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        nav = await pw.chromium.launch()
        pag = await nav.new_page()
        # `wait_until="load"` e não "networkidle": o HTML é local e não busca
        # nada na rede; esperar por rede ociosa só adiciona timeout.
        await pag.goto(f"file://{caminho_html}", wait_until="load")
        await pag.pdf(
            path=str(saida_pdf),
            format="A4",
            print_background=True,
            margin={"top": "18mm", "bottom": "20mm", "left": "16mm", "right": "16mm"},
            display_header_footer=True,
            header_template="<div></div>",
            footer_template=RODAPE.format(titulo=titulo),
        )
        await nav.close()


def construir(
    dir_partes: Path | str,
    saida_pdf: Path | str | None = None,
    titulo: str = "Apostila de estudo",
) -> Resultado:
    """Monta o HTML e imprime o PDF. Nunca levanta: erro vem no resultado."""
    dir_partes = Path(dir_partes).expanduser()
    res = Resultado(partes=[])
    try:
        partes = listar_partes(dir_partes)
        res.partes = [p.name for p in partes]
        res.html = montar(dir_partes)
        # Lido do HTML montado, não das partes: é o documento que existe de
        # fato, e é dele que a cobertura tem de sair.
        res.citacoes = citacoes(res.html.read_text(encoding="utf-8"))
        destino = Path(saida_pdf).expanduser() if saida_pdf else dir_partes / "apostila.pdf"
        destino.parent.mkdir(parents=True, exist_ok=True)
        asyncio.run(_imprimir(res.html, destino, titulo))
        res.pdf = destino
    except ImportError:
        res.erro = "playwright não instalado: pip install playwright && python -m playwright install chromium"
    except Exception as e:
        res.erro = f"{type(e).__name__}: {e}"
    return res


def novo_projeto(destino: Path | str, titulo: str = "Apostila") -> Path:
    """Cria um diretório de apostila com o cabeçalho de estilo pronto."""
    destino = Path(destino).expanduser()
    destino.mkdir(parents=True, exist_ok=True)
    cabecalho = MODELO / "00_head.html"
    alvo = destino / "00_head.html"
    if cabecalho.is_file() and not alvo.is_file():
        alvo.write_text(
            cabecalho.read_text(encoding="utf-8").replace(
                "Apostila Macro III — Prova 1", titulo
            ),
            encoding="utf-8",
        )
    return destino

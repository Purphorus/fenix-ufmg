"""Embedding local dos trechos — o lado semântico da busca.

Existe para o caso que o léxico não cobre: quem pergunta "choque de oferta"
não acha o slide que escreveu "deslocamento da curva de oferta agregada", e
quem pergunta em português não acha a nota escrita em inglês. BM25 casa
palavra; isto casa assunto.

**É opcional de propósito.** O modelo são ~470MB baixados na primeira vez, e
uma máquina sem ele não pode ficar sem busca: `disponivel()` é falso e
`busca.py` responde só com o lado léxico, que já é melhor que o índice por
arquivo que existia antes. Nada aqui levanta para quem chama.

Roda em CPU pelo onnxruntime (o fastembed não traz torch). Com ~800 trechos, o
corpus inteiro embute em poucos segundos e a consulta é força bruta sobre uma
matriz de 1,2MB — não há índice aproximado aqui porque não faria diferença
nenhuma nessa escala, e cada estrutura a mais é mais uma coisa para ficar
desatualizada.
"""

from __future__ import annotations

import functools
import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class Modelo:
    """Um modelo de embedding e o jeito certo de chamá-lo.

    O jeito de chamar é parte do modelo, não detalhe: o e5 exige "query: " e
    "passage: " na frente do texto e, sem isso, degrada em silêncio; o jina v3
    tem uma cabeça por tarefa e embute consulta e passagem de formas
    diferentes. Deixar isso em quem chama foi o que tornou o e5 arriscado.
    """

    apelido: str
    nome: str
    dim: int
    prefixo_consulta: str = ""
    prefixo_passagem: str = ""
    # Embute em janelas do tamanho máximo do modelo e tira a média. Sem isto o
    # tokenizer corta o resto em silêncio: medido em 24/09/2026, o MiniLM (128
    # tokens) não via 49% do texto do corpus — 72% dos trechos passavam do
    # limite, mediana de 207 tokens.
    janelas: bool = False
    tarefas: bool = False       # query_embed/passage_embed com cabeça própria
    # Limiares ABSOLUTOS de cosseno são do modelo, não da busca: cada modelo
    # tem sua escala (no e5 quase tudo passa de 0,7). Os do MiniLM foram
    # medidos em set/2026; os dos outros saem da mesma posição relativa entre
    # "consulta qualquer" e "o que responde", recalibrados em 24/09/2026.
    piso_cosseno: float = 0.45  # busca.PISO_COSSENO: corte do modo vetorial global
    score_fraco: float = 0.62   # busca.SCORE_FRACO: topo abaixo disto sugere "só menciona"
    # Largura da faixa de cosseno do modelo relativa à do MiniLM (mediana, por
    # consulta, de topo − mediana do curso). Medido em 24/09/2026: MiniLM
    # 0,311, jina 0,346, e5 0,063 — o e5 espreme tudo entre 0,79 e 0,85. Os
    # parâmetros da busca que são DISTÂNCIA de cosseno (margem do corte, bônus
    # da âncora) são multiplicados por isto; sem isso o e5 foi avaliado com a
    # régua do MiniLM: o bônus de 0,05 valia 80% da faixa dele e a margem de
    # 0,10 não cortava nada, e ele "perdeu" com p = 0,03.
    escala: float = 1.0
    # Textos por lote do onnx. O padrão do fastembed é 256: com o e5-large e
    # janelas de 512 tokens isso passou de 10 GB de memória intermediária numa
    # máquina de 8 GB, e o processo ficou 42 minutos paginando sem gravar um
    # vetor (24/09/2026). A memória cresce com lote × comprimento², então o
    # modelo de janela longa leva lote pequeno.
    lote: int = 64


MODELOS = {
    # Multilíngue, sem prefixo, 128 tokens. É o padrão porque o perfilador
    # calibrou limiares absolutos nele (consolidação a 0,85): quem não diz qual
    # modelo usa continua exatamente como estava.
    "minilm": Modelo("minilm",
                     "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
                     384),
    "minilm-janelas": Modelo("minilm-janelas",
                             "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
                             384, janelas=True),
    # Treinado para recuperação, 512 tokens.
    # Limiares na mesma posição relativa entre a mediana do curso e o topo que
    # os do MiniLM ocupam: piso = mediana + 0,305·faixa; fraco = mediana +
    # 0,852·faixa (medianas 0,794 e 0,168; faixas 0,063 e 0,346).
    "e5-large": Modelo("e5-large", "intfloat/multilingual-e5-large", 1024,
                       prefixo_consulta="query: ", prefixo_passagem="passage: ",
                       janelas=True, lote=8,
                       piso_cosseno=0.813, score_fraco=0.848, escala=0.20),
    # 8k tokens: janela nunca é necessária no tamanho de trecho daqui.
    "jina-v3": Modelo("jina-v3", "jinaai/jina-embeddings-v3", 1024, tarefas=True, lote=4,
                      piso_cosseno=0.274, score_fraco=0.463, escala=1.11),
}
PADRAO = "minilm"

# Compatibilidade: quem ainda lê o nome do modelo padrão.
MODELO = MODELOS[PADRAO].nome
DIM = MODELOS[PADRAO].dim


def modelo(con: sqlite3.Connection | None = None) -> Modelo:
    """O modelo que construiu o índice deste banco.

    Mora no banco (`config_busca`), não no código nem no ambiente: o índice e
    a consulta têm de usar o MESMO modelo, e o único lugar que sabe qual
    modelo construiu os vetores é o próprio banco que os guarda. O Fênix e o
    perfilador compartilham este módulo e podem, assim, usar modelos
    diferentes sem que um mexa no outro.
    """
    if con is None:
        return MODELOS[PADRAO]
    try:
        r = con.execute(
            "SELECT valor FROM config_busca WHERE chave = 'embedding'"
        ).fetchone()
    except sqlite3.OperationalError:     # banco anterior à tabela
        return MODELOS[PADRAO]
    return MODELOS.get(r[0], MODELOS[PADRAO]) if r else MODELOS[PADRAO]


def contexto_ligado(con: sqlite3.Connection | None) -> bool:
    if con is None:
        return False
    try:
        r = con.execute(
            "SELECT valor FROM config_busca WHERE chave = 'contexto'"
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    return bool(r) and r[0] == "1"


def _versao_fastembed() -> str:
    try:
        from importlib.metadata import version
        v = version("fastembed")
    except Exception:
        v = "?"
    return ".".join(v.split(".")[:2])


def chave_conteudo(con: sqlite3.Connection | None = None) -> str:
    """Identidade do vetor do TEXTO do trecho: modelo + versão do fastembed.

    A versão entra porque o fastembed já trocou o pooling do MiniLM entre
    releases (0.5 usava CLS, 0.8 usa média). Vetor de pooling diferente não é
    comparável, e a falha seria silenciosa: a busca continuaria respondendo,
    só que errado. Com a versão na chave, atualizar a biblioteca invalida os
    vetores sozinho e o próximo `--reindexar` os refaz.

    É este vetor que o grafo usa para achar conteúdo repetido entre arquivos:
    com o contexto do arquivo na frente, dois cabeçalhos iguais em arquivos
    diferentes deixariam de parecer iguais, e o detector de boilerplate cegaria.
    """
    m = modelo(con)
    sufixo = "+janelas" if m.janelas else ""
    return f"{m.nome}@fastembed{_versao_fastembed()}{sufixo}"


def chave(con: sqlite3.Connection | None = None) -> str:
    """Identidade do vetor que a BUSCA compara com a consulta."""
    base = chave_conteudo(con)
    return base + "+ctx1" if contexto_ligado(con) else base


_carregados: dict[str, object] = {}
_indisponivel: dict[str, str] = {}   # motivo por modelo — não tenta de novo a cada chamada


def _carregar(m: Modelo | None = None):
    """Carrega o modelo uma vez. Devolve None se não der, com motivo guardado."""
    m = m or MODELOS[PADRAO]
    if m.nome in _carregados:
        return _carregados[m.nome]
    if m.nome in _indisponivel:
        return None
    try:
        from fastembed import TextEmbedding
    except ImportError:
        _indisponivel[m.nome] = "fastembed não instalado (pip install fastembed)"
        return None
    try:
        # O padrão do fastembed é o diretório temporário do sistema, que o
        # macOS limpa — e aí o modelo é baixado de novo, no meio de um sync
        # agendado, possivelmente sem rede. Fica junto do resto dos dados.
        _carregados[m.nome] = TextEmbedding(model_name=m.nome,
                                            cache_dir=str(_dir_modelos()))
    except Exception as e:  # download interrompido, disco cheio, onnx quebrado
        _indisponivel[m.nome] = f"{type(e).__name__}: {e}"
        return None
    return _carregados[m.nome]


def _dir_modelos():
    import db
    d = db.BASE_DIR / "modelos"
    d.mkdir(parents=True, exist_ok=True)
    return d


def disponivel(con: sqlite3.Connection | None = None) -> bool:
    return _carregar(modelo(con)) is not None


def motivo_indisponivel(con: sqlite3.Connection | None = None) -> str:
    m = modelo(con)
    _carregar(m)
    return _indisponivel.get(m.nome, "")


def _np():
    import numpy
    return numpy


_tokenizers: dict[str, object] = {}


def _tokenizer_livre(m: Modelo, instancia):
    """Cópia do tokenizer SEM corte, para medir e fatiar janelas.

    Cópia porque o original é o que o modelo usa: desligar o corte nele
    mudaria o que o próprio fastembed embute.
    """
    if m.nome not in _tokenizers:
        from tokenizers import Tokenizer
        tk = Tokenizer.from_str(instancia.model.tokenizer.to_str())
        tk.no_truncation()
        tk.no_padding()
        limite = instancia.model.tokenizer.truncation["max_length"]
        _tokenizers[m.nome] = (tk, limite)
    return _tokenizers[m.nome]


def janelas_de(m: Modelo, instancia, texto: str) -> list[tuple[str, int]]:
    """Texto em janelas que cabem no modelo: [(janela, n_tokens)].

    Corta pelos offsets do próprio tokenizer, então nenhuma janela passa do
    limite e nenhum pedaço do texto fica de fora. O prefixo de passagem e os
    tokens especiais ocupam lugar na janela e são descontados.
    """
    tk, limite = _tokenizer_livre(m, instancia)
    reserva = 2 + len(tk.encode(m.prefixo_passagem, add_special_tokens=False).ids)
    cabe = max(16, limite - reserva)
    enc = tk.encode(texto, add_special_tokens=False)
    offs = enc.offsets
    if len(offs) <= cabe:
        return [(texto, max(1, len(offs)))]
    saida = []
    for i in range(0, len(offs), cabe):
        bloco = offs[i:i + cabe]
        saida.append((texto[bloco[0][0]:bloco[-1][1]], len(bloco)))
    return saida


def _normalizar(v):
    np = _np()
    v = np.asarray(v, dtype=np.float32)
    n = float(np.linalg.norm(v))
    return v / n if n else v


def embutir(textos: list[str], m: Modelo | None = None,
            tipo: str = "passagem") -> list[bytes]:
    """Texto -> vetor float32 normalizado, pronto para gravar como BLOB.

    `tipo` é "passagem" (o que vai para o índice) ou "consulta" (a pergunta):
    modelos assimétricos embutem os dois de formas diferentes.

    Normaliza aqui, na entrada, e não na consulta: com todo mundo em norma 1,
    cosseno é produto interno, e a busca não precisa dividir por nada.

    Devolve [] se o modelo não estiver disponível — quem chama trata isso como
    "sem lado vetorial", nunca como erro.
    """
    m = m or MODELOS[PADRAO]
    inst = _carregar(m)
    if inst is None or not textos:
        return []
    np = _np()
    consulta = tipo == "consulta"
    prefixo = m.prefixo_consulta if consulta else m.prefixo_passagem

    def _rodar(lote: list[str]):
        lote = [prefixo + t for t in lote]
        if m.tarefas:
            return (inst.query_embed(lote, batch_size=m.lote) if consulta
                    else inst.passage_embed(lote, batch_size=m.lote))
        return inst.embed(lote, batch_size=m.lote)

    if consulta or not m.janelas:
        return [_normalizar(v).tobytes() for v in _rodar(textos)]

    # Janelas: todas num lote só (o onnx paga por chamada, não por texto) e
    # depois a média de cada trecho, ponderada pelo tamanho da janela — uma
    # sobra de 10 tokens no fim não pode pesar o mesmo que 120 de conteúdo.
    planos = [janelas_de(m, inst, t) for t in textos]
    planas = [j for p in planos for j, _ in p]
    vetores = [np.asarray(v, dtype=np.float32) for v in _rodar(planas)]
    saida, i = [], 0
    for p in planos:
        pesos = np.array([n for _, n in p], dtype=np.float32)
        bloco = np.stack(vetores[i:i + len(p)])
        i += len(p)
        saida.append(_normalizar((bloco * pesos[:, None]).sum(0)).tobytes())
    return saida


# Uma busca embute a MESMA consulta 2 ou 3 vezes: a decisão de modo embute
# para medir alcance, a âncora embute para ordenar, e a ponte embute de novo.
# São 6,8 ms cada no MiniLM, um terço do tempo da busca inteira. O cache é pelo
# argumento inteiro (texto E modelo) — não há como servir vetor de outro modelo.
@functools.lru_cache(maxsize=256)
def _consulta_em_cache(texto: str, apelido: str) -> bytes | None:
    vs = embutir([texto], MODELOS[apelido], tipo="consulta")
    return vs[0] if vs else None


def embutir_um(texto: str, con: sqlite3.Connection | None = None) -> bytes | None:
    """Vetor de UMA consulta, no modelo do banco."""
    return _consulta_em_cache(texto, modelo(con).apelido)


def similaridades(consulta: bytes, vetores: list[bytes]) -> list[float]:
    """Cosseno da consulta contra cada vetor, na ordem em que vieram.

    Uma multiplicação de matriz só: 1400x1024 é instantâneo, e fazer em lote
    evita o laço em Python que dominaria o tempo.
    """
    if not consulta or not vetores:
        return []
    np = _np()
    q = np.frombuffer(consulta, dtype=np.float32)
    m = np.frombuffer(b"".join(vetores), dtype=np.float32).reshape(len(vetores), -1)
    if m.shape[1] != q.shape[0]:  # vetor de outro modelo entrou na lista
        return []
    return (m @ q).tolist()


def preparar(con: sqlite3.Connection | None = None) -> str:
    """Força o carregamento (baixa o modelo na primeira vez). Para o CLI."""
    m = modelo(con)
    if _carregar(m) is None:
        return f"Embedding indisponível: {_indisponivel.get(m.nome, '')}"
    return f"Modelo pronto: {m.nome} ({m.dim} dimensões)."


# --------------------------------------------------------------------------
# Reranker: um cross-encoder que lê consulta e trecho JUNTOS
# --------------------------------------------------------------------------
#
# O embedding comprime o trecho num vetor antes de ver a pergunta; o
# cross-encoder lê os dois de uma vez e por isso julga melhor. Medido contra
# rótulo: a precisão nas 3 primeiras sobe de 58% para 61% em Regional e de 44%
# para 52% em Investimento, onde o termo técnico vai de 56% para 69%.
#
# **Só ligue onde o corte é no topo.** Na tarefa de montar documento, que leva
# uma fatia larga, o resultado foi IDÊNTICO ao da busca sozinha, a 20 vezes o
# custo: reordenar não muda o conjunto quando se leva tudo. E custa caro, de
# 0,9 a 2,2 s por consulta contra 10 a 50 ms.
#
# Mais 1,1GB de modelo, e opcional como o resto: sem ele a busca devolve a
# ordem que já tinha.
RERANKER = "jinaai/jina-reranker-v2-base-multilingual"

_rk = None
_rk_indisponivel = ""


def _carregar_reranker():
    global _rk, _rk_indisponivel
    if _rk is not None or _rk_indisponivel:
        return _rk
    try:
        from fastembed.rerank.cross_encoder import TextCrossEncoder
    except ImportError:
        _rk_indisponivel = "fastembed sem suporte a reranker"
        return None
    try:
        _rk = TextCrossEncoder(model_name=RERANKER, cache_dir=str(_dir_modelos()))
    except Exception as e:  # download interrompido, disco cheio
        _rk_indisponivel = f"{type(e).__name__}: {e}"
        return None
    return _rk


def rerank_disponivel() -> bool:
    return _carregar_reranker() is not None


def reordenar(consulta: str, textos: list[str]) -> list[int] | None:
    """Devolve a ordem dos índices, do mais relevante ao menos.

    None quando o modelo não está disponível — quem chama mantém a ordem que
    já tinha, nunca fica sem resposta.
    """
    rk = _carregar_reranker()
    if rk is None or len(textos) < 2:
        return None
    try:
        scores = list(rk.rerank(consulta, textos))
    except Exception:  # noqa: BLE001 — reordenação é melhoria, não requisito
        return None
    return [i for i, _ in sorted(enumerate(scores), key=lambda p: -p[1])]

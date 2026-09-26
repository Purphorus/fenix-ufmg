"""Mede a recuperação contra os rótulos e compara com a rodada anterior.

Existe porque sem medição toda mudança na busca vira opinião. Nesta mesma
investigação eu projetei de 5 a 8 pontos de ganho para a limpeza de ruído e o
medido foi zero; e o reranker, que eu achava caro demais, rendeu 8 pontos em
Investimento. Nenhum dos dois era previsível sem rodar.

Cada rodada é gravada em `~/.ufmg-moodle-mcp/avaliacao/`, então o relatório
sempre compara com a última. Rodar duas vezes seguidas sem mexer em nada deve
dar diferença zero — se der outra coisa, há não determinismo na busca.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

import busca
import db
from avaliacao.consultas import MATERIAS

DIR_RODADAS = db.BASE_DIR / "avaliacao"
ROTULOS = Path(__file__).parent / "rotulos.json"


def _rotulos() -> tuple[dict, dict]:
    d = json.loads(ROTULOS.read_text(encoding="utf-8"))
    return d["consulta"], d["documento"]


@dataclass
class Medida:
    """Uma linha do relatório."""

    materia: str
    tarefa: str            # pontual | documento
    tokens: int = 0
    precisao: float = 0.0
    termo: float = 0.0
    assunto: float = 0.0
    uteis: int = 0
    arquivos: int = 0
    base: int = 0          # slots rotulados; é o denominador da precisão
    sem_rotulo: int = 0
    avisos: int = 0        # consultas com aviso de cobertura fraca
    ms: float = 0.0
    # consulta -> [acertos, rotulados]. É o que permite o teste pareado: com
    # só o agregado, "▲4%" não diz se veio de uma consulta ou de todas.
    por_consulta: dict = field(default_factory=dict)


def _tokens(achados) -> int:
    return sum(len(a["trecho"]) + len(a["nome"] or "") + 40 for a in achados) // 4


def medir_pontual(con, courseid: int, rerank: bool) -> Medida:
    look, _ = _rotulos()
    m = MATERIAS[courseid]
    r = Medida(m["nome"], "pontual")
    ac = to = ta = tt = aa = at = 0
    t0 = time.perf_counter()
    for q, tipo in m["consultas"]:
        a = busca.buscar(con, q, courseid=courseid, limite=8, rerank=rerank)
        r.tokens += _tokens(a[:3])
        if busca.aviso_cobertura(con, q, a, courseid):
            r.avisos += 1
        # Consulta ausente não entra na precisão: todo trecho devolvido é "n"
        # por definição, e quem a mede é `avisos`. Contada, ela derrubava a
        # matéria com mais assunto ausente — Investimento marcava 29% com
        # termo a 46% e assunto a 67%.
        if tipo == "ausente":
            continue
        aq = tq = 0
        for x in a[:3]:
            lab = look.get(q, {}).get(x["sha256"])
            if lab is None:
                r.sem_rotulo += 1
                continue
            to += 1
            tq += 1
            ok = lab == "s"
            ac += ok
            aq += ok
            if tipo == "termo":
                tt += 1
                ta += ok
            elif tipo == "assunto":
                at += 1
                aa += ok
        r.por_consulta[q] = [aq, tq]
    r.ms = (time.perf_counter() - t0) / max(len(m["consultas"]), 1) * 1000
    r.base = to
    r.precisao = ac / max(to, 1)
    r.termo = ta / max(tt, 1)
    r.assunto = aa / max(at, 1)
    return r


def rotulo_documento(doc: dict, topico: str, sha: str) -> str | None:
    """Rótulo de um trecho PARA UM TÓPICO.

    O formato antigo era por trecho só ({sha: s/n}: "é conteúdo útil do
    curso?"), e deixava passar trecho bom sobre OUTRO assunto — justamente o
    que a ponte do grafo tende a trazer. O novo é {tópico: {sha: s/n}}. Os dois
    são lidos: tópico com rótulo próprio usa o dele; senão vale o antigo.
    """
    proprio = doc.get(topico)
    if isinstance(proprio, dict):
        return proprio.get(sha)
    v = doc.get(sha)
    return v if isinstance(v, str) else None


def medir_documento(con, courseid: int) -> Medida:
    """Precisão sobre os pares (tópico, trecho): o mesmo trecho pode servir a
    um tópico e não a outro, então `uteis` e `base` contam pares."""
    _, doc = _rotulos()
    m = MATERIAS[courseid]
    r = Medida(m["nome"], "documento")
    arqs = set()
    t0 = time.perf_counter()
    for t in m["topicos"]:
        a = busca.buscar(con, t, courseid=courseid, limite=8, ponte=True)
        r.tokens += _tokens(a)
        rot = []
        for x in a:
            arqs.add(x["arquivo_id"])
            v = rotulo_documento(doc, t, x["sha256"])
            if v is None:
                r.sem_rotulo += 1
            else:
                rot.append(v)
        r.por_consulta[t] = [sum(1 for v in rot if v == "s"), len(rot)]
        r.uteis += r.por_consulta[t][0]
        r.base += len(rot)
    r.ms = (time.perf_counter() - t0) / max(len(m["topicos"]), 1) * 1000
    r.precisao = r.uteis / max(r.base, 1)
    r.arquivos = len(arqs)
    return r


# A rodada anterior só era comparável se o corpus fosse o mesmo, e nada
# registrava isso. Entre 14/09 e 17/09 o sync trouxe anexos de fórum que não
# existiam antes: as duas rodadas foram comparadas como se medissem a mesma
# coisa, e a precisão de Regional "subiu" 29 pontos enquanto o número de
# trechos sem rótulo dobrava. Comparar código novo em corpus novo não mede
# nem o código nem o corpus.


def impressao_corpus(con, cursos: list[int]) -> dict:
    """O que a busca tinha para achar nesta rodada.

    O sha resume o conjunto de trechos indexados: qualquer extração,
    reindexação ou sync que mude o material muda o sha, e o relatório para de
    fingir que a comparação vale.
    """
    import hashlib

    por_curso = {}
    h = hashlib.sha256()
    for c in sorted(cursos):
        shas = [
            r["sha256"]
            for r in con.execute(
                """SELECT t.sha256 FROM trechos t
                   JOIN arquivos a ON a.id = t.arquivo_id
                   WHERE a.courseid = ? AND a.ignorado = 0
                   ORDER BY t.sha256""",
                (c,),
            )
        ]
        arqs = con.execute(
            "SELECT COUNT(*) n FROM arquivos WHERE courseid = ? AND ignorado = 0", (c,)
        ).fetchone()["n"]
        por_curso[str(c)] = {"trechos": len(shas), "arquivos": arqs}
        for x in shas:
            h.update(x.encode())
    return {
        "sha": h.hexdigest()[:16],
        "trechos": sum(v["trechos"] for v in por_curso.values()),
        "arquivos": sum(v["arquivos"] for v in por_curso.values()),
        "por_curso": por_curso,
    }


@dataclass
class Rodada:
    em: str
    rerank: bool
    medidas: list = field(default_factory=list)
    corpus: dict = field(default_factory=dict)
    # Como a busca estava montada (modelo de embedding, contexto). O corpus
    # igual é o que torna a comparação válida; isto é o que diz O QUE mudou.
    config: dict = field(default_factory=dict)


# --------------------------------------------------------------------------
# Significância: a diferença é efeito ou acaso?
# --------------------------------------------------------------------------
#
# Com 13 a 30 slots rotulados por matéria, "▲4%" pode ser uma consulta só
# mudando de lado. Voorhees & Buckley (SIGIR 2002) mostram que mesmo com 50
# tópicos diferenças acima de 10% às vezes invertem. O teste é o de
# aleatorização pareada (Fisher), que Smucker, Allan & Carterette (CIKM 2007)
# recomendam para IR: sob a hipótese nula, trocar o rótulo "novo"/"velho" de
# cada consulta não muda nada, então se conta quantas das trocas dão diferença
# tão grande quanto a observada.

PERMUTACOES = 20000
EXATO_ATE = 16          # 2^16 trocas: enumera tudo, sem sorteio


def pares_por_consulta(nova: dict, velha: dict) -> list[tuple[float, float]]:
    """Precisão por consulta nas duas rodadas, só onde as duas têm rótulo.

    Consulta sem rótulo num dos lados fica fora: comparar 2/3 com "nada
    medido" inventaria uma diferença.
    """
    pares = []
    for q, (a, t) in (nova or {}).items():
        v = (velha or {}).get(q)
        if not v or not t or not v[1]:
            continue
        pares.append((a / t, v[0] / v[1]))
    return pares


def permutacao_pareada(pares: list[tuple[float, float]],
                       n: int = PERMUTACOES, semente: int = 0) -> float:
    """p-valor bilateral da diferença média. 1.0 quando não há diferença.

    Determinístico (semente fixa): rodar o relatório duas vezes sobre as
    mesmas rodadas tem de dar o mesmo p.
    """
    import itertools
    import random

    d = [x - y for x, y in pares if abs(x - y) > 1e-12]
    if not d:
        return 1.0
    obs = abs(sum(d))
    tol = 1e-9
    if len(d) <= EXATO_ATE:
        extremos = sum(
            1 for sinais in itertools.product((1, -1), repeat=len(d))
            if abs(sum(s * v for s, v in zip(sinais, d))) >= obs - tol
        )
        return extremos / 2 ** len(d)
    rng = random.Random(semente)
    extremos = sum(
        1 for _ in range(n)
        if abs(sum(v if rng.random() < 0.5 else -v for v in d)) >= obs - tol
    )
    return (extremos + 1) / (n + 1)


def significancia(r: "Rodada", ant: "Rodada") -> list[dict]:
    """Uma linha por matéria/tarefa, mais o total de cada tarefa."""
    velhas = {(m["materia"], m["tarefa"]): m for m in ant.medidas}
    linhas, todos = [], {"pontual": [], "documento": []}
    for m in r.medidas:
        v = velhas.get((m["materia"], m["tarefa"]))
        pares = pares_por_consulta(m.get("por_consulta"), v and v.get("por_consulta"))
        if not pares:
            continue
        todos[m["tarefa"]] += pares
        linhas.append(_linha_sig(m["materia"], m["tarefa"], pares))
    for tarefa, pares in todos.items():
        if pares and sum(1 for l in linhas if l["tarefa"] == tarefa) > 1:
            linhas.append(_linha_sig("TODAS", tarefa, pares))
    return linhas


def _linha_sig(materia: str, tarefa: str, pares) -> dict:
    delta = sum(x - y for x, y in pares) / len(pares)
    mudaram = sum(1 for x, y in pares if abs(x - y) > 1e-12)
    return {"materia": materia, "tarefa": tarefa, "n": len(pares),
            "mudaram": mudaram, "delta": delta, "p": permutacao_pareada(pares)}


def rodar(con, rerank: bool = True, cursos: list[int] | None = None) -> Rodada:
    alvos = cursos or sorted(MATERIAS)
    r = Rodada(time.strftime("%Y-%m-%dT%H:%M:%S"), rerank)
    r.corpus = impressao_corpus(con, alvos)
    r.corpus["rotulos"] = impressao_rotulos()
    r.config = busca.configuracao(con)
    for c in alvos:
        if not con.execute(
            "SELECT 1 FROM arquivos WHERE courseid = ? AND ignorado = 0 LIMIT 1", (c,)
        ).fetchone():
            continue
        r.medidas.append(asdict(medir_pontual(con, c, rerank)))
        r.medidas.append(asdict(medir_documento(con, c)))
    return r


def gravar(r: Rodada) -> Path:
    DIR_RODADAS.mkdir(parents=True, exist_ok=True)
    p = DIR_RODADAS / f"{r.em.replace(':', '-')}.json"
    p.write_text(json.dumps(asdict(r), ensure_ascii=False, indent=1), encoding="utf-8")
    return p


def anterior(antes_de: str | None = None) -> Rodada | None:
    if not DIR_RODADAS.is_dir():
        return None
    arqs = sorted(DIR_RODADAS.glob("*.json"))
    if antes_de:
        arqs = [a for a in arqs if a.stem < antes_de.replace(":", "-")]
    if not arqs:
        return None
    d = json.loads(arqs[-1].read_text(encoding="utf-8"))
    return Rodada(**d)


def _delta(novo: float, velho: float | None, pct: bool = True) -> str:
    if velho is None:
        return ""
    d = novo - velho
    if abs(d) < (0.005 if pct else 0.5):
        return "  ="
    seta = "▲" if d > 0 else "▼"
    return f" {seta}{abs(d):.0%}" if pct else f" {seta}{abs(d):.0f}"


def listar_sem_rotulo(con, cursos: list[int] | None = None, rerank: bool = True) -> str:
    """Os trechos que a busca devolveu e ninguém rotulou ainda.

    Sai no formato de `rotulos.json` para colar direto: a barreira para fechar
    a medição é justamente ter de achar o sha na mão.
    """
    look, doc = _rotulos()
    linhas: list[str] = []
    for c in cursos or sorted(MATERIAS):
        m = MATERIAS.get(c)
        if m is None:
            linhas.append(
                f"\ncurso {c} não tem conjunto de consultas em "
                "avaliacao/consultas.py — nada a medir nele"
            )
            continue
        faltam: dict[str, list] = {}
        for q, _tipo in m["consultas"]:
            for x in busca.buscar(con, q, courseid=c, limite=8, rerank=rerank)[:3]:
                if look.get(q, {}).get(x["sha256"]) is None:
                    faltam.setdefault(q, []).append(x)
        if not faltam:
            continue
        linhas.append(f"\n{m['nome']} — consulta pontual")
        for q, achados in faltam.items():
            linhas.append(f'  "{q}": {{')
            for x in achados:
                trecho = " ".join((x["trecho"] or "").split())[:90]
                linhas.append(f'    "{x["sha256"]}": "?",')
                linhas.append(f'        // {x["nome"]}, {x["rotulo"]}: {trecho}')
            linhas.append("  },")
    if not linhas:
        return "Nada sem rótulo: a medição está fechada."
    return (
        "Trechos devolvidos e ainda sem rótulo. Troque \"?\" por \"s\" (serve) ou\n"
        '"n" (não serve) e cole em avaliacao/rotulos.json, na chave "consulta".'
        + "\n".join(linhas)
    )


def _mesmo_corpus(r: Rodada, ant: Rodada | None) -> bool:
    """As duas rodadas mediram o mesmo material?

    Rodada antiga não tem impressão digital gravada; nesse caso não dá para
    afirmar que é o mesmo corpus, e não afirmar é o certo.
    """
    if ant is None:
        return False
    a, b = r.corpus.get("sha"), (ant.corpus or {}).get("sha")
    return bool(a) and a == b and _mesmos_rotulos(r, ant)


def impressao_rotulos() -> str:
    """Sha dos rótulos E das consultas: a régua com que a rodada foi medida.

    As consultas entram porque reclassificar uma delas (termo → ausente) muda
    a precisão sem mudar um rótulo sequer — e cinco de Investimento foram
    reclassificadas assim em 24/09/2026.
    """
    import hashlib
    h = hashlib.sha256(ROTULOS.read_bytes())
    h.update(json.dumps(MATERIAS, sort_keys=True, ensure_ascii=False).encode())
    return h.hexdigest()[:16]


def _mesmos_rotulos(r: Rodada, ant: Rodada) -> bool:
    """As duas rodadas foram medidas com a MESMA régua?

    Mesmo corpus não basta. Em 24/09/2026 os rótulos foram refeitos às cegas,
    mais rigorosos e com documento julgado por tópico: a primeira rodada
    depois disso mostrou ▼13% a ▼29% em tudo com o código idêntico — era a
    régua que tinha mudado, não a busca. Rodada sem a impressão dos rótulos
    (anterior a esta guarda) não é comparável: não afirmar é o certo.
    """
    a, b = r.corpus.get("rotulos"), (ant.corpus or {}).get("rotulos")
    return bool(a) and a == b


ALFA = 0.05


def _secao_significancia(r: Rodada, ant: Rodada) -> list[str]:
    L = ["", "SIGNIFICÂNCIA (aleatorização pareada, por consulta)"]
    sig = significancia(r, ant)
    if not sig:
        # Rodada gravada antes de existir `por_consulta`: sem o detalhe, o
        # teste não tem o que parear. Dizer isso é melhor que omitir a seção,
        # que pareceria "nada mudou".
        L.append("  sem dados por consulta na rodada anterior — rode as duas")
        L.append("  versões de novo para ter o teste.")
        return L
    L.append(f"{'matéria':<28} {'tarefa':<10} {'Δ média':>8} {'n':>4} {'mudaram':>8} {'p':>7}  leitura")
    for s in sig:
        if not s["mudaram"]:
            leitura = "idêntico"
        elif s["p"] < ALFA:
            leitura = "efeito" + (" (melhora)" if s["delta"] > 0 else " (piora)")
        else:
            leitura = "pode ser acaso"
        L.append(
            f"{s['materia'][:27]:<28} {s['tarefa']:<10} {s['delta']:>+8.1%}"
            f" {s['n']:>4} {s['mudaram']:>8} {s['p']:>7.3f}  {leitura}"
        )
    L.append("  Δ média é a média da precisão POR CONSULTA, não a precisão agregada")
    L.append("  da tabela; 'n' conta só consultas com rótulo nas duas rodadas.")
    return L


def relatorio(r: Rodada, ant: Rodada | None = None) -> str:
    """Relatório legível, com a variação contra a rodada anterior."""
    comparavel = _mesmo_corpus(r, ant)
    velhas = {}
    if ant and comparavel:
        for m in ant.medidas:
            velhas[(m["materia"], m["tarefa"])] = m

    L = [f"Avaliação da busca — {r.em}",
         f"reranker: {'ligado' if r.rerank else 'desligado'}"]
    if r.corpus:
        L.append(
            f"corpus: {r.corpus['trechos']} trechos em {r.corpus['arquivos']} "
            f"arquivos (sha {r.corpus['sha']})"
        )
    if r.config:
        L.append("busca: " + ", ".join(f"{k}={v}" for k, v in sorted(r.config.items())))
    if ant and comparavel:
        L.append(f"comparado com: {ant.em} (reranker {'ligado' if ant.rerank else 'desligado'})")
        mudou = {k for k in set(r.config) | set(ant.config or {})
                 if r.config.get(k) != (ant.config or {}).get(k)}
        if mudou and ant.config:
            L.append("  mudou desde lá: " + ", ".join(
                f"{k} {(ant.config or {}).get(k)} → {r.config.get(k)}" for k in sorted(mudou)))
    elif ant:
        # Sem esta recusa o relatório mistura mudança de código com mudança de
        # material e apresenta a soma como se fosse efeito do código.
        velho = (ant.corpus or {}).get("sha")
        so_rotulos = bool(velho) and velho == r.corpus.get("sha")
        if so_rotulos:
            L.append(f"SEM COMPARAÇÃO com {ant.em}: os rótulos não são os mesmos")
            L.append("  mesmo corpus, régua diferente — a variação seria da régua, não da busca.")
            L.append("  Para comparar código: rode as duas versões com os rótulos atuais.")
        else:
            L.append(f"SEM COMPARAÇÃO com {ant.em}: o corpus não é o mesmo")
            if velho:
                d = r.corpus.get("trechos", 0) - (ant.corpus or {}).get("trechos", 0)
                L.append(
                    f"  lá: sha {velho}, {(ant.corpus or {}).get('trechos')} trechos"
                    f"  |  aqui: sha {r.corpus.get('sha')}, {r.corpus.get('trechos')}"
                    f" trechos ({d:+d})"
                )
            else:
                L.append("  a rodada anterior é de antes da impressão digital do corpus")
            L.append("  Para comparar código: rode as duas versões sobre o mesmo banco.")
    else:
        L.append("primeira rodada: não há com o que comparar")
    L.append("")

    L.append("CONSULTA PONTUAL (precisão nas 3 primeiras)")
    L.append(f"{'matéria':<28} {'precisão':>12} {'termo':>12} {'assunto':>12} {'base':>5} {'tokens':>9} {'ms':>7} {'avisos':>7} {'s/rót':>6}")
    for m in [x for x in r.medidas if x["tarefa"] == "pontual"]:
        v = velhas.get((m["materia"], "pontual"))
        L.append(
            f"{m['materia'][:27]:<28}"
            f" {m['precisao']:>7.0%}{_delta(m['precisao'], v and v['precisao']):>5}"
            f" {m['termo']:>7.0%}{_delta(m['termo'], v and v['termo']):>5}"
            f" {m['assunto']:>7.0%}{_delta(m['assunto'], v and v['assunto']):>5}"
            f" {m.get('base', 0):>5} {m['tokens']:>9} {m['ms']:>6.0f} {m['avisos']:>7} {m['sem_rotulo']:>6}"
        )

    L.append("")
    L.append("DOCUMENTO (material que entra num resumo da matéria)")
    L.append(f"{'matéria':<28} {'precisão':>12} {'úteis':>8} {'base':>5} {'arquivos':>9} {'tokens':>9} {'ms':>7} {'s/rót':>6}")
    for m in [x for x in r.medidas if x["tarefa"] == "documento"]:
        v = velhas.get((m["materia"], "documento"))
        L.append(
            f"{m['materia'][:27]:<28}"
            f" {m['precisao']:>7.0%}{_delta(m['precisao'], v and v['precisao']):>5}"
            f" {m['uteis']:>4}{_delta(m['uteis'], v and v['uteis'], pct=False):>4}"
            f" {m.get('base', 0):>5} {m['arquivos']:>9} {m['tokens']:>9} {m['ms']:>6.0f} {m['sem_rotulo']:>6}"
        )

    # Mudar a ordenação traz trechos que nunca foram rotulados, e aí a variação
    # é medida sobre um subconjunto. Sem este aviso, um número parece firme
    # quando não é — foi o que aconteceu ao medir a limpeza de ruído.
    # O que torna o número frágil não é `sem_rotulo` alto em si, é ele alto
    # EM RELAÇÃO ao que entrou na conta: 30 sem rótulo contra 30 rotulados
    # quer dizer que a precisão foi medida sobre metade do que a busca
    # devolveu, e a outra metade pode ser qualquer coisa.
    duvidosas = [
        m for m in r.medidas
        if m["sem_rotulo"] >= max(5, m.get("base", 0))
    ]
    if duvidosas:
        L.append("")
        L.append("⚠ PRECISÃO MEDIDA SOBRE MENOS DA METADE do que a busca devolveu.")
        L.append("  O resto veio sem rótulo e ficou fora da conta — a variação")
        L.append("  contra a rodada anterior pode ser só mudança de denominador.")
        for m in duvidosas:
            base = m.get("base", 0)
            L.append(
                f"    {m['materia']} / {m['tarefa']}: {base} na conta, "
                f"{m['sem_rotulo']} fora"
            )
        L.append("  Para fechar: `cli.py avaliar --sem-rotulo` lista os trechos, e")
        L.append("  rotular é editar `avaliacao/rotulos.json`.")

    if ant and comparavel:
        L += _secao_significancia(r, ant)

    L.append("")
    L.append("Os rótulos foram feitos pelo assistente, não pelo usuário — são")
    L.append("indicativos. 'avisos' conta as consultas em que a busca disse que o")
    L.append("material só menciona o assunto; para as consultas marcadas ausentes")
    L.append("esse número deve ser alto.")
    return "\n".join(L)

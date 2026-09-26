"""A avaliação só vale se souber quando NÃO vale.

Comparar duas rodadas medidas sobre corpus diferentes soma mudança de código
com mudança de material e apresenta o total como efeito do código. Foi o que
aconteceu entre 14/09 e 17/09 de 2026.
"""

from __future__ import annotations

import pytest

from avaliacao.rodar import Rodada, _mesmo_corpus, impressao_corpus, relatorio


def _rodada(sha, **kw):
    r = Rodada(kw.get("em", "2026-09-20T10:00:00"), True)
    # rodada moderna: grava a régua (rótulos) além do corpus
    r.corpus = {"sha": sha, "trechos": kw.get("trechos", 100), "arquivos": 10,
                "rotulos": kw.get("rotulos", "r0")}
    r.medidas = [{
        "materia": "Macro", "tarefa": "pontual", "precisao": kw.get("p", 0.8),
        "termo": 1.0, "assunto": 0.5, "uteis": 0, "arquivos": 0, "base": 10,
        "sem_rotulo": 1, "avisos": 0, "ms": 1.0, "tokens": 100,
    }]
    return r


class TestComparabilidade:
    def test_mesmo_sha_compara(self):
        assert _mesmo_corpus(_rodada("abc"), _rodada("abc")) is True

    def test_sha_diferente_nao_compara(self):
        assert _mesmo_corpus(_rodada("abc"), _rodada("xyz")) is False

    def test_rodada_antiga_sem_impressao_nao_compara(self):
        velha = Rodada("2026-09-17T10:00:00", True)  # corpus vazio
        assert _mesmo_corpus(_rodada("abc"), velha) is False

    def test_relatorio_recusa_e_explica(self):
        texto = relatorio(_rodada("abc", p=0.9), _rodada("xyz", p=0.5, trechos=80))
        assert "SEM COMPARAÇÃO" in texto
        # O delta não pode aparecer: é ele que mente.
        assert "▲" not in texto and "▼" not in texto

    def test_relatorio_compara_quando_pode(self):
        texto = relatorio(_rodada("abc", p=0.9), _rodada("abc", p=0.5))
        assert "comparado com" in texto
        assert "▲" in texto


class TestImpressaoDoCorpus:
    def test_muda_quando_o_material_muda(self, con):
        from testes.test_invariantes import _indexar

        _indexar(con, 1, ["um trecho qualquer", "outro trecho"])
        antes = impressao_corpus(con, [6095])
        assert antes["trechos"] == 2 and antes["arquivos"] == 1

        _indexar(con, 2, ["material novo que chegou depois"], nome="Aula 2.pdf")
        depois = impressao_corpus(con, [6095])
        assert depois["sha"] != antes["sha"], "o sha não viu o material novo"
        assert depois["trechos"] == 3

    def test_estavel_quando_nada_muda(self, con):
        from testes.test_invariantes import _indexar

        _indexar(con, 1, ["a", "b"])
        assert impressao_corpus(con, [6095]) == impressao_corpus(con, [6095])

    def test_arquivo_ignorado_fica_de_fora(self, con):
        from testes.test_invariantes import _indexar

        _indexar(con, 1, ["conteúdo"])
        antes = impressao_corpus(con, [6095])
        _indexar(con, 2, ["escaneado"], nome="Scan.pdf")
        con.execute("UPDATE arquivos SET ignorado = 1 WHERE id = 2")
        con.commit()
        # O que está fora da busca não pode mexer na impressão digital dela.
        assert impressao_corpus(con, [6095])["sha"] == antes["sha"]


# --------------------------------------------------------------------------
# Significância: "▲4%" só vale se não for uma consulta mudando de lado
# --------------------------------------------------------------------------

from avaliacao.rodar import (  # noqa: E402
    pares_por_consulta, permutacao_pareada, significancia,
)


def _com_consultas(sha, por_consulta, p=0.5, **kw):
    r = _rodada(sha, p=p, **kw)
    r.medidas[0]["por_consulta"] = por_consulta
    return r


class TestPermutacao:
    def test_sem_diferenca_p_um(self):
        assert permutacao_pareada([(0.5, 0.5), (1.0, 1.0)]) == 1.0

    def test_melhora_em_todas_e_significativa(self):
        # 10 consultas, todas melhoram: só 2 das 1024 trocas de sinal são tão
        # extremas (todas + ou todas -), p = 2/1024
        pares = [(2 / 3, 1 / 3)] * 10
        assert permutacao_pareada(pares) == pytest.approx(2 / 1024)

    def test_uma_consulta_so_nao_e_significativa(self):
        pares = [(1.0, 0.0)] + [(0.5, 0.5)] * 20
        assert permutacao_pareada(pares) == 1.0

    def test_sorteio_e_deterministico(self):
        # acima de EXATO_ATE o teste sorteia; rodar duas vezes tem de dar o mesmo p
        pares = [(0.66, 0.33) if i % 3 else (0.33, 0.66) for i in range(30)]
        assert permutacao_pareada(pares) == permutacao_pareada(pares)

    def test_consulta_sem_rotulo_num_lado_fica_fora(self):
        nova = {"a": [2, 3], "b": [1, 3], "c": [0, 0]}
        velha = {"a": [1, 3], "b": [0, 0], "c": [1, 3]}
        assert pares_por_consulta(nova, velha) == [(2 / 3, 1 / 3)]


class TestRelatorioComSignificancia:
    def _q(self, n, a, t=3):
        return {f"consulta {i}": [a, t] for i in range(n)}

    def test_melhora_consistente_vira_efeito(self):
        velha = _com_consultas("abc", self._q(10, 1))
        nova = _com_consultas("abc", self._q(10, 2), p=0.66)
        texto = relatorio(nova, velha)
        assert "SIGNIFICÂNCIA" in texto
        assert "efeito (melhora)" in texto

    def test_uma_consulta_mudando_e_acaso(self):
        velha = _com_consultas("abc", self._q(10, 1))
        q = self._q(10, 1)
        q["consulta 0"] = [3, 3]
        nova = _com_consultas("abc", q, p=0.6)
        texto = relatorio(nova, velha)
        # o agregado sobe e a tabela mostra ▲; o teste pareado tem de dizer
        # que isso pode ser uma consulta só
        assert "▲" in texto
        assert "pode ser acaso" in texto and "efeito" not in texto

    def test_rodada_antiga_sem_detalhe_avisa(self):
        velha = _rodada("abc")                      # sem por_consulta
        nova = _com_consultas("abc", self._q(5, 2))
        assert "sem dados por consulta" in relatorio(nova, velha)

    def test_corpus_diferente_nao_testa(self):
        velha = _com_consultas("xyz", self._q(10, 1))
        nova = _com_consultas("abc", self._q(10, 2))
        assert "SIGNIFICÂNCIA" not in relatorio(nova, velha)

    def test_diz_o_que_mudou_na_configuracao(self):
        velha = _com_consultas("abc", self._q(3, 1))
        velha.config = {"embedding": "minilm", "contexto": False}
        nova = _com_consultas("abc", self._q(3, 1))
        nova.config = {"embedding": "e5-large", "contexto": False}
        assert "embedding minilm → e5-large" in relatorio(nova, velha)

    def test_total_junta_as_materias(self):
        velha = _com_consultas("abc", self._q(6, 1))
        velha.medidas.append(dict(velha.medidas[0], materia="Regional",
                                  por_consulta=self._q(6, 1)))
        nova = _com_consultas("abc", self._q(6, 2))
        nova.medidas.append(dict(nova.medidas[0], materia="Regional",
                                 por_consulta={f"r{i}": [2, 3] for i in range(6)}))
        velha.medidas[1]["por_consulta"] = {f"r{i}": [1, 3] for i in range(6)}
        linhas = significancia(nova, velha)
        total = [l for l in linhas if l["materia"] == "TODAS"]
        assert total and total[0]["n"] == 12


class TestMedicaoGravaCadaConsulta:
    def test_por_consulta_conta_so_o_rotulado(self, con, monkeypatch):
        from avaliacao import rodar as av

        materias = {1: {"nome": "X", "consultas": [("juros", "termo"), ("choque", "assunto")],
                        "topicos": ["crescimento"]}}
        rot = {"juros": {"s1": "s", "s2": "n"}, "choque": {"s3": "s"}}
        doc = {"s1": "s", "s4": "n"}
        achados = {"juros": ["s1", "s2", "s9"], "choque": ["s3"], "crescimento": ["s1", "s4", "s5"]}
        monkeypatch.setattr(av, "MATERIAS", materias)
        monkeypatch.setattr(av, "_rotulos", lambda: (rot, doc))
        monkeypatch.setattr(av.busca, "aviso_cobertura", lambda *a, **k: "")
        monkeypatch.setattr(av.busca, "buscar", lambda con, q, **k: [
            {"sha256": s, "trecho": "t", "nome": "n", "arquivo_id": 1} for s in achados[q]])
        m = av.medir_pontual(con, 1, rerank=False)
        # s9 não tem rótulo: fica fora do denominador daquela consulta
        assert m.por_consulta == {"juros": [1, 2], "choque": [1, 1]}
        d = av.medir_documento(con, 1)
        assert d.por_consulta == {"crescimento": [1, 2]}


    def test_consulta_ausente_fica_fora_da_precisao_e_conta_no_aviso(self, con, monkeypatch):
        from avaliacao import rodar as av

        materias = {1: {"nome": "X", "consultas": [("juros", "termo"), ("Macaulay", "ausente")],
                        "topicos": []}}
        rot = {"juros": {"s1": "s"}, "Macaulay": {"s2": "n", "s3": "n"}}
        achados = {"juros": ["s1"], "Macaulay": ["s2", "s3"]}
        monkeypatch.setattr(av, "MATERIAS", materias)
        monkeypatch.setattr(av, "_rotulos", lambda: (rot, {}))
        monkeypatch.setattr(av.busca, "aviso_cobertura",
                            lambda con, q, *a, **k: "aviso" if q == "Macaulay" else "")
        monkeypatch.setattr(av.busca, "buscar", lambda con, q, **k: [
            {"sha256": s, "trecho": "t", "nome": "n", "arquivo_id": 1} for s in achados[q]])
        m = av.medir_pontual(con, 1, rerank=False)
        assert m.precisao == 1.0 and m.base == 1
        assert "Macaulay" not in m.por_consulta
        assert m.avisos == 1


class TestRotuloDeDocumentoPorTopico:
    def test_trecho_bom_sobre_outro_assunto_nao_conta(self, con, monkeypatch):
        """O defeito do formato antigo: rótulo por trecho aprovava conteúdo
        útil do curso mesmo quando ele era de outro tópico."""
        from avaliacao import rodar as av

        materias = {1: {"nome": "X", "consultas": [],
                        "topicos": ["Solow", "Romer"]}}
        doc = {"Solow": {"a": "s", "b": "n"}, "Romer": {"a": "n", "c": "s"}}
        achados = {"Solow": ["a", "b"], "Romer": ["a", "c", "z"]}
        monkeypatch.setattr(av, "MATERIAS", materias)
        monkeypatch.setattr(av, "_rotulos", lambda: ({}, doc))
        monkeypatch.setattr(av.busca, "buscar", lambda con, q, **k: [
            {"sha256": s, "trecho": "t", "nome": "n", "arquivo_id": 1} for s in achados[q]])
        d = av.medir_documento(con, 1)
        # "a" serve a Solow e não a Romer: conta uma vez de cada jeito
        assert d.por_consulta == {"Solow": [1, 2], "Romer": [1, 2]}
        assert (d.uteis, d.base, d.sem_rotulo) == (2, 4, 1)

    def test_formato_antigo_ainda_e_lido(self):
        from avaliacao.rodar import rotulo_documento

        assert rotulo_documento({"sha1": "s"}, "qualquer", "sha1") == "s"
        novo = {"Solow": {"sha1": "n"}, "sha1": "s"}
        assert rotulo_documento(novo, "Solow", "sha1") == "n"


class TestMesmaRegua:
    """Mesmo corpus com rótulos diferentes não é comparável: a variação seria
    da régua. Em 24/09/2026 os rótulos foram refeitos e a primeira rodada
    depois disso mostrou ▼13% a ▼29% com o código idêntico."""

    def _com_rotulos(self, sha_rot, p=0.5):
        r = _com_consultas("abc", {"q": [1, 3]}, p=p)
        r.corpus["rotulos"] = sha_rot
        return r

    def test_mesmos_rotulos_compara(self):
        texto = relatorio(self._com_rotulos("r1", 0.9), self._com_rotulos("r1", 0.5))
        assert "comparado com" in texto and "▲" in texto

    def test_rotulos_diferentes_recusa(self):
        texto = relatorio(self._com_rotulos("r2", 0.9), self._com_rotulos("r1", 0.5))
        assert "os rótulos não são os mesmos" in texto
        assert "▲" not in texto and "▼" not in texto

    def test_rodada_sem_impressao_dos_rotulos_nao_compara(self):
        velha = _com_consultas("abc", {"q": [1, 3]})      # sem "rotulos"
        assert "SEM COMPARAÇÃO" in relatorio(self._com_rotulos("r1"), velha)

    def test_rodar_grava_a_impressao(self, con, monkeypatch):
        from avaliacao import rodar as av

        monkeypatch.setattr(av, "MATERIAS", {})
        r = av.rodar(con, rerank=False, cursos=[])
        assert r.corpus["rotulos"] == av.impressao_rotulos()

    def test_reclassificar_consulta_muda_a_impressao(self, monkeypatch):
        # termo → ausente muda a precisão sem tocar num rótulo: cinco consultas
        # de Investimento foram reclassificadas assim em 24/09/2026
        from avaliacao import rodar as av

        monkeypatch.setattr(av, "MATERIAS", {1: {"consultas": [("q", "termo")]}})
        antes = av.impressao_rotulos()
        monkeypatch.setattr(av, "MATERIAS", {1: {"consultas": [("q", "ausente")]}})
        assert av.impressao_rotulos() != antes


class TestConferencia:
    """A régua é do assistente; a conferência é do usuário. Amostra
    estratificada e cega, kappa de Cohen no fim."""

    def _rot(self):
        return {"consulta": {f"q{i}": {f"a{i}": "s", f"b{i}": "n"} for i in range(30)},
                "documento": {f"t{i}": {f"c{i}": "s", f"d{i}": "n"} for i in range(30)}}

    def test_amostra_estratificada_e_cega(self):
        from avaliacao.conferir import amostra

        rot = self._rot()
        a = amostra(rot, por_estrato=5)
        assert len(a) == 20
        assert all(set(p) == {"tarefa", "item", "sha"} for p in a)   # sem o rótulo
        estratos = {}
        for p in a:
            v = rot[p["tarefa"]][p["item"]][p["sha"]]
            estratos[(p["tarefa"], v)] = estratos.get((p["tarefa"], v), 0) + 1
        assert estratos == {(t, v): 5 for t in ("consulta", "documento") for v in "sn"}

    def test_kappa(self):
        from avaliacao.conferir import kappa

        assert kappa([("s", "s"), ("n", "n")] * 5) == 1.0
        # concordar só pelo acaso dá zero, não 50%
        assert kappa([("s", "s"), ("s", "n"), ("n", "s"), ("n", "n")]) == 0.0
        assert kappa([("s", "n"), ("n", "s")] * 3) < 0

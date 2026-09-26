"""O vetor tem de ver o trecho inteiro, e índice e consulta, o mesmo modelo.

Medido em 24/09/2026: o MiniLM corta a entrada em 128 tokens e 72% dos
trechos passavam disso — o lado semântico da busca não via 49% do texto. E
como o Fênix e o perfilador compartilham este motor, trocar o modelo de um
não pode mexer no outro: o perfilador calibrou limiar absoluto no MiniLM.

Os testes usam um embedder falso (saco de palavras com hash) atrás de um
tokenizer de verdade que corta em 40 tokens, igual o modelo real corta em 128:
o que se testa é o encanamento, não a qualidade do modelo.
"""

from __future__ import annotations

import hashlib

import numpy as np
import pytest

import vetor

LIMITE = 40
DIM = 64


def _tokenizer(limite=LIMITE):
    from tokenizers import Tokenizer, models, pre_tokenizers

    tk = Tokenizer(models.WordLevel(vocab={"[UNK]": 0}, unk_token="[UNK]"))
    tk.pre_tokenizer = pre_tokenizers.Whitespace()
    tk.enable_truncation(max_length=limite)
    return tk


class _Interno:
    def __init__(self):
        self.tokenizer = _tokenizer()


class ModeloFalso:
    """Corta no limite como o modelo real e embute o que sobrou."""

    def __init__(self, dim=DIM):
        self.model = _Interno()
        self.dim = dim
        self.vistos: list[str] = []
        self.lotes: list[int] = []

    def _um(self, texto):
        self.vistos.append(texto)
        enc = self.model.tokenizer.encode(texto)
        v = np.zeros(self.dim, dtype=np.float32)
        for a, b in enc.offsets:
            palavra = texto[a:b].lower()
            v[int(hashlib.md5(palavra.encode()).hexdigest(), 16) % self.dim] += 1
        return v

    def embed(self, textos, batch_size=256, **_):
        self.lotes.append(batch_size)
        return [self._um(t) for t in textos]

    query_embed = passage_embed = embed


@pytest.fixture()
def falso(monkeypatch):
    modelos: dict[str, ModeloFalso] = {}

    def carregar(m=None):
        m = m or vetor.MODELOS[vetor.PADRAO]
        # dimensão diferente por modelo: vetor de um modelo nunca se compara
        # por engano com o de outro
        return modelos.setdefault(m.nome, ModeloFalso(dim=m.dim if m.dim < 512 else 96))

    monkeypatch.setattr(vetor, "_carregar", carregar)
    monkeypatch.setattr(vetor, "_tokenizers", {})
    vetor._consulta_em_cache.cache_clear()
    yield modelos
    vetor._consulta_em_cache.cache_clear()


def _config(con, chave, valor):
    con.execute("INSERT OR REPLACE INTO config_busca (chave, valor) VALUES (?,?)",
                (chave, valor))
    con.commit()


def _cos(a: bytes, b: bytes) -> float:
    return float(np.frombuffer(a, dtype=np.float32) @ np.frombuffer(b, dtype=np.float32))


class TestModeloPorBanco:
    def test_sem_configuracao_vale_o_padrao_historico(self, con):
        # É o caso do perfil.db: nada muda para quem não escolheu.
        assert vetor.modelo(con).apelido == "minilm"
        assert vetor.chave(con) == vetor.chave_conteudo(con)
        assert "+" not in vetor.chave(con).split("@")[1].replace("fastembed", "")

    def test_o_banco_escolhe_o_modelo(self, con, tmp_path):
        import db

        _config(con, "embedding", "e5-large")
        assert vetor.modelo(con).apelido == "e5-large"
        outro = db.conectar(tmp_path / "outro.db")
        db.init_db(outro)
        assert vetor.modelo(outro).apelido == "minilm", "a escolha vazou de banco"

    def test_apelido_desconhecido_cai_no_padrao(self, con):
        _config(con, "embedding", "modelo-que-saiu-do-codigo")
        assert vetor.modelo(con).apelido == "minilm"

    def test_banco_sem_a_tabela_nao_quebra(self, tmp_path):
        import sqlite3

        c = sqlite3.connect(tmp_path / "velho.db")
        assert vetor.modelo(c).apelido == "minilm"
        assert vetor.contexto_ligado(c) is False

    def test_contexto_muda_so_a_chave_da_busca(self, con):
        antes = vetor.chave_conteudo(con)
        _config(con, "contexto", "1")
        assert vetor.chave(con) == antes + "+ctx1"
        # o vetor do texto puro é o do grafo; ele não pode mudar com o contexto
        assert vetor.chave_conteudo(con) == antes

    def test_chave_diferente_por_modelo(self, con):
        a = vetor.chave(con)
        _config(con, "embedding", "minilm-janelas")
        assert vetor.chave(con) != a, "janelas e truncado não podem dividir vetor"


class TestJanelas:
    def test_cobrem_o_texto_inteiro_sem_passar_do_limite(self, falso):
        m = vetor.MODELOS["minilm-janelas"]
        inst = vetor._carregar(m)
        texto = " ".join(f"palavra{i}" for i in range(100))
        js = vetor.janelas_de(m, inst, texto)
        assert len(js) == 3
        assert all(n <= LIMITE - 2 for _, n in js)
        assert sum(n for _, n in js) == 100
        assert js[0][0].startswith("palavra0") and js[-1][0].endswith("palavra99")

    def test_texto_curto_e_uma_janela_so(self, falso):
        m = vetor.MODELOS["minilm-janelas"]
        js = vetor.janelas_de(m, vetor._carregar(m), "curto demais")
        assert js == [("curto demais", 2)]

    def test_com_janelas_o_vetor_ve_o_fim_do_trecho(self, falso):
        """O defeito medido: o assunto no fim do trecho não chegava ao vetor."""
        texto = " ".join(["enchimento"] * 60) + " inflação"
        q_trunc = vetor.embutir(["inflação"], vetor.MODELOS["minilm"], tipo="consulta")[0]
        truncado = vetor.embutir([texto], vetor.MODELOS["minilm"])[0]
        q_jan = vetor.embutir(["inflação"], vetor.MODELOS["minilm-janelas"], tipo="consulta")[0]
        janelado = vetor.embutir([texto], vetor.MODELOS["minilm-janelas"])[0]
        assert _cos(q_trunc, truncado) == pytest.approx(0.0)
        assert _cos(q_jan, janelado) > 0.0

    def test_media_ponderada_pelo_tamanho_da_janela(self, falso):
        # 38 tokens de "a" e uma sobra de 2 de "b": a sobra não pode pesar o
        # mesmo que a janela cheia
        m = vetor.MODELOS["minilm-janelas"]
        v = np.frombuffer(vetor.embutir([" ".join(["aaa"] * 38 + ["bbb"] * 2)], m)[0],
                          dtype=np.float32)
        qa = np.frombuffer(vetor.embutir(["aaa"], m, tipo="consulta")[0], dtype=np.float32)
        qb = np.frombuffer(vetor.embutir(["bbb"], m, tipo="consulta")[0], dtype=np.float32)
        assert float(v @ qa) > 5 * float(v @ qb)


class TestChamadaCerta:
    def test_e5_recebe_os_prefixos(self, falso):
        m = vetor.MODELOS["e5-large"]
        vetor.embutir(["juros"], m, tipo="consulta")
        vetor.embutir(["juros reais"], m)
        vistos = falso[m.nome].vistos
        assert "query: juros" in vistos
        assert "passage: juros reais" in vistos

    def test_cache_da_consulta_separa_modelos(self, con, falso, tmp_path):
        import db

        a = vetor.embutir_um("choque de oferta", con)
        outro = db.conectar(tmp_path / "e5.db")
        db.init_db(outro)
        _config(outro, "embedding", "e5-large")
        b = vetor.embutir_um("choque de oferta", outro)
        # mesma consulta, modelos diferentes: o cache não pode servir o vetor
        # do primeiro para o segundo
        assert len(a) != len(b)


class TestMemoria:
    def test_modelo_grande_embute_em_lote_pequeno(self, falso):
        """Com o lote padrão (256) o e5 passou de 10 GB e travou a máquina."""
        m = vetor.MODELOS["e5-large"]
        vetor.embutir(["um trecho qualquer"], m)
        assert falso[m.nome].lotes == [m.lote]
        assert m.lote <= 16

    def test_vetores_ja_feitos_sobrevivem_a_falha_no_meio(self, con, falso, monkeypatch):
        from pathlib import Path

        import db
        import extract

        con.execute("""INSERT INTO arquivos (id, site, courseid, nome, fileurl, ignorado)
                       VALUES (1, '20262', 6095, 'a.pdf', 'http://x/1', 0)""")
        paginas = "\n".join(f"--- página {i} ---\ntrecho número {i} " + "conteúdo " * 10
                            for i in range(1, 7))
        extract.indexar(con, 1, "a.pdf", paginas)
        con.execute("DELETE FROM vetores")       # como depois de trocar de modelo
        con.commit()

        monkeypatch.setattr(extract, "BLOCO_VETORES", 2)
        chamadas = {"n": 0}
        original = vetor.embutir

        def cai_no_terceiro_bloco(textos, m=None, tipo="passagem"):
            chamadas["n"] += 1
            if chamadas["n"] == 3:
                raise KeyboardInterrupt      # queda no meio da rodada
            return original(textos, m, tipo)

        monkeypatch.setattr(vetor, "embutir", cai_no_terceiro_bloco)
        with pytest.raises(KeyboardInterrupt):
            extract.completar_vetores(con)
        con.rollback()
        caminho = Path(con.execute("PRAGMA database_list").fetchone()[2])
        outro = db.conectar(caminho)
        # dois blocos de 2 foram gravados antes da queda; o terceiro não
        assert outro.execute("SELECT COUNT(*) FROM vetores").fetchone()[0] == 4
        outro.close()


class TestIndexacaoComContexto:
    def _arquivo(self, con, aid, nome, secao):
        con.execute(
            """INSERT INTO arquivos (id, site, courseid, nome, secao, fileurl, ignorado)
               VALUES (?,?,?,?,?,?,0)""",
            (aid, "20262", 6095, nome, secao, f"http://x/{aid}"))

    def test_sem_contexto_um_vetor_por_trecho(self, con, falso):
        import extract

        self._arquivo(con, 1, "Aula 3 - Solow.pdf", "Semana 2")
        extract.indexar(con, 1, "Aula 3 - Solow.pdf", "--- página 1 ---\nCrescimento de longo prazo, poupança e acumulação de capital")
        assert {r[0] for r in con.execute("SELECT modelo FROM vetores")} == {vetor.chave(con)}

    def test_com_contexto_dois_vetores_e_o_sha_nao_muda(self, con, falso):
        import extract

        _config(con, "contexto", "1")
        self._arquivo(con, 1, "Aula 3 - Solow.pdf", "Semana 2")
        texto = "Crescimento de longo prazo, poupança e acumulação de capital"
        extract.indexar(con, 1, "Aula 3 - Solow.pdf", f"--- página 1 ---\n{texto}")
        t = con.execute("SELECT sha256, texto FROM trechos").fetchone()
        # invariante 6: o sha é do TEXTO do trecho; o contexto não entra nele
        assert t["sha256"] == hashlib.sha256(texto.encode()).hexdigest()
        assert t["texto"] == texto
        modelos = {r[0] for r in con.execute("SELECT modelo FROM vetores")}
        assert modelos == {vetor.chave_conteudo(con), vetor.chave(con)}
        fts = con.execute("SELECT nome FROM trechos_fts").fetchone()[0]
        assert "Semana 2" in fts and "Aula 3 - Solow" in fts

    def test_contexto_chega_ao_vetor_da_busca(self, con, falso):
        import extract

        _config(con, "contexto", "1")
        self._arquivo(con, 1, "Solow.pdf", "Crescimento")
        extract.indexar(con, 1, "Solow.pdf", "--- página 1 ---\nA poupança determina o capital por trabalhador no estado estacionário")
        sha = con.execute("SELECT sha256 FROM trechos").fetchone()[0]
        v = dict(con.execute("SELECT modelo, vetor FROM vetores WHERE sha256 = ?", (sha,)))
        q = vetor.embutir(["Crescimento"], vetor.modelo(con), tipo="consulta")[0]
        assert _cos(q, v[vetor.chave(con)]) > 0.0
        assert _cos(q, v[vetor.chave_conteudo(con)]) == pytest.approx(0.0)

    def test_grafo_acha_conteudo_repetido_mesmo_com_contexto(self, con, falso):
        """O grafo compara TEXTO: com o contexto do arquivo na frente, a mesma
        tabela republicada em dois arquivos deixaria de parecer igual."""
        import extract
        import grafo

        _config(con, "contexto", "1")
        tabela = "PIB per capita Brasil 1950 1980 2000 crescimento anual médio da tabela"
        self._arquivo(con, 1, "Aula sobre fatos estilizados do crescimento.pdf", "Semana 1")
        self._arquivo(con, 2, "Leitura complementar Maddison dados históricos.pdf", "Semana 4")
        extract.indexar(con, 1, "Aula sobre fatos estilizados do crescimento.pdf",
                        f"--- página 1 ---\n{tabela}")
        # quase igual, não igual: com o MESMO texto os dois teriam o mesmo sha e
        # dividiriam o vetor, e o teste passaria mesmo se o grafo olhasse o
        # vetor com contexto — passaria pelo motivo errado
        extract.indexar(con, 2, "Leitura complementar Maddison dados históricos.pdf",
                        f"--- página 1 ---\n{tabela} revisada")
        shas = {r[0] for r in con.execute("SELECT sha256 FROM trechos")}
        assert len(shas) == 2
        grafo.construir(con, 6095)
        tipos = {r[0] for r in con.execute("SELECT tipo FROM grafo_documentos")}
        assert "conteudo" in tipos


class TestContextoDoTrecho:
    def test_arquivo_secao_e_titulo(self):
        import extract

        c = extract.contexto_do_trecho("Aula 5 - IS-LM.pdf", "Semana 3", "Choque monetário",
                                       "a curva LM desloca")
        assert c == "Aula 5 - IS-LM — Semana 3\nChoque monetário"

    def test_titulo_que_o_trecho_ja_abre_nao_se_repete(self):
        import extract

        c = extract.contexto_do_trecho("x.pdf", "", "Choque", "Choque\ncorpo")
        assert c == "x"

    def test_secao_contida_no_nome_nao_se_repete(self):
        import extract

        assert extract.contexto_do_trecho("Avisos — prova.pdf", "Avisos", "", "t") == "Avisos — prova"


class TestLimiarDoModelo:
    def test_aviso_usa_o_limiar_do_modelo_do_banco(self, con, monkeypatch):
        import busca

        achados = [{"score": 0.70}]
        monkeypatch.setattr(busca, "_df_minimo", lambda *a: 0)
        # no MiniLM 0,70 é topo forte: sem aviso
        assert busca.aviso_cobertura(con, "algo raro", achados) == ""
        # num modelo cuja escala é mais alta, o mesmo 0,70 é fraco
        import dataclasses

        alto = dataclasses.replace(vetor.MODELOS["e5-large"], score_fraco=0.80)
        monkeypatch.setitem(vetor.MODELOS, "e5-large", alto)
        _config(con, "embedding", "e5-large")
        assert busca.aviso_cobertura(con, "algo raro", achados) != ""

    def test_piso_do_modo_vetorial_e_do_modelo(self, con, falso, monkeypatch):
        import dataclasses

        import busca
        import extract

        con.execute("""INSERT INTO arquivos (id, site, courseid, nome, fileurl, ignorado)
                       VALUES (1, '20262', 6095, 'a.pdf', 'http://x/1', 0)""")
        extract.indexar(con, 1, "a.pdf", "--- página 1 ---\n" + " ".join(["juros"] * 12))  # cosseno 1,0 no falso
        assert busca._vetorial(con, "juros", 6095, None), "sem piso alto, acha"
        # vetor.modelo() lê o dicionário a cada chamada: trocar a entrada troca o piso
        monkeypatch.setitem(vetor.MODELOS, "minilm",
                            dataclasses.replace(vetor.MODELOS["minilm"], piso_cosseno=1.01))
        assert busca._vetorial(con, "juros", 6095, None) == []


class TestEscalaDoModelo:
    """Margem e bônus são DISTÂNCIAS de cosseno: valem na escala do modelo.
    Sem isso o e5, que espreme tudo numa faixa 5x mais estreita, foi julgado
    com a régua do MiniLM (24/09/2026)."""

    def _dois_trechos(self, con):
        import extract

        con.execute("""INSERT INTO arquivos (id, site, courseid, nome, fileurl, ignorado)
                       VALUES (1, '20262', 6095, 'a.pdf', 'http://x/1', 0)""")
        a = " ".join(["juros"] * 40)                       # cosseno 1,00
        # o falso corta em 40 tokens como o modelo real corta: a diferença tem
        # de estar no começo — 8 "outra" + 32 "juros" dá cosseno ~0,97
        b = " ".join(["outra"] * 8 + ["juros"] * 40)
        extract.indexar(con, 1, "a.pdf", f"--- página 1 ---\n{a}\n--- página 2 ---\n{b}")

    def test_margem_encolhe_com_a_escala(self, con, falso, monkeypatch):
        import dataclasses

        import busca

        self._dois_trechos(con)
        # na régua do MiniLM (margem 0,10), 0,05 de diferença passa
        assert len(busca._ancora(con, "juros", 6095, None, None, False)) == 2
        # num modelo 5x mais estreito a mesma diferença é grande: corta
        monkeypatch.setitem(vetor.MODELOS, "minilm",
                            dataclasses.replace(vetor.MODELOS["minilm"], escala=0.2))
        assert len(busca._ancora(con, "juros", 6095, None, None, False)) == 1

    def test_bonus_da_ancora_encolhe_com_a_escala(self, con, falso, monkeypatch):
        import dataclasses

        import busca

        monkeypatch.setitem(vetor.MODELOS, "minilm",
                            dataclasses.replace(vetor.MODELOS["minilm"], escala=0.2))
        self._dois_trechos(con)
        topo = busca._ancora(con, "juros", 6095, None, None, False)[0][0]
        assert topo == pytest.approx(1.0 + busca.BONUS_ANCORA * 0.2, abs=1e-6)


class TestUniaoNoModoVetorial:
    """O modo vetorial descartava a âncora inteira: "risco de inadimplência"
    estava literal em 12 trechos de Investimento e nenhum voltava (24/09/2026)."""

    def _corpus(self, con, monkeypatch):
        import busca
        import extract
        import grafo

        con.execute("""INSERT INTO arquivos (id, site, courseid, nome, fileurl, ignorado)
                       VALUES (1, '20262', 8025, 'a.pdf', 'http://x/1', 0)""")
        # página 1: o termo literal diluído em 38 palavras — cosseno baixo,
        # abaixo do piso; página 2: só "risco", cosseno alto sem o termo raro
        diluido = "risco inadimplência " + " ".join(f"enchimento{i}" for i in range(38))
        extract.indexar(con, 1, "a.pdf", f"--- página 1 ---\n{diluido}\n"
                                         f"--- página 2 ---\n{' '.join(['risco'] * 40)}")
        monkeypatch.setattr(grafo, "decidir", lambda *a, **k: ("vetorial", None, {}))
        return busca

    def test_ancora_AND_entra_no_modo_vetorial(self, con, falso, monkeypatch):
        busca = self._corpus(con, monkeypatch)
        monkeypatch.setattr(busca, "UNIR_ANCORA", False)
        assert [x["pagina"] for x in busca.buscar(con, "risco inadimplência", 8025)] == [2]
        monkeypatch.setattr(busca, "UNIR_ANCORA", True)
        assert 1 in [x["pagina"] for x in busca.buscar(con, "risco inadimplência", 8025)]

    def test_ancora_OR_nao_entra(self, con, falso, monkeypatch):
        # "blablabla" não existe: o AND falha, o OR casa "risco" sozinho — e
        # uma palavra comum sozinha é o ruído que a âncora existe para evitar
        busca = self._corpus(con, monkeypatch)
        monkeypatch.setattr(busca, "UNIR_ANCORA", True)
        assert [x["pagina"] for x in busca.buscar(con, "risco blablabla", 8025)] == [2]

"""Conjuntos de consulta, um por matéria.

Cada consulta foi ancorada no vocabulário que o material de fato contém,
medido antes de escrever. O tipo separa o que o teste mede:

  termo    — termo técnico curto, onde o casamento exato é forte
  assunto  — pergunta em linguagem natural, onde o vetor é forte
  ausente  — o assunto existe na disciplina mas só nos PDFs digitalizados,
             que estão marcados `ignorado`. Mede se o sistema sabe dizer
             "não está aqui" em vez de inventar vizinhança.

`CONSULTAS` é para a tarefa pontual (precisão nas 3 primeiras).
`TOPICOS` é para a tarefa de documento (cobrir a matéria).
"""

MATERIAS = {
    9065: {
        "nome": "Economia Regional e Urbana",
        "consultas": [
            ("von Thünen", "termo"),
            ("economias de aglomeração", "termo"),
            ("Christaller lugar central", "termo"),
            ("custo de transporte", "termo"),
            ("Weber localização industrial", "termo"),
            ("externalidades marshallianas", "termo"),
            ("renda da terra", "termo"),
            ("economias de escala", "termo"),
            ("urbanização", "termo"),
            ("por que as empresas se concentram numa mesma região", "assunto"),
            ("o que determina onde uma indústria decide se instalar", "assunto"),
            ("como o custo de transporte influencia a localização da produção", "assunto"),
            ("qual a diferença entre economias internas e externas de aglomeração", "assunto"),
            ("o que explica a formação de uma hierarquia entre as cidades", "assunto"),
            ("como a urbanização se relaciona com o desenvolvimento regional", "assunto"),
            ("de que forma o espaço afeta as decisões econômicas das firmas", "assunto"),
            # Estas três eram "ausente" (só nos PDFs digitalizados) até o texto
            # de teorias do desenvolvimento regional ser indexado: a rotulagem
            # cega de 24/09/2026 achou Perroux, Becattini e Hirschman
            # ensinados no material. Marcadas ausentes, elas contavam como
            # acerto do aviso de cobertura quando ele disparava à toa.
            ("polos de crescimento de Perroux", "termo"),
            ("distrito industrial marshalliano", "termo"),
            ("encadeamentos para frente e para trás", "termo"),
            ("espaço econômico de Boudeville", "ausente"),
        ],
        "topicos": [
            "espaço e região na economia, conceitos básicos",
            "produção social do espaço",
            "teorias clássicas da localização, escola alemã",
            "von Thünen, renda da terra e uso do solo agrícola",
            "Weber e a minimização do custo de transporte",
            "Christaller, lugares centrais e áreas de mercado",
            "Lösch e o cone de demanda",
            "externalidades e economias de aglomeração",
            "economias de localização e economias de urbanização",
            "hierarquia urbana e tamanho das cidades",
            "geografia econômica contemporânea e escalas",
            "desenvolvimento regional e crescimento urbano",
        ],
    },
    6095: {
        "nome": "Macroeconomia III",
        "consultas": [
            ("estado estacionário", "termo"),
            ("regra de ouro do estoque de capital", "termo"),
            ("contabilidade do crescimento", "termo"),
            ("por que a poupança não sustenta o crescimento no longo prazo", "assunto"),
            ("o que a PTF mede e como ela é calculada", "assunto"),
            # Ampliado em 24/09/2026: com 5 consultas Macro não pesava na
            # calibração. Vocabulário conferido nos slides (Solow, AK, Romer,
            # Jones, Aghion-Howitt, contas nacionais).
            ("modelo AK", "termo"),
            ("não-rivalidade das ideias", "termo"),
            ("destruição criativa", "termo"),
            ("ótica da demanda do PIB", "termo"),
            ("crescimento populacional no modelo de Solow", "termo"),
            ("por que o modelo de Romer gera crescimento sustentado", "assunto"),
            ("qual a diferença entre efeito nível e efeito taxa das políticas de P&D", "assunto"),
            ("como a taxa de juros equilibra poupança e investimento", "assunto"),
            ("como a renda nacional se divide entre capital e trabalho", "assunto"),
            ("teoria dos ciclos reais de negócios", "ausente"),
        ],
        "topicos": [
            "dados em macroeconomia e contas nacionais",
            "modelo clássico: renda nacional e sua distribuição",
            "demanda por bens e serviços, poupança e investimento",
            "determinação da taxa de juros no modelo clássico",
            "modelo de Solow: acumulação de capital",
            "modelo de Solow: crescimento populacional",
            "modelo de Solow: progresso tecnológico",
            "regra de ouro do nível de capital",
            "contabilidade do crescimento e produtividade total dos fatores",
            "PTF e acumulação de capital no Brasil",
            "crescimento endógeno e a economia das ideias",
            "modelo de Romer e retornos crescentes",
        ],
    },
    8025: {
        "nome": "Investimento e Financiamento",
        "consultas": [
            # Cinco consultas perguntavam por aula que ainda não aconteceu e
            # pesavam como falha da busca: o programa põe VPL e critérios de
            # investimento em 30/09 e 05/10 (Ross cap. 9) e custo de capital em
            # 09/11 (cap. 13-14). Até lá o material só MENCIONA — é o caso do
            # aviso de cobertura. Volte-as para termo/assunto depois dessas aulas.
            ("valor presente líquido", "ausente"),
            ("taxa interna de retorno", "termo"),
            ("custo de capital", "ausente"),
            ("debênture incentivada", "termo"),
            ("estrutura a termo da taxa de juros", "termo"),
            ("alavancagem financeira", "termo"),
            ("fluxo de caixa livre", "ausente"),
            # literal em 12 trechos e a busca não devolvia nenhum: o modo
            # vetorial descartava a âncora (busca.UNIR_ANCORA)
            ("risco de inadimplência", "termo"),
            ("como a empresa decide entre capital próprio e capital de terceiros", "assunto"),
            ("por que o risco de um título afeta a taxa que o investidor exige", "assunto"),
            ("o que faz o preço de um título cair quando a taxa de juros sobe", "assunto"),
            ("como se avalia se um projeto de investimento vale a pena", "ausente"),
            ("o que o investidor ganha ao diversificar a carteira", "ausente"),
            ("período de payback descontado", "ausente"),
            ("duration de Macaulay", "ausente"),
        ],
        "topicos": [
            "introdução às finanças corporativas",
            "demonstrações contábeis e fluxos de caixa",
            "análise das demonstrações financeiras e índices",
            "valor do dinheiro no tempo e juros compostos",
            "valor presente líquido e critérios de decisão de investimento",
            "risco e retorno de um investimento",
            "custo de capital da empresa",
            "títulos de dívida e precificação de debêntures",
            "estrutura a termo da taxa de juros",
            "títulos públicos e dívida pública federal",
            "renda variável, ações e dividendos",
            "estrutura de capital e alavancagem",
        ],
    },
    # Micro II e Econometria entraram em 24/09/2026: até então duas das cinco
    # matérias não eram medidas por ninguém. Consultas escritas a partir do
    # vocabulário dos slides; as "ausente" foram conferidas no FTS (zero
    # ocorrência no material indexado) e evitam assunto coberto com outra
    # grafia ("teoremas de Bem-estar", "heterocedástico").
    6738: {
        "nome": "Microeconomia II",
        "consultas": [
            ("utilidade esperada", "termo"),
            ("aversão ao risco", "termo"),
            ("paradoxo de São Petersburgo", "termo"),
            ("paradoxo de Ellsberg", "termo"),
            ("excedente do consumidor", "termo"),
            ("caixa de Edgeworth", "termo"),
            ("equilíbrio walrasiano", "termo"),
            ("lei de Walras", "termo"),
            ("ótimo de Pareto", "termo"),
            ("por que o consumidor avesso ao risco compra seguro", "assunto"),
            ("como a diversificação reduz o risco", "assunto"),
            ("qual o efeito de um imposto sobre o bem-estar do mercado", "assunto"),
            ("o que acontece com o excedente quando o governo fixa um preço mínimo", "assunto"),
            ("como os preços levam uma economia de trocas ao equilíbrio", "assunto"),
            ("por que o equilíbrio competitivo é eficiente", "assunto"),
            ("teorema da impossibilidade de Arrow", "ausente"),
            ("seleção adversa e risco moral", "ausente"),
        ],
        "topicos": [
            "escolhas sob incerteza e utilidade esperada",
            "preferências em relação ao risco",
            "paradoxos da utilidade esperada",
            "diversificação e demanda por ativos de risco",
            "excedente do consumidor e do produtor",
            "impostos, subsídios e controle de preços",
            "equilíbrio de trocas na caixa de Edgeworth",
            "equilíbrio walrasiano e lei de Walras",
            "eficiência de Pareto e teoremas do bem-estar",
            "modelo de uma firma e um consumidor",
        ],
    },
    8268: {
        "nome": "Econometria I",
        "consultas": [
            ("mínimos quadrados ordinários", "termo"),
            ("média condicional zero", "termo"),
            ("coeficiente de determinação", "termo"),
            ("homocedasticidade", "termo"),
            ("viés de variável omitida", "termo"),
            ("multicolinearidade", "termo"),
            ("teorema de Gauss-Markov", "termo"),
            ("teste t", "termo"),
            ("teste F", "termo"),
            ("p-valor", "termo"),
            ("consistência do estimador", "termo"),
            ("o que significa ceteris paribus em um modelo econométrico", "assunto"),
            ("como interpretar o coeficiente de um modelo log-log", "assunto"),
            ("o que acontece se eu omitir uma variável relevante da regressão", "assunto"),
            ("por que variáveis explicativas correlacionadas aumentam a variância do estimador", "assunto"),
            ("como decidir se um coeficiente é estatisticamente significante", "assunto"),
            ("como testar se várias variáveis são conjuntamente significantes", "assunto"),
            ("modelo logit e probit", "ausente"),
            ("modelo de probabilidade linear", "ausente"),
        ],
        "topicos": [
            "introdução à econometria e dados econômicos",
            "modelo de regressão linear simples",
            "estimadores de MQO e método de momentos",
            "qualidade do ajuste e R2",
            "formas funcionais e modelos logarítmicos",
            "hipóteses do modelo linear e ausência de viés",
            "variância dos estimadores de MQO",
            "regressão múltipla e efeito parcial",
            "viés de variável omitida e multicolinearidade",
            "teste t e intervalo de confiança",
            "teste F de restrições múltiplas",
            "consistência e propriedades assintóticas",
        ],
    },
}

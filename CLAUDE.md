# Projeto Fênix — UFMG Moodle Companion

Assistente pessoal para o UFMG Virtual: sincroniza materiais, monta o
calendário real da disciplina e ajuda a estudar a partir do próprio material.

## Versões

Esta é a versão para colegas da UFMG. O núcleo — sync, extração, busca,
servidor MCP, CLI, skill e subagente — roda em macOS, Linux e Windows. Dois
extras são só do macOS, porque falam com apps do sistema por AppleScript:

- **agenda** (`agenda_mac.py`): provas e blocos de estudo no Calendário do Mac.
  Fora dele, o servidor esconde essas ferramentas (`server.SO_MAC`) e
  `exportar_calendario` gera um `.ics`;
- **e-mail** (`correio.py`, `server_correio.py`): pelo Mail.app. Fora do
  macOS, não registre o servidor `ufmg-correio`.

O sync agendado (`agendar.py`) usa o launchd; no Linux e no Windows,
`cli.py agendar` imprime a linha de cron ou de `schtasks` equivalente.

No Windows o interpretador é `.venv\Scripts\python` em vez de
`.venv/bin/python`.

## Antes de responder sobre a faculdade

**Use a skill `ufmg-companion`.** Ela tem o roteador (qual ferramenta, em que
ordem, quando parar) e as regras de economia de token. Perguntas sobre matéria,
prova, nota, prazo, slide ou questionário passam por ela.

Para ler material que exija mais de um ou dois arquivos, delegue ao subagente
`pesquisador-material` — ele lê no contexto dele e devolve síntese curta.

## Rodar

O interpretador é `.venv/bin/python`. O Python do sistema **não** tem as
dependências.

```bash
.venv/bin/python cli.py sync && .venv/bin/python cli.py extrair
.venv/bin/python diagnostico.py     # token ainda vale? o que ele libera?
```

Dados ficam em `~/.ufmg-moodle-mcp/` (banco, materiais, token, sessões), nunca
no repositório.

## Invariantes — quebrar qualquer uma é regressão

Cada uma existe por um motivo específico, e todas as sete têm teste em
`testes/test_invariantes.py` (`.venv/bin/python -m pytest testes/ -q`).

Os testes foram conferidos por mutação: quebrar a invariante no código tem de
derrubar teste. Se você mexer neles, refaça essa conferência — um teste que
passa pelo motivo errado é pior que teste nenhum, porque dá licença. O de
precedência de evento passava assim: o caso que ele cobria já era barrado pelo
peso da origem, e a guarda de `confirmado` podia ser removida sem quebrar
nada. O caso que morde é evento de `programa_pdf` confirmado na mão e depois
contrariado pelo calendário, que tem peso maior.

Rode a conferência **sem bytecode** (`python -B`, `PYTHONDONTWRITEBYTECODE=1`).
O Python só recompila quando muda a data (em segundos) ou o tamanho do
arquivo: mutação do mesmo tamanho (`days=1` → `days=2`), restaurada no mesmo
segundo, deixou o compilado MUTADO valendo para o original, e as mutações
seguintes "derrubaram" teste pelo motivo errado (27/09).

1. **Escrita ensaia por padrão.** `confirmar=True` só quando o usuário mandar
   enviar naquela mensagem. Pedir para redigir não é pedir para publicar.
   Toda tentativa, inclusive falha, vai para `log_escrita`. Vale para o Moodle
   e para e-mail — no e-mail o ensaio é um rascunho aberto no Mail.app.
   Destinatário não confirmado é recusado; não contorne por conta própria.
2. **Evento confirmado nunca é alterado por fonte automática**, e fonte de peso
   menor não sobrescreve maior (`PESO_ORIGEM` em `sync.py`).
3. **`programa.py` nunca confirma sozinho.** Datas lidas de tabela em PDF
   entram com `confirmado=0` e o trecho literal visível.
4. **`companion.pode_enviar()`** decide envio de questionário, consultando a
   política declarada. Não contorne: se bloquear, mostre o gabarito e explique.
5. **`memoria.VOLATIL`** recusa memorizar nota, prazo e status. O Moodle é
   autoridade sobre eles; cache ali faria a ferramenta mentir com confiança.
6. **Cobertura é chaveada por sha de trecho, nunca por id.** `trechos.id` é
   reciclado a cada `extrair --reindexar` — a tabela é apagada e reinserida.
   Isso já invalidou 172 referências de uma vez. `vetores` e `cobertura` usam
   o sha do texto pelo mesmo motivo; qualquer registro novo deve usar também.
7. **Informação derivada carrega a fonte.** `fonte_trecho` nas questões,
   `trecho_origem` nos eventos, `payload` nas escritas. Ao responder sobre a
   matéria, cite arquivo **e página** — `busca.py` devolve as duas coisas em
   todo resultado, justamente para isso. Sem fonte no material, diga isso.

## Convenções

- Tudo em português: nome de coluna, comando, saída, docstring.
- Comentário explica **por quê**, não o quê. Vários comentários no código
  registram armadilhas encontradas contra o Moodle real — não os remova.
- Erro em lote nunca derruba a rodada (extração, sync): vai para a lista de
  erros do resultado.

## Custo de token é requisito, não detalhe

`preparar_questionario` já devolveu 26k tokens numa chamada. Hoje devolve ~1k,
e o detalhe sai de `questao_detalhe` só nos slots pedidos. Não "melhore"
devolvendo o JSON completo de novo.

Mesma ideia em `briefing()` (uma chamada no lugar de quatro) e
`memoria_contexto()` (~200 tokens que evitam uma resolução por conversa).

`buscar_material` tem orçamento declarado (`ORCAMENTO_CHARS` em `busca.py`) e
sai em ~700-1100 tokens, com o cabeçalho da citação contado dentro. O degrau
seguinte é `trecho_material` (uma página e as vizinhas), não `texto_material`,
que traz o PDF inteiro por 20k — a escada existe para que esse último caso
fique raro.

**O índice mais barato é o que não é chamado.** Cada fluxo da skill carrega as
assinaturas das ferramentas que ele usa. A skill já está carregada e já foi
paga; `indice` custa 167 tokens E um turno, e um turno reenvia o histórico
inteiro. São +268 tokens uma vez no SKILL.md contra milhares por ida evitada.
`cli.py skill` confere que o cardápio não envelheceu.

**`indice` é semântico.** Ele ranqueia as 60 frias por cosseno contra o pedido
em linguagem natural e devolve **5**, não 46: `indice("ver o que errei na
prova")` custa 167 tokens e traz as de questionário. Cinco e não quarenta por
medição de terceiros: com K grande a recuperação acerta quase sempre mas a
acurácia do modelo cai de 10 a 16 pontos e o custo sobe dez vezes (arXiv
2605.18857). `indice(tudo=True)` ainda lista todas, e custa 1166.

**Reaproveitar vale mais que buscar melhor.** Medido: saber que um documento
já existe economiza de 60% a 92%; escolher o melhor método de busca economiza
21%. `cobrir_topico(documento=...)` registra o que devolveu e `novidade=True`
traz só o que falta. O registro é feito pela ferramenta, não pelo modelo:
pedir contabilidade ao modelo custa token e ele esquece.

**Decisão que a matemática resolve não gasta token de deliberação.**
`grafo.decidir()` escolhe entre modo âncora e modo vetorial por uma razão
entre conjuntos de documentos, em Python, antes de recuperar qualquer coisa.
Não passe essa escolha para a skill: ela custaria contexto a cada pergunta
para decidir o que uma divisão resolve. Vale como regra geral aqui.

**O esquema das ferramentas entra no contexto a cada mensagem.** Com 45
expostas custava ~6,4k tokens sempre — mais que qualquer chamada isolada.
Hoje são **4 tipadas = ~300 tokens**, e as outras 60 saem por `indice` +
`executar`. O conjunto quente é **medido, não escolhido**: simulando três
conversas reais, com o histórico sendo reenviado a cada turno,

| configuração | esquema | custo somado |
|---|---|---|
| 9 tipadas | 742 | 89.813 |
| 2 tipadas (tudo frio) | 209 | 102.398 |
| 4 tipadas | 332 | **79.973** |

"Tudo frio" **perde**: cada ferramenta fora do prefixo custa uma ida ao índice,
e um turno a mais reenvia todo o histórico acumulado. Economizar 533 tokens por
mensagem não paga um turno que custa milhares. Antes de mexer no conjunto
quente, refaça essa conta. Ao acrescentar ferramenta:

- decore com `@fria()`. `@mcp.tool()` só se ela entrar em quase toda conversa
  E for barata — hoje só `buscar_material` e `memoria_contexto` passam nesse
  teste, e passar é decidido pela conta acima, não por impressão;
- marque em `_PERIGOSAS` se escreve em algo externo;
- descrição diz o que faz e o que é irreversível, nunca o porquê (isso é da
  skill), e a primeira linha vira a entrada do índice;
- parâmetro opcional usa sentinela vazia (`site: str = ""`), não
  `str | None = None`, que vira `anyOf` de dois ramos e custa o dobro;
- **atualize o fluxo da skill e rode `cli.py skill --refazer`** — cada fluxo
  carrega o cardápio de assinaturas das ferramentas que ele usa, para o modelo
  não precisar chamar `indice`. Assinatura copiada envelhece: `cli.py skill`
  confere contra o código e acusa o que ficou para trás.

## Mapa

| Arquivo | Papel |
|---|---|
| `moodle_client.py` | única porta para a API REST (call, download, upload); IPv4 forçado, token no chaveiro |
| `sync.py` | turmas, materiais, calendário, avisos, mudanças e conclusão → banco |
| `extract.py` | PDF/PPTX/DOCX → texto, fatiado em trechos e indexado |
| `busca.py` | léxico acha onde, grafo até onde, vetor qual; com citação |
| `avaliacao/` | consultas, rótulos por sha e `cli.py avaliar` — mede antes de opinar |
| `grafo.py` | grafo entre documentos e a decisão de modo, calculada |
| `vetor.py` | embedding local dos trechos; opcional, degrada sozinho |
| `escrita.py` | fronteira de escrita; tudo passa por `_executar()` |
| `companion.py` | revisão de questionário; parser do HTML do Moodle |
| `simulado.py` | questões a partir do material; recusa questão sem fonte |
| `programa.py` | datas de prova do plano de ensino |
| `memoria.py` | apelidos e resumos por sha256 |
| `correio.py` | e-mail pelo Mail.app; sem credencial |
| `apostila.py` | partes HTML → PDF via chromium do playwright; lê de volta as citações |
| `calendario.py` | eventos → .ics; só o confirmado, pela invariante 2 |
| `agenda_mac.py` | Calendário do Mac: provas confirmadas e blocos de estudo na agenda; horário livre; só mexe no que tem a marca `fenix://` |
| `agendar.py` | agente launchd do sync; `--rodar-sync` é o que ele executa |
| `server.py` | MCP: 4 tipadas + índice semântico sobre 60, 5 prompts |
| `server_correio.py` | MCP separado de e-mail; registre só se for usar |
| `cli.py` | operação e edição manual |

Detalhe de design em `PROJETO.md`; uso no dia a dia em `GUIA.md`.

## Fragilidades conhecidas

- Nomes de campo do questionário (`q42:1_answer`) saem por regex do HTML
  renderizado — a API não os expõe. Tema do site muda, quebra ali; o sinal é
  `campos` vazio.
- Tentativa finalizada só responde a `get_attempt_review`, não a
  `get_attempt_data`.
- Seis PDFs escaneados estão marcados `ignorado`: fora da busca, e o usuário já
  disse que não importam — não os mencione.
- O lado semântico da busca depende do `fastembed`, que é opcional. Sem ele a
  busca responde só pelo termo, não há grafo entre documentos, e `cli.py
  extrair` diz quantos trechos estão sem vetor.
- **Ruído é rebaixado na busca, mas detectado na indexação.** `trechos.ruido`
  e `trechos.repetido` saem de `extract.pontuar_ruido` e de `grafo.construir`.
  Não reintroduza regex de ruído em `busca.py`: ela rodava a cada consulta e
  não enxergava repetição entre arquivos, que é o sinal mais forte.
- **Capa e cabeçalho envenenavam o grafo.** Em ECN300, trechos de 42 caracteres
  com só o nome do departamento geravam 77 das 78 arestas de conteúdo. Hoje
  `repetido >= 3` os exclui da construção, e o curso caiu de 110 para 31
  arestas. Em Macro o mesmo filtro não muda nada, porque lá o que se repete é
  citação real.
- **Meça antes de afirmar que melhorou.** `cli.py avaliar` roda 86 consultas e
  58 tópicos nas cinco matérias contra 1.332 rótulos cegos (por pooling, como
  no TREC; documento julgado por tópico), grava a rodada e compara com a
  anterior — **só se o corpus e a régua forem os mesmos**. A régua é o sha dos
  rótulos E das consultas: reclassificar uma consulta muda a precisão sem
  mudar rótulo. A comparação traz permutação pareada por consulta; diferença
  sem p < 0,05 é ruído. A rodada grava um sha do
  conjunto de trechos indexados; com sha diferente o relatório se recusa a
  comparar, porque somar mudança de código com mudança de material e chamar o
  total de efeito do código foi exatamente o erro de setembro. A coluna `base`
  diz sobre quantos slots a precisão foi calculada: Regional marcou 84% sobre
  19, com 30 fora da conta por falta de rótulo. `cli.py avaliar --sem-rotulo`
  lista o que falta rotular, já no formato do `rotulos.json`.
  Consulta "ausente" fica fora da precisão (todo trecho dela é "n"; quem a
  mede é `avisos`). Consulta sobre aula que ainda não aconteceu é ausente:
  cinco de Investimento derrubavam a matéria a 29% por isso — o comentário em
  `avaliacao/consultas.py` diz quando voltá-las. Precisão sozinha premia
  devolver pouco: a calibração de 24/09 "achou" +10 pontos em documento
  cortando 19 trechos úteis de 116. Olhe os úteis absolutos. Nesta investigação eu projetei de 5 a 8 pontos de ganho para a
  limpeza de ruído e o medido foi zero; e o reranker, que eu achava caro
  demais, rendeu 13 pontos em Investimento. Nenhum dos dois era previsível.
- **O reranker só entra em `buscar_material`.** Na tarefa de documento o
  resultado é IDÊNTICO a 20x o custo: reordenar não muda o conjunto quando se
  leva uma fatia larga. Custa 2,4 s por busca contra 180 ms.
- **A busca avisa quando o material só MENCIONA o assunto.** `aviso_cobertura`
  em `busca.py` cruza a frequência do termo mais raro com o score do primeiro
  colocado. Medido (24/09, 86 consultas): pega 7 de 13 ausentes e alarma
  falsamente 4 de 73 cobertas. Escapam os conceitos de várias palavras comuns
  ("valor presente líquido": "líquido" está em 58 trechos) — e contar a
  expressão inteira não separa, porque o material MENCIONA VPL 12 vezes antes
  de ensinar. Ele **avisa, não recusa** — a base é pequena para negar resposta. Se o aviso aparecer, mostre-o
  ao usuário: montar questão sobre conteúdo que ele não tem é o pior resultado
  possível aqui.
- **A âncora léxica é porteiro.** Se o BM25 não alcança um arquivo, nenhum
  ajuste de corte alcança: medido, afrouxar de 0,10 a 0,60 mantinha 5 dos 10
  arquivos de Macro. Quem abre a porta é o grafo (`ponte=True`), e só no modo
  cobertura — numa consulta pontual a ponte derruba a precisão de 76% para
  65%, porque o documento vizinho dilui.
- **O piso absoluto do modo vetorial foi atacado e ele venceu.** `PISO_COSSENO
  = 0,45` contradiz na cara a regra abaixo, e os números parecem dar razão ao
  ataque: "estado estacionário" tem melhor cosseno 0,432 e o piso zera a
  consulta, enquanto "o que determina onde uma indústria decide se instalar"
  passa 58 trechos do mesmo piso. Troquei por corte relativo, medi, e ficou
  PIOR: 85% → 79% de precisão em Macro, termo de 100% para 88%, com o mesmo
  corpus e os mesmos rótulos. Dois motivos, e os dois só aparecem medindo:
  `_vetorial` devolver vazio NÃO deixa a consulta sem resposta — `buscar` cai
  no léxico, que nessa consulta acerta mais que o vetor relativo; e poucas
  consultas passam pelo modo vetorial (14 de 86), então o que parecia um
  defeito central mexe em quase nada. Não tente de novo sem medir antes: em
  24/09 uma grade de 41 configurações de piso, margem, bônus e cobertura, com
  validação cruzada deixando uma matéria de fora, não achou nada melhor que o
  padrão sem perder trecho útil.
- **O modo vetorial une as âncoras AND** (`busca.UNIR_ANCORA`). Antes ele as
  descartava: "risco de inadimplência" está literal em 12 trechos de
  Investimento e nenhum voltava, porque as âncoras eram o mesmo PDF duas vezes
  (Cap 07 e Cap 07 rev) e o grafo alcançava pouco. Âncora OR não entra: uma
  palavra comum sozinha é o ruído que a âncora existe para evitar.
- **Embedding: o MiniLM corta em 128 tokens e mesmo assim ficou.** e5-large,
  jina-v3, média das janelas e maior janela foram medidos com os rótulos cegos
  e nenhum ganhou com significância (jina custava 7 s por busca). As falhas
  que sobram não são de vocabulário: dos 79 trechos úteis fora do top-3, 77
  contêm as palavras da consulta e só ficam abaixo na ordem.
- **Nenhum limiar de cosseno separa "coberto" de "ausente".** Medido em
  Regional: as consultas sobre assunto ausente têm teto ajustado entre 0,52 e
  0,60, ACIMA de "Christaller lugar central" (0,511), que é coberta. Quem
  separa é `aviso_cobertura`, por frequência do termo — é outro sinal, e é por
  isso que funciona.
- **Os limiares desta busca são relativos, não absolutos, e isso é medido.**
  Piso de cosseno não transfere entre consultas; similaridade entre documentos
  não transfere entre cursos (o par mais parecido de Regional fica abaixo do
  limiar que funciona em Macro). Por isso o corte do modo âncora é relativo ao
  melhor da rodada e o grafo usa k vizinhos, não limiar.
- `correio.py` chama o Mail.app por AppleScript: só macOS, e o conteúdo vai por
  `argv`, nunca interpolado no script (aspas no corpo quebrariam a compilação).
  O módulo **não** pode se chamar `email.py`: sombrearia o pacote da stdlib.
- **IPv4 forçado em `moodle_client.py`, e é isso que deixa tudo rápido.**
  `virtual.ufmg.br` publica IPv6 que não conecta (curl: IPv4 0,1 s, IPv6
  estoura em 40 s), e o httpx esperava ~30 s antes de cair para IPv4 na
  primeira chamada de TODO processo. Medido: sync de 31 s para 1 s, `notas`
  de 30 s para 0,3 s. Parecia lentidão do `site_info`, que só era o primeiro
  da fila. Não tire o `local_address`; `MOODLE_IPV6=1` desliga.
- **Material pode vir como anexo de aviso.** Em ECN299 todas as seções estão
  vazias e slides e programa chegam anexados ao fórum de Avisos, que
  `core_course_get_contents` não enxerga. `sync_forum_avisos` registra esses
  anexos em `arquivos` (`modname='forum'`, seção `Avisos — <assunto>`) pelo
  mesmo `_registrar_arquivo` das seções, e relê as discussões da turma que
  teve sync completo, para pegar anexo trocado.
- **Aviso não vira evento.** `avisos` guarda mensagem, notificação e fórum de
  avisos ("a primeira prova será 26/08"), mas data em texto livre é fonte pior
  que o plano de ensino, que já não confirma sozinho. A skill oferece
  `evento add`; não crie.
- **`modulos.concluido` não significa leitura para arquivo.** Baixar pela API
  não marca visualização: 50 de 50 arquivos apareciam pendentes. O briefing
  só olha `_EXIGE_ACAO` (questionário, tarefa...). Não "conserte" incluindo
  `resource`.
- **O sync pula turma sem mudança** (`core_course_get_updates_since`), mas
  refaz completo uma vez por dia (`COMPLETO_A_CADA`) e sempre que a rodada
  anterior teve erro — a marca `cursos.sincronizado_em` só avança sem erro.
- **O teto do corte relativo sai do score já rebaixado por ruído.** Com o score
  cru, um título de 41 caracteres virava o teto e "modelo de Solow" em Macro
  devolvia UM trecho. A avaliação não pegou porque mede precisão entre os
  devolvidos: devolver pouco e certo parece ótimo. Olhe `tokens` baixo demais
  numa matéria como sinal de que a busca está devolvendo pouco.
- **A busca é escopada no semestre corrente, e isso importa antes de doer.**
  `site` sempre foi a chave de tudo, mas nada dizia qual semestre é o de
  agora, então `buscar_material` varria todos. Com um semestre só, "todos" e
  "o corrente" são a mesma coisa; com dois, a resposta traria o slide do ano
  passado sem avisar. `db.site_atual` é o maior alias não arquivado, e
  `semestre="*"` ou um alias explícito atravessa. Quando a busca não acha nada
  e existe material de outro semestre, ela diz isso — senão o parâmetro é
  inalcançável na prática. Semestre arquivado (`cli.py semestre arquivar`) sai
  do sync e fica na busca: o token dele expira e uma rodada por dia falhando
  não avisa nada a ninguém.
  O parâmetro `semestre` custa **23 tokens por mensagem**, porque
  `buscar_material` é quente — medido, e é por isso que a docstring dela tem
  uma linha e não três. Enquanto houver um semestre só, esses 23 tokens não
  compram nada; o escopo em si (`_escopo_semestre`) é de graça. Se o parâmetro
  incomodar antes de o segundo semestre chegar, tire ele e deixe o escopo.
- **A API do Moodle voltou a aceitar só o token em 24/09/2026**, e o cookie
  vencido passou a quebrar: o gateway responde 200, corpo vazio e um
  `Set-Cookie` apagando `ufmg_saml_session` — o erro aparecia como "Resposta
  não-JSON" sem mensagem. `moodle_client._cookie_recusado` descarta o cookie e
  repete a chamada uma vez, sem tocar no chaveiro. Se "não-JSON" voltar, olhe
  status, `set-cookie` e destino final da resposta crua antes de mexer em
  login. O que segue vale se a parede do SSO voltar.
- **A API do Moodle exigiu a sessão do minhaUFMG** (21 a 24/09/2026). Sem o
  cookie `ufmg_saml_session`, `webservice/rest/server.php` responde 302 para
  `sistemas.ufmg.br/idp` **com ou sem token** — por isso parecia token
  expirado e não era. Medido um a um, dos nove cookies do login ele é o
  **único** necessário. Fica no chaveiro como `20262:saml`, `get_client` o
  carrega e o cliente o manda. É cookie de sessão: não diz quando expira, quem
  decide é o servidor. Quando expirar, `MoodleBloqueadoSSO` diz para rodar
  `login_navegador.py 20262 --so-sessao`, que renova só o cookie e mantém o
  token. **Login automático** (autorizado pelo usuário): com
  `login_navegador.py --guardar-credenciais`, usuário e senha do minhaUFMG
  vão para o chaveiro por prompt que não ecoa, e o cliente refaz o login
  sem janela quando bate no SSO — uma vez, e no máximo a cada 15 min,
  porque o SSO tem captcha após tentativas erradas e insistir bloquearia a
  conta. `MOODLE_SEM_LOGIN_AUTOMATICO=1` desliga. A senha abre todos os
  sistemas da UFMG: nunca por argumento, nunca por variável de ambiente.
  Antes do erro nomeado, o agente gastou 15 turnos tentando contornar;
  e no download a página de login vinha com status 200 e seria gravada no lugar
  do PDF.
- **Token no chaveiro do sistema** (`keyring`), o `sites.json` só com URL.
  `cli.py token` diz onde está e testa a leitura.

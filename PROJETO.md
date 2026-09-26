# UFMG Moodle Companion — documento de projeto

Assistente pessoal para o UFMG Virtual: sincroniza materiais, monta o calendário
real da disciplina, avisa de prazos e ajuda a estudar a partir do seu próprio
material de aula.

Status: fundação, escrita, extração de texto e revisão assistida de
questionário prontas. Falta o resumo por seção, o extrator de programa de
curso, as notificações e a GUI.

---

## 1. Escopo

**Dentro:**

- Sincronizar turmas, seções, materiais e eventos de vários semestres.
- Baixar e versionar slides, PDFs e listas; detectar o que mudou.
- Calendário unificado com origem rastreável e edição manual.
- Resumo semanal e alertas de prazo (Telegram/e-mail).
- Companion de estudo: resumos, simulados e revisão dirigida por erro.
- Consulta ad-hoc via MCP, dentro do Claude.
- **Escrita no Moodle** (`escrita.py`): postar e responder em fórum, anexar,
  salvar e entregar tarefa, iniciar/salvar/enviar questionário. Paridade com o
  que o app oficial do Moodle já faz pelo mesmo token.

**Como a escrita é tratada.** É a única parte irreversível do sistema, então
segue três regras, garantidas em `escrita.py` e testadas:

1. **Ensaio por padrão.** Sem `confirmar=True` (`--confirmar` na CLI) a função
   devolve o payload que *seria* enviado e não chama o Moodle.
2. **Log completo.** Toda tentativa vira linha em `log_escrita`, inclusive as
   que falham, com o payload exato. `cli.py escritas` mostra tudo que já saiu
   no seu nome.
3. **Conteúdo é seu.** As funções enviam o que recebem; nenhuma gera texto.

**Fora:**

- Enviar questionário sem política declarada quando ele vale nota no Moodle.
  Não é uma proibição, é um default: o sistema não adivinha o peso real das
  notas da sua disciplina, então na dúvida ele para e pergunta. Uma declaração
  por curso resolve para o semestre inteiro.

**Adjacente, projeto separado:** QA de questionário para docente, usando a
pré-visualização do Moodle e token de conta de professor. Não é um modo deste
app; é outro app, com outro token e outro público.

---

## 2. Arquitetura

```
moodle_client.py   camada única de acesso à API REST do Moodle
      │            (call = leitura/escrita, download, upload)
      │
      ├── escrita.py   fronteira de escrita: fórum, tarefa, questionário
      ├── extract.py   PDF/PPTX/DOCX/ipynb → texto
      │
      ├── server.py    servidor MCP  (você pergunta → responde)
      ├── sync.py      sincronizador (roda em cron → alimenta o banco)
      ├── cli.py       operação e edição manual
      └── web/         GUI FastAPI  (fase 4)
              │
           db.py   SQLite: estado, materiais, eventos, questões, log_escrita
```

Acima do MCP há duas camadas que não são código Python:

```
.claude/skills/ufmg-companion/   procedimento: qual tool, em que ordem, quando parar
.claude/agents/pesquisador-material.md   isolamento de contexto na leitura pesada
```

A divisão é deliberada: **ferramenta dá capacidade, skill dá procedimento,
subagente dá contexto separado**. Sem a skill, a sequência de chamadas é
re-inventada a cada conversa; sem o subagente, ler material enche o contexto
principal e encarece o resto da sessão.

Toda escrita passa por `escrita.py`, e dentro dele por `_executar()` — caminho
único que aplica o ensaio e o log. Auditar o que este programa é capaz de
publicar é ler um arquivo.

Decisão: **o cron não chama o MCP**. MCP é interface de pergunta; o agendado é
um processo comum. Ambos compartilham `moodle_client.py` e `db.py`.

---

## 3. Modelo de dados

Schema completo em `db.py`. Os pontos que carregam o projeto:

**`trechos` + `trechos_fts` + `vetores`** — o material cortado no tamanho em
que é citado (uma página, um slide, um bloco de parágrafos), com o índice
léxico e o vetorial sobre a mesma unidade. Alimentados por `extract.indexar()`
a cada extração, então o agente do launchd mantém tudo sozinho.

A versão anterior indexava o **arquivo inteiro** como um documento, e errava
de dois jeitos: o BM25 ranqueava um PDF de 80 páginas acima do slide exato só
por ele conter o termo em algum lugar, e o trecho devolvido era uma janela de
28 tokens sem número de página — o que empurrava a conversa para ler o PDF
inteiro (20k chars) quando aquela janela não bastava. Hoje o resultado já sai
citável e a busca custa ~700-1100 tokens.

Vetor é chaveado pelo sha256 do texto do trecho, com o nome do modelo e a
versão do fastembed junto: slide repetido entre duas aulas embute uma vez só,
reextrair arquivo que não mudou não paga o modelo, e trocar de modelo invalida
os vetores sozinho. Busca do usuário usa AND (quem procura "estado
estacionário" quer as duas palavras); `achar_fonte` usa OR, porque exigir que
os seis termos de um enunciado coocorram quase nunca casa — com OR o BM25
ranqueia por quantos bateram e quão raros são.

**`grafo_documentos`** — arestas entre ARQUIVOS, construídas na indexação a
partir dos vetores. A adjacência dentro de um arquivo já vem de
`trechos.ordinal`; isto liga arquivo a arquivo, por centroide (os k mais
próximos) e por conteúdo praticamente igual.

Existe por um defeito medido: a busca por âncora alcançava 5 dos 10 arquivos
de Macro, e afrouxar o corte de 0,10 até 0,60 não mudava — porta que nunca
abriu não abre por margem. Com o grafo, 10 de 10. Os três arquivos órfãos eram
justamente os que tinham a mesma tabela ou a mesma figura de outro arquivo, ou
centroide muito próximo de quem os citava.

O fluxo e as regras que decidem o resultado estão em `busca.py` e `grafo.py`,
documentados lá. Vale registrar um princípio que saiu desses testes: **limiar
absoluto não transfere**. Nem entre consultas (o cosseno cai dentro de uma
vizinhança já relevante) nem entre cursos (o par de documentos mais parecido
de Regional fica abaixo do limiar que funciona em Macro). Por isso o corte é
relativo ao melhor da rodada e o grafo usa k vizinhos.

**`arquivos`** — chave `(site, fileurl)`. Mudança é detectada por
`(timemodified, filesize)`, porque `contenthash` nem sempre vem em
`core_course_get_contents`. O `sha256` é calculado localmente depois do
download e serve para o caso comum de professor republicar o mesmo PDF: os
metadados mudam, o conteúdo não, e não faz sentido avisar você por isso.

**`eventos`** — todo evento guarda `origem`, `origem_ref` e `trecho_origem`.
Precedência (`PESO_ORIGEM` em `sync.py`):

```
manual (5) > calendario (4) > forum (3) > programa_pdf (2)
```

Duas regras invioláveis, já implementadas e testadas em `upsert_evento`:

1. Evento com `confirmado = 1` nunca é alterado por fonte automática.
2. Fonte de peso menor não sobrescreve fonte de peso maior.

Toda mudança de data grava linha em `historico_eventos`. É isso que permite o
aviso mais útil do sistema: *"a Prova 1 mudou de 15/04 para 22/04, segundo o
calendário"*.

**`log_escrita`** — toda ação de escrita, com `payload` (o JSON exato enviado)
e `resposta`. Falhas entram também. É o `historico_eventos` da escrita: se
saiu algo no seu nome, saiu daqui, e dá para reconstruir o texto exato.

**`politicas_envio`** — sua declaração sobre o peso real das notas de
questionário, por quiz, curso ou global, com motivo e data. Existe porque a
API do Moodle responde "vale 10 pontos" mas não responde "esses 10 pontos
importam", e a segunda pergunta é a que decide.

**`memoria_alias`** — "macro" → curso 6095. A chave inclui o site, então
apelido de semestre antigo morre sozinho. Existe porque traduzir o nome que
você usa no id que a API quer custava uma chamada em toda conversa.

**`memoria_resumo`** — resumo derivado, chaveado pelo `sha256` do conteúdo e
não pelo id do arquivo. Republicação com alteração real muda o hash e o resumo
antigo deixa de ser encontrado; o mesmo PDF em dois cursos aproveita um resumo
só. A lista do que **não** pode ser memorizado está em `memoria.VOLATIL` — nota,
prazo, estado de tentativa — e a recusa é código, não comentário.

**`notificacoes`** — chave semântica (`evento:12:prazo_48h`) para o bot não
repetir o mesmo alerta a cada execução.

**`questoes` / `respostas`** — cada questão guarda `fonte_trecho`, o pedaço do
material que a originou. Sem isso não há como distinguir questão fiel do seu
material de questão alucinada.

---

## 4. Fases

### Fase 1 — Fundação ✅
`moodle_client.py`, `get_token.py`, `server.py`, `db.py`, `sync.py`, `cli.py`.
Sync completo, download com dedup por hash, calendário com precedência e edição
manual funcionando.

### Fase 1b — Escrita ✅
`escrita.py` + 12 ferramentas MCP + comandos `forum` / `tarefa` / `quiz` /
`escritas` na CLI. Ensaio por padrão, `log_escrita` em toda tentativa,
`MoodleClient.upload()` para anexo de tarefa (multipart em `/webservice/
upload.php`, que não passa pelo endpoint REST).

Ponto frágil conhecido: os nomes de campo do questionário (`q42:1_answer`)
só existem no HTML renderizado da tentativa; `dados_tentativa` os extrai por
regex. Se o tema do site mudar a renderização, quebra ali.

### Validado contra o UFMG Virtual real (2026-09-08)

Moodle 4.1.21+, 396 funções liberadas — inclusive escrita de fórum, tarefa e
questionário. Nenhuma restrição de web service para aluno neste site.
O que só apareceu no servidor de verdade:

- **`limitnum` do calendário é limitado a 50.** O código pedia 200 e o sync
  perdia o calendário inteiro, com o erro escondido no fim da saída. Agora
  pagina por `aftereventid`.
- **`get_action_events_by_timesort` só devolve o que ainda exige ação.** Prazo
  vencido e evento informativo não aparecem: com 5 eventos reais no calendário,
  ele devolvia 0. A fonte primária passou a ser
  `core_calendar_get_calendar_events`, com o outro endpoint como complemento
  (os dois usam o mesmo id, então `cal:<id>` deduplica).
- **`--sem-download` envenenava o banco.** Arquivo registrado sem
  `caminho_local` era pulado para sempre nas rodadas seguintes: os metadados
  não mudaram, então o sync o considerava em dia. Você ficava sem material e
  sem aviso. `falta_local` cobre isso, inclusive para arquivo apagado do disco.
- **Tentativa finalizada não responde a `get_attempt_data`** — só a
  `get_attempt_review`. Era justamente o caso mais útil para estudo, e não
  funcionava. Ver "modo revisão" na fase 2.

### Fase 2 — Texto e companion de estudo
1. `extract.py` ✅: PDF/PPTX/DOCX/ipynb/md → texto em `arquivos.texto_path`.
   PDF sem camada de texto marca `precisa_ocr = 1` em vez de gerar texto vazio
   (limiar: `MIN_CHARS_POR_PAGINA`). Do PPTX saem também as notas do
   apresentador; do DOCX, as tabelas — é onde mora o cronograma. Reprocessa
   sozinho quando o sync detecta mudança real de conteúdo.
2. `resumo.py`: o cache por `sha256` já existe em `memoria_resumo`; falta o
   gerador de resumo por seção.
3. `companion.py` ✅ — revisão assistida de questionário:

   ```
   preparar_revisao()  lê a tentativa, separa alternativas, acha no SEU
                       material o trecho do assunto
        ↓  (o Claude propõe resposta + explicação, com o trecho na mão)
   você edita e aprova
        ↓
   salvar_revisao()    grava em questoes/respostas — guarda a proposta do
                       modelo E a sua resposta, separadas
        ↓
   enviar_aprovado()   envia ao Moodle — só se o quiz não valer nota
   registrar_correcao()puxa o resultado real do Moodle
   roteiro_estudo()    errou primeiro, divergiu depois, revisar por último
   ```

   **A regra de envio.** A API informa a nota máxima do questionário, mas não
   informa o que aquela nota vale na disciplina — num curso em que o Moodle é
   participação e a avaliação é presencial, `grade = 10` não diz nada sobre
   risco. Isso o código não tem como descobrir, então `pode_enviar()` resolve
   na ordem:

   1. política declarada para o **quiz**;
   2. política declarada para o **curso**;
   3. política **global**;
   4. sem declaração: heurística `grade == 0` (só prática envia).

   Declarar exige motivo, e fica em `politicas_envio` com data — "por que este
   curso está liberado?" precisa ter resposta no banco daqui a três meses.
   Funciona nos dois sentidos: com o curso liberado, dá para bloquear o quiz
   específico que de fato conta, e a regra mais específica vence.

   **`proposta_ia` vs `correta`.** As duas ficam gravadas. Onde você discordou
   do modelo havia dúvida real — é o segundo critério de prioridade do
   roteiro, atrás só do que o Moodle marcou como errado.

   **Modo revisão (tentativa finalizada).** `preparar_revisao` detecta o caso
   pela falha de `get_attempt_data` e cai em `get_attempt_review`. Aí o Moodle
   entrega a correção pronta, e o desenho fica melhor do que o original: a
   resposta certa é a oficial, não uma proposta do modelo, que passa a só
   explicar o porquê. Uma explicação alucinada não consegue virar gabarito.

   Extrai da revisão: sua resposta (`checked`, ou o `value` do input de texto
   em questão numérica), a correta, a nota da questão e o acerto. Quando você
   **erra**, o Moodle marca só a sua opção como `incorrect` e não marca a
   certa — ela existe apenas na frase "A resposta correta é 'X'". Sem o
   fallback por texto (`_correta_por_texto`), justamente as questões erradas
   ficariam sem gabarito. Cuidado correlato: `class="r1 correct"` dá a
   *posição* da opção, não o `value`; em verdadeiro/falso r0 costuma ser
   `value=1`, então confundir os dois inverte o gabarito inteiro.

   Ainda falta: gerar simulado do zero a partir do material (sem tentativa no
   Moodle) e a repetição espaçada por intervalo.

### Fase 3 — Programa de curso e notificações
4. `programa.py` ✅: acha o plano de ensino por nome/seção, lê o cronograma e
   propõe as avaliações com `origem='programa_pdf'` e `confirmado=0`.
   **Nunca confirma sozinho.**

   O aviso do plano original se confirmou na prática: "Avaliação" casa com
   título de capítulo ("Avaliação por Fluxos de Caixa Descontados — Cap. 6"),
   que é leitura. A primeira versão marcou 16 provas onde havia 5. O que
   distingue é o **ordinal** e a **caixa alta** — `_e_avaliacao()`.

   O ano vem do alias do site (`20262` → 2026, semestre 2), porque a tabela
   escreve só dia/mês; mês ≤ 2 num segundo semestre é do ano seguinte.

   Datas fora de ordem são **sinalizadas, não corrigidas**: no ECN300 elas
   revelaram erro de digitação no próprio PDF do professor (aula 22 em 26/10,
   aula 23 em 04/10). Esconder seria pior — a data entra no calendário do
   mesmo jeito.
5. `notificar.py`: resumo semanal (segunda de manhã) + alertas D-7/D-2/D-1.
   Telegram primeiro (API trivial, chega no celular). Cron ou systemd timer.

### Fase 4 — GUI
6. FastAPI servindo HTML local, no mesmo processo que já roda o sync agendado.
   Telas: painel da semana, calendário com badge de origem e confirmação em um
   clique, navegador de materiais, sessão de estudo.
   Streamlit foi descartado: resolve a primeira tela em uma tarde e briga com
   você a partir da segunda.

---

## 5. Riscos conhecidos

| Risco | Mitigação |
|---|---|
| Data errada extraída de PDF | tudo entra como pendente, com trecho original visível |
| Professor muda data em sala | precedência + `historico_eventos` + edição manual |
| PDF escaneado | detecção explícita de `precisa_ocr`, sem resumo falso |
| Questão alucinada no simulado | `fonte_trecho` obrigatório em toda questão |
| Token expirado / fim de semestre | `get_token.py` de novo (~30s), um site novo por semestre |
| Escrita acidental / no alvo errado | ensaio é o default; `--confirmar` é explícito; `log_escrita` guarda o payload |
| Entrega irreversível enviada cedo demais | `anexar`/`salvar` e `entregar` são comandos separados; confira com `status_tarefa` antes |
| Renderização do quiz muda e quebra o regex | `ler_questionario` mostra os campos encontrados; lista vazia é o sinal |
| Explicação alucinada na revisão | `fonte_trecho` vem do seu material e aparece junto; "fonte não encontrada" é aviso explícito |
| Envio a questionário que de fato conta | `pode_enviar()` exige política declarada; sem ela, só prática passa. Política de quiz vence a de curso, então dá para bloquear a exceção |
| Carga no servidor da UFMG | sync no máximo de hora em hora; nunca em loop |
| `--sem-download` deixar material só registrado | `falta_local` rebaixa o arquivo e baixa na rodada seguinte |
| Endpoint de calendário incompleto | duas fontes combinadas, dedup por `cal:<id>` |

---

## 6. Primeiros comandos

```bash
pip install -r requirements.txt
python get_token.py          # login no navegador, uma vez por semestre
python cli.py init
python cli.py sync           # baixa tudo e monta o calendário
python cli.py agenda --dias 30
python cli.py evento add "Prova 1" --data 2026-04-15T14:00 --site 20262
python cli.py evento set 7 --data 2026-04-22T14:00
python cli.py extrair              # material baixado -> texto

# Escrita: ensaia primeiro, envia depois
python cli.py forum postar --forum 3 --assunto "Dúvida" --texto "..."
python cli.py forum postar --forum 3 --assunto "Dúvida" --texto "..." --confirmar
python cli.py tarefa anexar --assign 12 --arquivo trabalho.pdf --confirmar
python cli.py tarefa salvar --assign 12 --itemid 555 --confirmar
python cli.py tarefa entregar --assign 12 --aceitar-declaracao --confirmar
python cli.py escritas             # tudo que já saiu no seu nome

# Revisão assistida de questionário
python cli.py estudo preparar --tentativa 88     # gera sessao_88.json
# (o Claude preenche proposta_ia/explicacao; você edita resposta_final)
python cli.py estudo salvar --arquivo sessao_88.json
python cli.py estudo enviar --arquivo sessao_88.json --confirmar   # só prática
python cli.py estudo corrigir --tentativa 88     # puxa o resultado do Moodle
python cli.py estudo roteiro

# Uma vez por curso: declare o que a nota do Moodle significa
python cli.py estudo politica --curso 12345 --permitir \
    --motivo "questionários valem participação; provas são presenciais"
python cli.py estudo politica --quiz 5 --bloquear --motivo "esse conta"
python cli.py estudo politicas
```

---

## 7. Para continuar no Claude Code

Ordem sugerida: `resumo.py` → `programa.py` → `notificar.py` → `web/`.
(`extract.py`, `escrita.py` e `companion.py` já estão prontos.)

Convenções do projeto: tudo em português (nomes de coluna, comandos, saída);
toda escrita no Moodle passa por `escrita.py`, ensaia por padrão e é
registrada; nenhuma credencial em arquivo
— apenas o token, no chaveiro do sistema (ou em `~/.ufmg-moodle-mcp/sites.json`
com permissão 600, sem o pacote `keyring`); toda
informação derivada (resumo, questão, data extraída) carrega o ponteiro para a
fonte que a originou.

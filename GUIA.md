# Guia de uso

Como operar o UFMG Moodle Companion no dia a dia. Para instalação, veja o
`README.md`; para as decisões de projeto, o `PROJETO.md`.

---

## O modelo mental

Duas portas para a mesma coisa:

```
   você no terminal              você conversando com o Claude
        │                                    │
     cli.py  ────────┐              ┌──── server.py (MCP)
                     ▼              ▼
              banco SQLite + materiais em disco
                     ▲
                     │
              sync.py ── API do Moodle
```

**A CLI é para operar** — sincronizar, conferir, executar. **O MCP é para
perguntar** — é onde você conversa sobre o material, porque o Claude enxerga
suas turmas e o texto das aulas.

Nada acontece sozinho: o sync só roda quando você manda (ou quando você o
colocar num cron). O banco fica em `~/.ufmg-moodle-mcp/dados.db`, os PDFs em
`~/.ufmg-moodle-mcp/materiais/`.

Nos exemplos abaixo, `py` é `.venv/bin/python` dentro da pasta do projeto.

---

## Material de estudo em PDF e e-mail

A apostila em PDF funciona em qualquer sistema. O e-mail é **só macOS**:
ele usa o Mail.app.

```bash
py cli.py apostila --partes ~/.ufmg-moodle-mcp/apostilas/macro-iii-prova-1
py cli.py email remetentes
py cli.py email enviar --para voce@gmail.com --assunto "Apostila" \
    --anexo ~/.ufmg-moodle-mcp/apostilas/macro-iii-prova-1/apostila.pdf \
    --texto "..."
```

**O ensaio do e-mail é um rascunho de verdade**: abre no Mail.app para você ler
como vai sair. `--confirmar` envia direto, sem abrir.

Nenhuma credencial: o Mail.app já tem sua conta. Com mais de uma conta, `--de`
escolhe o remetente; com uma só, pode omitir.

**Destinatário novo é recusado.** E-mail enviado não tem desfazer — post em
fórum dá para apagar, mensagem na caixa de outra pessoa não. Confirme uma vez e
ele fica conhecido:

```bash
py cli.py email lembrar --para colega@ufmg.br --nome "Fulano"
py cli.py email contatos
```

Resumo da semana por e-mail, para o cron:

```bash
17 7 * * 1 cd "<projeto>" && .venv/bin/python cli.py email briefing \
    --para voce@gmail.com --confirmar
```

## Onde o MCP funciona (e onde não)

MCP não tem interface própria — é encanamento que dá ferramentas ao Claude. A
interface é o Claude que você já usa, e você fala em português normal:

> "Tenho alguma entrega essa semana?"
> "Qual minha nota na Lista 1?"
> "O que os slides de Macro dizem sobre estado estacionário?"

| Onde | Funciona? | Como registrar |
|---|---|---|
| Claude Code (terminal/IDE) | sim | `claude mcp add --scope user ufmg-moodle -- <venv>/bin/python <projeto>/server.py` |
| Claude Desktop (Mac/Windows) | sim | entrada `mcpServers` em `claude_desktop_config.json` |
| claude.ai no navegador | **não** | — |
| App de celular | **não** | — |

O motivo dos dois "não": o `server.py` é um processo que roda **na sua
máquina**, iniciado pelo cliente como subprocesso. O site e o app rodam nos
servidores da Anthropic e não alcançam — nem devem alcançar — um processo no
seu laptop. Consultar as turmas pelo celular não dá.

### Skill no Desktop

A skill e o subagente vivem em `.claude/` e são um recurso do Claude Code: só
valem quando você trabalha nessa pasta. O Desktop não tem esse conceito.

O contorno é o próprio protocolo MCP, que tem **prompts** — e prompts chegam a
qualquer cliente. O servidor expõe cinco:

| Prompt | Para quê |
|---|---|
| Situação atual | prazos, material novo, o que estudar |
| O que estudar | roteiro a partir dos seus erros + material |
| Revisar questionário | tentativa corrigida, gabarito do Moodle |
| Pesquisar no material | busca com citação obrigatória |
| Datas de prova | lê o plano de ensino |

No Desktop eles aparecem no menu de anexos/comandos da caixa de mensagem
(o ícone de `+` ou de ferramentas). Escolher um já entrega ao modelo o
procedimento e as regras de economia — é a skill por outro caminho.

Há também o recurso `ufmg://guia`, que você pode anexar à conversa.

O que **não** dá para reproduzir no Desktop é o subagente: ele existe para ler
material num contexto separado e devolver só a síntese. Sem ele, uma pesquisa
longa enche o contexto da conversa. As ferramentas continuam funcionando —
custa mais token.

Nos dois que funcionam, o registro é por cliente: registrar no Claude Code não
registra no Desktop. E ambos só carregam MCP na inicialização — depois de
registrar, **feche e reabra** (no Mac, Cmd+Q; fechar a janela não basta).

Se o servidor sumir, o sintoma é o Claude dizer que não tem as ferramentas.
Confira com `claude mcp list` — precisa dizer `✔ Connected`. A causa mais comum
é a pasta do projeto ter sido movida ou renomeada, porque o caminho no registro
é absoluto.

## Skill, memória e subagente

O repositório traz uma **skill** (`.claude/skills/ufmg-companion/`) que ensina
o Claude a escolher a ferramenta certa e parar na hora certa, e um **subagente**
(`.claude/agents/pesquisador-material.md`) que lê material pesado no contexto
dele e devolve síntese curta — o contexto principal recebe ~800 tokens em vez
de ~28k. Ambos são versionados: quem clonar o repo já os tem.

A **memória** guarda o que não muda:

```bash
py cli.py memoria lembrar "macro" --curso 6095 --rotulo "MACROECONOMIA III"
py cli.py memoria listar
py cli.py memoria esquecer "macro"
```

Com isso, "minha nota em Macro?" custa uma chamada em vez de duas — não é
preciso listar as turmas para descobrir o id.

Nota, prazo, estado de tentativa e status de entrega **não** são memorizáveis:
a ferramenta recusa, porque o Moodle muda e um cache desses mentiria com
confiança. Memoriza-se o caminho, nunca o valor.

Resumos de material são chaveados pelo `sha256`: se o professor republicar com
alteração real, o hash muda e o resumo antigo deixa de ser servido, sem
ninguém precisar limpar nada.

## O ritmo

| Quando | O quê |
|---|---|
| Uma vez por semestre | `py login_navegador.py` (token novo) |
| Toda vez que quiser novidade | `py cli.py sync && py cli.py extrair` |
| Antes de estudar | `py cli.py agenda`, ou pergunte ao Claude |
| Depois de fazer um questionário | `py cli.py estudo preparar --tentativa N` |

Os dois primeiros comandos são 90% do uso.

---

## 1. Trazer novidade do Moodle

```bash
py cli.py sync        # baixa material novo e atualiza o calendário
py cli.py extrair     # transforma os PDFs novos em texto e indexa a busca
py cli.py programa    # lê o plano de ensino e propõe as datas de prova
```

`sync` é seguro de repetir: material que não mudou não é rebaixado, e arquivo
republicado sem alteração real de conteúdo (mesmo `sha256`) não vira novidade
falsa. `extrair` só processa o que ainda não tem texto.

Não rode em laço nem de minuto em minuto — é um servidor institucional. De hora
em hora já é bastante.

```bash
py cli.py sync --sem-download     # só ver o que mudou, sem baixar
```

`sync` também traz o que **não é arquivo**:

- **Avisos** — mensagens diretas e o fórum "Avisos" de cada turma. É por onde
  chega "a primeira prova será 26/08" e "aula hoje no Laboratório 1102", que o
  calendário não tem. Mensagem de professor da turma aparece marcada; recibo
  de envio e resumo de fórum ficam guardados, mas fora da lista.
- **Mudanças** — antes de baixar a estrutura de uma turma, ele pergunta ao
  Moodle o que mudou desde a última vez, e pula a turma que não mudou. Uma vez
  por dia baixa tudo mesmo assim, para não depender só da palavra do Moodle.
- **Conclusão** — o que o Moodle marca como concluído. Só conta para
  questionário e tarefa: arquivo baixado pelo sync não fica "visto" no Moodle,
  então todo arquivo apareceria pendente.

```bash
py cli.py avisos                         # últimos 14 dias, só de pessoas
py cli.py avisos --dias 30 --automaticos # inclui os do sistema
```

Aviso **não vira evento sozinho**. "A prova será 26/08" num texto livre é fonte
pior que o plano de ensino, e nem o plano confirma sozinho. Se a data importa:
`py cli.py evento add "Prova 1" --data 2026-08-26T07:30 --curso 8025`.

**Onde isso aparece:**

```bash
py cli.py turmas                       # suas 5 turmas, com o id de cada
py cli.py materiais --novos            # o que chegou nos últimos 7 dias
py cli.py materiais --curso 6095       # tudo de Macroeconomia III
```

Material marcado `[precisa OCR]` é PDF escaneado, sem camada de texto — hoje
você tem 6 assim, todos capítulos de livro da Economia Regional. Eles não
entram na busca nem no companion. É limitação conhecida, não erro.

---

## 2. Saber o que vem por aí

```bash
py cli.py agenda                  # próximos 30 dias
py cli.py agenda --dias 60
py cli.py agenda --todos          # inclui o que já passou
```

Cada linha traz um selo: `✓` confirmado por você, `?` veio de fonte automática.
A origem aparece entre parênteses.

Pelo Claude, "o que tem essa semana?" chama o `briefing`, que junta numa
resposta curta o que exige atenção. Cada linha só aparece quando tem conteúdo:

| Linha | O que é |
|---|---|
| `PRAZOS` | eventos da janela, prova e entrega |
| `ATRASADO` | entrega vencida que o Moodle **ainda** lista como ação sua — a que você já fez não aparece |
| `CARGA` | a semana mais pesada das próximas 4: prova pesa 3, entrega pesa 1, e só aparece de 3 para cima |
| `AVISOS` | mensagens de pessoas, até 3 com texto; o resto em `avisos()` |
| `MUDOU NO MOODLE` | atividade que o professor alterou (arquivo novo já está em `MATERIAL NOVO`) |
| `NÃO CONCLUÍDO` | questionário e tarefa que o Moodle marca como pendentes |

**Quando o professor muda a data em sala** e o Moodle não reflete:

```bash
py cli.py evento add "Prova 2" --data 2026-10-20T14:00 --curso 6095
py cli.py evento set 7 --data 2026-10-27T14:00     # corrigir a data do #7
py cli.py evento confirmar 7                        # blindar contra o sync
py cli.py evento cancelar 7
```

Evento confirmado nunca mais é alterado por fonte automática — sua palavra
vence o calendário do Moodle. Toda mudança de data fica no histórico, então dá
para saber que a Prova 1 andou de 15/04 para 22/04 e por quê.

---

## 3. Estudar com o material (a parte que vale a pena)

Aqui o MCP ganha da CLI, porque a conversa é o ponto. No Claude Code, com o
servidor registrado, pergunte em português:

> "O que o material de Macroeconomia III diz sobre estado estacionário no
> modelo de Solow?"

> "Quais PDFs de Econometria falam de heterocedasticidade?"

> "Resume o Guia de Estudos de ECN300."

Por trás, o Claude usa `buscar_material`, que devolve o trecho **com arquivo e
página**, para você conferir contra a fonte. Ela mistura duas buscas: a de
termo exato e a de sentido. Na prática isso muda como perguntar — "estado
estacionário" acha pelo termo; "por que a poupança não sustenta o crescimento"
acha pelo sentido, e encurtar essa segunda para duas palavras piora o
resultado. Quando o trecho quase responde, vem `trecho_material` (aquela
página e as vizinhas); `texto_material`, o arquivo inteiro, é o último recurso.

Você também pode buscar direto: `py cli.py buscar "estado estacionário"`.

**O que já foi resumido ou montado:** `py cli.py cobertura --curso 6095`. Ele
mostra quais arquivos têm resumo, quais herdariam resumo de um arquivo irmão
(material republicado sob outro nome é comum), e que documentos já foram
montados. Vale olhar antes de pedir um resumo novo: reaproveitar economiza
entre 60% e 92% do token, mais que qualquer ajuste de busca.

Pela CLI, o equivalente cru:

```bash
py cli.py materiais --curso 6095
```

---

## 4. Revisar um questionário que você já fez

Este é o fluxo mais útil do companion, e o mais seguro: a tentativa já acabou,
não há nada a enviar.

```bash
py cli.py estudo preparar --tentativa 57300
```

Ele lê a tentativa, e como ela está finalizada, puxa a **correção oficial do
Moodle**: sua resposta, a resposta certa, a nota de cada questão. Para cada
questão ainda procura no seu material o trecho que fala do assunto. Gera
`sessao_57300.json`.

O modelo não propõe resposta aqui — não precisa, o gabarito é o do Moodle. O
que ele acrescenta é a explicação do *porquê*. No Claude:

> "Prepara a revisão da tentativa 57300 e me explica as que eu errei."

Depois:

```bash
py cli.py estudo salvar --arquivo sessao_57300.json
py cli.py estudo roteiro
```

O roteiro ordena pelo que você menos sabe:

1. **ERROU** — o Moodle marcou como errada
2. **DIVERGIU** — você mudou a resposta que o modelo propôs (havia dúvida)
3. **revisar** — o resto

Hoje o seu roteiro tem 4 questões erradas da Lista 1: duas contas de Solow
(13440000 em vez de 3360000; 10000 em vez de 70000) e dois verdadeiro/falso
sobre estado estacionário. As duas contas são o mesmo tipo de erro.

---

## 5. Responder um questionário em andamento

Quando a tentativa está **aberta**, o fluxo é o que você desenhou: o modelo
propõe, você edita e aprova, e o companion envia.

```bash
py cli.py quiz tentativas --quiz 3800      # ver se há tentativa aberta
py cli.py quiz iniciar --quiz 3800 --confirmar
py cli.py estudo preparar --tentativa <id>
# você revisa e edita resposta_final no JSON
py cli.py estudo salvar --arquivo sessao_<id>.json
py cli.py estudo enviar --arquivo sessao_<id>.json --confirmar
```

**O envio depende da política do curso.** Sem declarar nada, só questionário de
prática (`grade == 0`) é enviado. Como na sua Macro III os questionários valem
nota no Moodle mas a avaliação real é presencial, declare uma vez:

```bash
py cli.py estudo politica --curso 6095 --permitir \
    --motivo "listas valem participação; avaliação da disciplina é presencial"
```

Isso vale para o semestre. Se algum questionário específico realmente contar:

```bash
py cli.py estudo politica --quiz 3800 --bloquear --motivo "esse conta"
```

A regra mais específica vence: quiz > curso > global. `py cli.py estudo
politicas` lista o que você declarou, com motivo e data.

Depois que o professor corrigir:

```bash
py cli.py estudo corrigir --tentativa <id>    # traz o resultado real
py cli.py estudo roteiro                       # e o erro sobe no roteiro
```

---

## 6. Escrever no Moodle

Toda escrita **ensaia por padrão**. Sem `--confirmar`, o comando mostra o que
enviaria e não chama o Moodle. É de propósito: entregar tarefa e finalizar
questionário não têm desfazer.

```bash
# Fórum
py cli.py forum postar --forum 42 --assunto "Dúvida lista 2" --texto "..."
py cli.py forum postar --forum 42 --assunto "Dúvida lista 2" --texto "..." --confirmar
py cli.py forum responder --post 991 --assunto "Re" --arquivo-texto resposta.md

# Tarefa — três passos separados, porque só o terceiro o professor vê
py cli.py tarefa anexar   --assign 3406 --arquivo mapas.pdf --confirmar
py cli.py tarefa salvar   --assign 3406 --itemid 555 --confirmar
py cli.py tarefa entregar --assign 3406 --aceitar-declaracao --confirmar
```

Para texto longo, `--arquivo-texto arquivo.md` em vez de `--texto`.

**Antes de entregar**, confira o rascunho — no Claude: *"qual o status da tarefa
3406?"*, ou `status_tarefa` no MCP.

**Tudo que já saiu no seu nome:**

```bash
py cli.py escritas
```

Guarda inclusive as tentativas que falharam, com o payload exato enviado. Se
algo apareceu no Moodle vindo daqui, está nessa lista.

---

## 7. Automatizar o sync

```bash
py cli.py agendar instalar                       # sync de hora em hora
py cli.py agendar instalar --email voce@gmail.com  # + resumo segunda 07:30
py cli.py agendar status
py cli.py agendar remover
```

Usa **launchd**, não cron. No macOS o cron funciona (testei), mas o launchd é
melhor num laptop: `RunAtLoad` recupera a execução perdida enquanto a máquina
dormia, e o agente sobrevive a reboot. Logs em `~/.ufmg-moodle-mcp/logs/`.

O agente roda `sync`, `extrair` e `programa` em sequência. Token expirado vira
mensagem legível no log, não traceback — você vai ler isso semanas depois.

### Se preferir cron

Quando quiser que rode sozinho, um `cron` a cada hora resolve:

```bash
crontab -e
# 17 * * * * cd "/caminho/para/fenix-ufmg" && .venv/bin/python cli.py sync && .venv/bin/python cli.py extrair
```

O minuto 17 é só para não bater no topo da hora junto com todo mundo.

---

## 8. Conferir se a busca continua boa

A busca é medida contra um gabarito versionado em `avaliacao/`: 39 consultas e
36 tópicos em três matérias, com 400 trechos rotulados.

```bash
py cli.py avaliar                      # mede, grava e compara com a rodada anterior
py cli.py avaliar --curso 6095         # só uma matéria
py cli.py avaliar --sem-rerank         # sem o reranker, mais rápido
py cli.py avaliar --email voce@x.com   # abre rascunho com o relatório
```

Leva uns 100 segundos com o reranker ligado. Cada rodada fica em
`~/.ufmg-moodle-mcp/avaliacao/`, então o relatório sempre mostra a variação
contra a anterior.

Para receber isso todo dia:

```bash
py cli.py agendar instalar --avaliar-email voce@x.com --avaliar-hora 20
```

Duas leituras importantes do relatório. A coluna `s/rót` conta trechos que o
resultado trouxe e que ninguém rotulou ainda — quando ela está alta, a variação
não é confiável, e o próprio relatório avisa. E os rótulos foram feitos pelo
assistente, não por você: são indicativos, e `avaliacao/rotulos.json` é um
arquivo simples de corrigir.

## Quando algo der errado

| Sintoma | O que é |
|---|---|
| busca não acha nada que você sabe que existe | tente a pergunta por extenso (aciona o lado semântico); se persistir, `py cli.py extrair --reindexar` reconstrói o índice |
| prova não aparece na agenda | `py cli.py programa`, depois `py cli.py evento pendentes` para conferir e confirmar |
| `Erro: Nenhum site configurado` | rode `py login_navegador.py` |
| `invalidtoken` / `Token expirado` | token venceu; `py login_navegador.py` de novo (não pede senha, a sessão está salva) |
| `estudo preparar` mostra `campos: (nenhum)` | o tema do site mudou a renderização e o parser quebrou — me traga o HTML de uma questão |
| Material some da busca | veja se está `[precisa OCR]` em `py cli.py materiais` |
| Sync não baixa nada | `py cli.py sync` sem `--sem-download`; se persistir, cheque `py diagnostico.py` |

`py diagnostico.py` responde a pergunta "meu token ainda funciona e o que ele
libera?" — é o primeiro comando a rodar quando algo estranho acontecer.

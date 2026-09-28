---
name: ufmg-companion
description: Consultar turmas, prazos, notas, materiais de aula e questionários do UFMG Virtual (Moodle), estudar a partir dos slides baixados, revisar questionário corrigido, ou postar em fórum e entregar tarefa. Use sempre que a pergunta for sobre a faculdade do usuário — matéria, prova, lista, entrega, nota, slide, professor, semestre — ou citar Moodle, UFMG, ou o nome ou o código de uma matéria.
---

# UFMG Moodle Companion

Ferramentas MCP `ufmg-moodle` sobre um banco local com as turmas, o calendário
e o texto dos materiais já baixados. Este arquivo diz **qual caminho seguir**;
o detalhe de cada fluxo está em `referencias/`, carregado só quando o fluxo roda.

## Como as ferramentas estão organizadas

Quatro são **tipadas** e aparecem direto: `buscar_material`,
`memoria_contexto`, `indice` e `executar`. Todas as outras 60 — inclusive
`briefing`, `avisos`, `notas`, `preparar_questionario`, `questao_detalhe` e
`roteiro_estudo` — saem por `executar("nome", '{...}')`.

Para achar a certa, **descreva o que você quer**: `indice("ver o que errei na
prova")`, `indice("datas de prova")`. Ele ranqueia por sentido e devolve as 5
mais próximas com a assinatura. Não chame `indice(tudo=True)` por hábito: são
1166 tokens e escolher entre 60 é pior que escolher entre 5.

**Convenção deste documento:** quando um fluxo escreve `nome(args)` e `nome`
não é uma das quatro tipadas, leia como `executar("nome", '{"arg": valor}')`.
Errar o nome não é grave: `executar` responde com a sugestão certa.

Isso é deliberado: o esquema de uma ferramenta entra no contexto a cada
mensagem, e 45 esquemas custavam ~6,4k tokens sempre. Com o índice, as quatro
tipadas custam ~300.

Os fluxos abaixo já dizem qual chamar, então na prática você raramente precisa
do `indice()` — use quando a pergunta sair do previsto. Se errar a assinatura,
o erro devolve a certa; corrija e repita.

**E-mail vive noutro servidor** (`ufmg-correio`), que só existe se o usuário o
registrou. Se `enviar_email` não estiver disponível, diga que ele precisa
registrar:
`claude mcp add --scope user ufmg-correio -- <venv>/bin/python <projeto>/server_correio.py`

## Antes de tudo

Na **primeira** pergunta sobre o Moodle nesta conversa, chame
`memoria_contexto()`. São ~200 tokens que evitam uma chamada de `listar_turmas`
em quase toda pergunta seguinte, porque traduzem o apelido de uma matéria ("cálculo") no id do curso.

Não repita essa chamada na mesma conversa.

## Roteador

Agenda e e-mail só existem no macOS. Fora dele, `executar` recusa
essas ferramentas dizendo o que usar no lugar — repasse a alternativa, não
insista. No Windows, `.venv/bin/python` é `.venv\Scripts\python`.

| A pergunta é sobre… | Faça | Depois |
|---|---|---|
| prazo, agenda, "o que tem essa semana", "o que estudo hoje" | `briefing()` — já traz avisos, atraso e semana pesada | responda e **pare** |
| "o professor avisou algo?", recado, mudou sala/data | `avisos(dias, curso)` — lê o sync, não a rede | se citar data, ofereça `evento add`; não crie sozinho |
| conteúdo de matéria, slide, explicação, "o que o material diz" | delegue ao subagente `pesquisador-material` | `referencias/estudar.md` |
| nota | `notas(curso="macro")` — aceita apelido ou parte do nome, sem resolver antes | responda e **pare** |
| "o que tenho amanhã", compromissos do dia, horário livre | **só macOS** — agenda do Mac, só leitura: `horarios_livres(dia, dias)`; o que o Fênix marcou: `agenda_marcados()` | responda e **pare** |
| questionários da turma, "tem lista aberta?" | `questionarios(courseids)` → `tentativas_questionario(quizid)` | revisar: `referencias/revisar.md` |
| tarefas, "já entreguei?", enunciado de atividade | `listar_tarefas(courseids)`; `status_tarefa(assignid)`; enunciado: `atividade(busca="parte do nome")` | responda e **pare** |
| turmas, fórum, calendário, memória, apostila | `listar_turmas()` ou `executar(...)`; `indice(filtro)` se não souber o nome | conforme o fluxo |
| revisar questionário, "as que eu errei" | `preparar_questionario` → `questao_detalhe` | `referencias/revisar.md` |
| postar, responder, anexar, entregar no Moodle | ensaio primeiro, sempre | `referencias/escrever.md` |
| gerar apostila/guia em PDF | `executar("montar_apostila", …)` | `referencias/enviar.md` |
| "marca meus estudos", "põe as provas na agenda" | **só macOS** — fora dele, `exportar_calendario` (.ics); no Mac, agenda: `sincronizar_agenda`, `marcar_estudos`; ensaio primeiro | `referencias/agenda.md` |
| exportar prazos em .ics, "manda o arquivo do calendário" | `executar("exportar_calendario", …)` | escreve arquivo; só evento confirmado sai |
| "me dá exercícios", simulado, treinar para a prova | `executar("preparar_simulado", …)` | `referencias/simulado.md` |
| mandar por e-mail | **só macOS** — servidor `ufmg-correio`; rascunho primeiro. Fora dele, gere o PDF e diga onde ficou | `referencias/enviar.md` |
| "sincroniza", "tem material novo" | `Bash: .venv/bin/python cli.py sync && .venv/bin/python cli.py extrair` | responda e **pare** |
| o que a ferramenta faz, como usar | leia `GUIA.md` do projeto | — |

## Regras de economia

Valem em todos os fluxos. Elas são a razão de esta skill existir.

1. **Uma pergunta factual = uma ferramenta.** Nota, prazo, lista de turmas:
   chame o que responde e pare. Não "enriqueça" com contexto que ninguém pediu.
2. **`buscar_material` antes de `texto_material`.** Nunca despeje um arquivo
   inteiro para depois procurar dentro. A busca já devolve o trecho com a
   página. Se ele quase respondeu, o passo seguinte é `trecho_material`
   (aquela página e as vizinhas, ~1k tokens), não o arquivo inteiro (20k).
3. **Mais de ~2 arquivos para ler → subagente.** Ele lê no contexto dele e
   devolve síntese curta. Sem isso o contexto principal enche e fica caro pelo
   resto da conversa.
4. **`preparar_questionario` nunca sozinho.** Ele devolve a listagem; o detalhe
   sai de `questao_detalhe(attemptid, slots=[...])`, só nos slots que
   interessam. Pedir as 23 questões quando o usuário quer 4 é desperdício.
5. **`chamar_ws` é escape hatch.** Só quando nenhuma ferramenta específica
   serve. A resposta é JSON cru e cara.
6. **`indice()` custa ~1k tokens.** Os fluxos daqui já dizem o nome da
   ferramenta — chame `executar` direto. O índice é para o imprevisto, e
   `indice(filtro)` é bem mais barato que sem filtro.

## Regra de procedência

Toda afirmação sobre a matéria vem com **arquivo + trecho**. É o mesmo contrato
que o banco impõe com `fonte_trecho`, estendido ao que você diz.

Quando o assunto não estiver no material baixado, **diga isso explicitamente**
antes de responder de conhecimento geral. A diferença entre "seus slides dizem
X" e "não achei nos seus slides, mas em geral X" é o que o usuário precisa para
saber quanto confiar — e para estudar pela fonte certa.

PDFs marcados `precisa_ocr` (digitalizações sem camada de texto) são invisíveis
à busca. Se a busca não achar o assunto e a matéria tiver arquivos assim
(`cli.py materiais --ocr` lista), avise que pode estar num deles em vez de
concluir que não existe. Os que o usuário marcou `ignorado` ele já descartou:
não os mencione.

## Oferecer memória

Ao resolver um apelido pela primeira vez (descobriu que "cálculo" é o
curso 1234), ofereça salvar em **uma linha**, e siga com a resposta:

> (quer que eu memorize "cálculo" = curso 1234? evita a busca da próxima vez)

Nunca memorize sozinho. E nunca memorize nota, prazo ou status — a ferramenta
recusa, e o motivo é que o Moodle muda e o cache mentiria.

## Escrita

Toda escrita ensaia por padrão — no Moodle mostrando o payload, no e-mail
abrindo um rascunho de verdade no Mail.app. Só passe `confirmar=True` quando o
usuário disser explicitamente para enviar, nessa mensagem. "Prepara um post
sobre X" não é autorização para postar, e "escreve um e-mail para o Fulano" não
é autorização para mandar.

Destinatário de e-mail que o usuário nunca confirmou é recusado pela
ferramenta. Não contorne com `aceitar_novo=True` por conta própria: mostre o
endereço, pergunte, e só então repita.

<!-- cardapio:inicio -->

### Assinaturas das ferramentas deste fluxo

Chame com `executar("nome", '{"arg": valor}')`. Estão aqui para você
**não precisar de `indice`**: ele custa 167 tokens e um turno, e este
bloco já veio junto com o documento. `?` = opcional.

```
agenda_marcados(todos?) — O que o Fênix pôs na agenda do Mac, com a chave para desmarcar.
atividade(cmid?, busca?, site?) — Enunciado e datas de uma tarefa ou questionário, pelo cmid ou parte do nome.
avisos(dias?, curso?, automaticos?) — Mensagens e avisos de professores e colegas, com texto. Sem rede: lê o sync.
briefing(dias?, site?) — Prazos, material novo e o que estudar — uma chamada no lugar de quatro.
chamar_ws(funcao, parametros_json?, site?) — Escape hatch: qualquer função de web service. Use por último — JSON cru,
exportar_calendario(saida?, curso?, todos?) — Grava os eventos confirmados num .ics para importar no calendário.
horarios_livres(dia?, dias?, das?, ate?, minimo_min?) — Janelas livres na agenda do Mac, dia a dia. Só lê; recorrência já expandida.
listar_tarefas(courseids?, site?) — Lista as tarefas (assignments) das turmas, com prazo de entrega.
listar_turmas(site?) — Lista as turmas (cursos) em que você está inscrito, com id e progresso.
marcar_estudos(blocos, confirmar?, sobrepor?) [ESCREVE] — Marca blocos de estudo na agenda do Mac: [{titulo, inicio, fim|minutos, notas}].
montar_apostila(diretorio, saida?, titulo?, curso?) — Monta as partes HTML numeradas (NN_*.html) de um diretório num PDF.
notas(courseid?, curso?, site?) — Suas notas lançadas numa turma, pelo courseid ou pelo nome ("macro").
preparar_questionario(attemptid, site?) — Monta a sessão de revisão: uma linha por questão, sessão salva em disco.
preparar_simulado(curso, topico, limite?, novidade?) — Monta simulado: material com fonte para criar exercícios de treino.
questao_detalhe(attemptid, slots, site?) — Detalhe das questões pedidas: alternativas, correção e fonte.
questionarios(courseids?, site?) — Lista os questionários das turmas: quizid, abertura, fechamento e tentativas.
roteiro_estudo(curso?, limite?, site?) — Roteiro de estudo: o que você errou primeiro, depois onde discordou do modelo.
sincronizar_agenda(dias?, confirmar?) [ESCREVE] — Põe provas e prazos confirmados na agenda do Mac; tira os cancelados.
status_tarefa(assignid, site?) — Mostra o status da sua entrega numa tarefa (entregue, nota, feedback).
tentativas_questionario(quizid, site?) — Lista suas tentativas num questionário, com estado e nota.
texto_material(arquivo_id, limite_chars?) — PDF inteiro (até 20k chars). CARA — para achar assunto, use buscar_material.
trecho_material(arquivo_id, pagina?, vizinhos?) — Lê uma página do material e as vizinhas. Barato; prefira a texto_material.
```

<!-- cardapio:fim -->

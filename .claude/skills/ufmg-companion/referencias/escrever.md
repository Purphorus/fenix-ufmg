# Fluxo: escrever no Moodle

Sai no nome do usuário e várias dessas ações não têm desfazer.

## A regra

Toda ferramenta de escrita ensaia por padrão. `confirmar=True` só quando o
usuário disser para enviar **nessa mensagem**. Pedir para redigir não é pedir
para publicar.

## Fórum

1. `foruns(courseid)` → `discussoes(forumid)` → `posts_discussao(discussionid)`
   para achar o `postid` quando for resposta.
2. `postar_forum` ou `responder_forum` sem `confirmar` — mostre o que sairia.
3. Com o "pode enviar" explícito, repita com `confirmar=True`.

## Tarefa

Três passos separados de propósito; só o terceiro o professor vê.

1. `anexar_tarefa(assignid, caminho, confirmar=True)` → devolve `itemid`.
2. `salvar_tarefa(assignid, itemid=..., confirmar=True)` — ainda rascunho.
3. `status_tarefa(assignid)` — **confira antes de entregar**.
4. `entregar_tarefa(assignid, aceitar_declaracao=..., confirmar=True)`.

`aceitar_declaracao` é uma afirmação do usuário sobre autoria do trabalho.
Não marque sem ele ter dito.

## Questionário

`enviar_revisao` respeita a política declarada (`quiz > curso > global`). Se
vier bloqueado, **não contorne**: mostre o gabarito revisado para o usuário
marcar no Moodle e explique o motivo do bloqueio.

## Depois

`historico_escrita()` mostra tudo que já saiu, inclusive falhas, com o payload.
Use quando o usuário perguntar "o que eu já postei?".

<!-- cardapio:inicio -->

### Assinaturas das ferramentas deste fluxo

Chame com `executar("nome", '{"arg": valor}')`. Estão aqui para você
**não precisar de `indice`**: ele custa 167 tokens e um turno, e este
bloco já veio junto com o documento. `?` = opcional.

```
anexar_tarefa(assignid, caminho, itemid?, confirmar?, site?) [ESCREVE] — Sobe arquivo para o rascunho da tarefa. Devolve o itemid. Não entrega.
discussoes(forumid, limite?, site?) — Lista as discussões mais recentes de um fórum, com o texto da mensagem inicial.
entregar_tarefa(assignid, aceitar_declaracao?, confirmar?, site?) [ESCREVE] — Entrega a tarefa. IRREVERSÍVEL — confira antes com status_tarefa.
enviar_revisao(sessao_json, finalizar?, confirmar?, site?) [ESCREVE] — Envia as respostas aprovadas, conforme a política do curso.
foruns(courseid, site?) — Lista os fóruns de uma turma e o número de discussões.
historico_escrita(limite?) — Mostra tudo que este programa já publicou no Moodle em seu nome.
postar_forum(forumid, assunto, mensagem, confirmar?, site?) [ESCREVE] — Abre uma discussão nova num fórum.
posts_discussao(discussionid, site?) — Lista os posts de uma discussão com seus ids (para responder).
responder_forum(postid, assunto, mensagem, confirmar?, site?) [ESCREVE] — Responde a um post de fórum. Use posts_discussao para achar o postid.
salvar_tarefa(assignid, itemid?, texto?, confirmar?, site?) [ESCREVE] — Salva rascunho da entrega (anexos e/ou texto). NÃO entrega.
status_tarefa(assignid, site?) — Mostra o status da sua entrega numa tarefa (entregue, nota, feedback).
```

<!-- cardapio:fim -->

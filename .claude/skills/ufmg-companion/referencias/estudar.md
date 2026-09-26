# Fluxo: estudar a partir do material

## Quando

"O que estudar?", "me explica X", "o que os slides dizem sobre Y", "resume o
material de Z".

## Passos

0. **Antes de montar qualquer documento, veja o que já existe.**
   `cobertura(curso)` diz quais arquivos já têm resumo, quais herdariam resumo
   de um arquivo irmão, e que documentos já foram montados. Reaproveitar
   economiza de 60% a 92% de token — mais que qualquer escolha de busca, que
   rende 21%. Para pergunta pontual, pule este passo.

1. **Resolva a matéria.** `memoria_contexto()` já deu os apelidos. Sem apelido,
   `listar_turmas()` uma vez — e ofereça memorizar depois.

2. **Existe resumo guardado?** `resumo_material(arquivo_id)` devolve o resumo
   se o arquivo não mudou desde que foi feito. Se devolver, use: já está pago.
   Ela também distingue três casos: resumo válido, resumo que morreu porque o
   professor republicou (refazer é barato, o assunto já é conhecido), e nunca
   resumido. Nesse último caso ela ainda aponta o arquivo irmão que tem resumo,
   quando existe — material republicado sob outro nome é comum.

3. **Uma pergunta pontual** (um termo, um conceito): `buscar_material(termo,
   curso)` resolve. O resultado já vem com arquivo e página, e é isso que você
   cita. **Pare aqui.**

   A busca é híbrida: casa o termo exato (BM25) e o sentido (vetor). O formato
   da pergunta muda o que ela usa, então formule como você perguntaria:
   - **termo técnico** ("estado estacionário", "IS-LM") → busque o termo seco;
     é aí que o casamento exato acerta.
   - **assunto em palavras suas** ("por que a inflação sobe quando o governo
     gasta mais") → mande a frase inteira, não as palavras-chave. A frase
     completa é o que liga a pergunta ao slide que a responde com outras
     palavras. Encurtar para duas palavras desliga o lado semântico.

4. **O trecho quase respondeu?** `trecho_material(arquivo_id, pagina)` lê
   aquela página e as vizinhas por ~1k tokens. Não pule daí para
   `texto_material`, que traz o PDF inteiro por 20k.

5. **Montar resumo ou apostila de um assunto**, e não responder uma pergunta:
   `cobrir_topico(termo, curso, documento="Prova 2")`. Passar `documento`
   registra o que foi usado; depois, `novidade=True` devolve só o que ainda
   não entrou ali. Medido: 2530 tokens para montar, 394 para ampliar. Ela atravessa para os arquivos vizinhos e
   traz material que a busca por termo não alcança — medido, 10 arquivos de
   Macro contra 5. Custa mais e é menos precisa, então **não** a use para
   pergunta pontual; ali `buscar_material` ganha.

6. **Pergunta que exige comparar ou percorrer vários arquivos**: delegue ao
   subagente `pesquisador-material`. Passe a pergunta, o curso e o que já se
   sabe. Ele devolve síntese curta com citações.

7. **Se o subagente produziu um resumo de arquivo inteiro**, guarde:
   `guardar_resumo(arquivo_id, resumo)`. Chaveado por sha256, morre sozinho se
   o professor republicar.

## Quando a busca avisa

Se o resultado começar com ⚠, o material menciona o assunto mas não o ensina —
tipicamente bibliografia, ementa ou vizinhança temática. **Repasse o aviso ao
usuário** e não monte questão sobre aquilo sem ele confirmar. Costuma ser
assunto que está nos PDFs digitalizados, fora da busca.

## O que não fazer

- `texto_material` de cara. São 20k+ tokens de um PDF inteiro no contexto
  principal, e a busca quase sempre bastava. A escada é
  `buscar_material` → `trecho_material` → `texto_material`, nessa ordem.
- `cobrir_topico` para pergunta pontual. Ela existe para cobrir assunto; numa
  pergunta direta traz material vizinho que só ocupa espaço.
- Encadear buscas por sinônimos "para garantir". Duas buscas sem resultado já
  indicam que o assunto não está no material — diga isso.
- Responder de conhecimento geral sem avisar que não veio dos slides.

## Roteiro de estudo dirigido

Quando a pergunta for "o que estudar hoje", `roteiro_estudo()` já ordena pelo
que o usuário menos sabe: errou primeiro, divergiu depois. Combine com o
material só para as questões do topo — não para todas.

<!-- cardapio:inicio -->

### Assinaturas das ferramentas deste fluxo

Chame com `executar("nome", '{"arg": valor}')`. Estão aqui para você
**não precisar de `indice`**: ele custa 167 tokens e um turno, e este
bloco já veio junto com o documento. `?` = opcional.

```
cobertura(curso?) — O que já foi resumido ou montado, e o que herdaria resumo pelo grafo.
cobrir_topico(termo, curso?, limite?, documento?, novidade?, semestre?) — Material de vários arquivos sobre um tópico, para montar resumo ou apostila.
guardar_resumo(arquivo_id, resumo, site?) — Guarda resumo de material (chave: sha256 — morre se o arquivo mudar).
listar_turmas(site?) — Lista as turmas (cursos) em que você está inscrito, com id e progresso.
resumo_material(arquivo_id) — Resumo já guardado deste material, se o arquivo não mudou desde então.
roteiro_estudo(curso?, limite?, site?) — Roteiro de estudo: o que você errou primeiro, depois onde discordou do modelo.
texto_material(arquivo_id, limite_chars?) — PDF inteiro (até 20k chars). CARA — para achar assunto, use buscar_material.
trecho_material(arquivo_id, pagina?, vizinhos?) — Lê uma página do material e as vizinhas. Barato; prefira a texto_material.
```

<!-- cardapio:fim -->

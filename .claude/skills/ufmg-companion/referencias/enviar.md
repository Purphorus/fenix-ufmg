# Fluxo: apostila e e-mail

## Gerar material de estudo em PDF

O formato que o usuário quer, e o porquê de cada parte:

- explicação detalhada por tópico;
- **pelo menos 5 exercícios por matéria, em dificuldade crescente**;
- **resoluções comentadas reunidas no fim do arquivo**, não junto de cada
  exercício — ele resolve antes de ler a resposta, e gabarito intercalado
  estraga isso;
- **citação de onde está no slide**, para ele conferir contra a fonte.

Exercício reaproveitado da lista do Moodle vale mais que inventado: use
`preparar_questionario` + `questao_detalhe` nos slots que ele errou, porque
mostram o padrão de erro real.

Passos:

1. `executar("novo_projeto_apostila", '{"diretorio": "...", "titulo": "..."}')`
2. Escreva as partes como `01_capa.html`, `02_topico.html`, …, gabarito por
   último. Matemática com `<sup>`/`<sub>` e grego unicode; **não use MathJax**,
   depende de rede e falha na impressão.
3. `executar("montar_apostila", '{"diretorio": "..."}')` → PDF.

Apostilas ficam em `~/.ufmg-moodle-mcp/apostilas/<nome>/`, não no repositório.

## Enviar por e-mail

As ferramentas de e-mail estão no servidor **`ufmg-correio`**, separado, porque
o esquema delas custa ~680 tokens em toda mensagem e e-mail é pouco frequente.
Se não estiverem disponíveis, o usuário precisa registrar o servidor — diga
isso em vez de tentar contornar.

1. `remetentes_email()` se houver dúvida sobre de qual conta mandar. Com uma só
   conta, `de` pode ser omitido; com várias, a ferramenta exige escolha em vez
   de adivinhar.
2. `contatos_email()` para ver quem já está confirmado.
3. `enviar_email(para, assunto, corpo, anexos=[pdf])` **sem `confirmar`** —
   abre um rascunho no Mail.app, que é o ensaio.
4. Só com o "pode mandar" explícito, repita com `confirmar=True`.

Destinatário novo é recusado com o endereço na mensagem. Mostre o endereço ao
usuário, confirme, e repita com `aceitar_novo=True`. Depois disso ele fica
conhecido e não pergunta mais.

Escreva o corpo do e-mail você mesmo — assunto claro, corpo curto dizendo o que
vai anexado e por quê. Não copie o roteiro de estudo inteiro no corpo se ele já
está no PDF.

## Depois

`historico_escrita()` mostra e-mails junto com as escritas no Moodle: as ações
são `email_rascunho` e `email_enviar`, com destinatário, assunto e anexos.

<!-- cardapio:inicio -->

### Assinaturas das ferramentas deste fluxo

Chame com `executar("nome", '{"arg": valor}')`. Estão aqui para você
**não precisar de `indice`**: ele custa 167 tokens e um turno, e este
bloco já veio junto com o documento. `?` = opcional.

```
exportar_calendario(saida?, curso?, todos?) — Grava os eventos confirmados num .ics para importar no calendário.
historico_escrita(limite?) — Mostra tudo que este programa já publicou no Moodle em seu nome.
montar_apostila(diretorio, saida?, titulo?, curso?) — Monta as partes HTML numeradas (NN_*.html) de um diretório num PDF.
novo_projeto_apostila(diretorio, titulo?) — Cria diretório de apostila com o cabeçalho de estilo pronto.
preparar_questionario(attemptid, site?) — Monta a sessão de revisão: uma linha por questão, sessão salva em disco.
questao_detalhe(attemptid, slots, site?) — Detalhe das questões pedidas: alternativas, correção e fonte.
```

<!-- cardapio:fim -->


## Exportar os prazos para o calendário

`exportar_calendario(saida="", curso=0, todos=False)` grava um .ics em
`~/Downloads/ufmg.ics` e o usuário importa no calendário dele.

**Só evento confirmado sai.** Data que o Moodle deu sozinho, ou que saiu por
regex de tabela em PDF, ainda não é compromisso — no calendário ela viraria
uma prova que talvez não exista naquele dia. Se a exportação voltar vazia,
não force com `todos=True`: mostre os eventos não confirmados, pergunte
quais conferem, e confirme os que ele disser. `todos=True` é para quando ele
pedir explicitamente a agenda inteira, e aí cada evento automático vai com
"[não confirmado]" no título.

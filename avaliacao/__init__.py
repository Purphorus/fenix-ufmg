"""Banco de avaliação da recuperação: consultas, rótulos e o medidor.

Viveu num diretório temporário durante todo o desenvolvimento e quase foi
perdido. Agora é parte do repositório, porque sem ele qualquer mudança na
busca vira opinião: nesta mesma investigação eu projetei de 5 a 8 pontos de
ganho para a limpeza de ruído e o medido foi zero.

Os rótulos são chaveados pelo **sha do texto do trecho**, nunca pelo id: o id
é reciclado a cada `extrair --reindexar` e invalidou tudo três vezes.

**Os rótulos foram feitos pelo assistente, não pelo usuário.** São indicativos.
Se o usuário revisar, `rotulos.json` é um arquivo simples de editar.
"""

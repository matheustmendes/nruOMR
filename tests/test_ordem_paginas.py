"""
Testes da reconstrução da numeração de páginas.

Este é o ponto onde um erro é mais caro e mais invisível. O scanner entrega o
maço da última folha para a primeira, e o OCR do número de página falha em
algumas folhas (3 de 11 num scan real de Ondina). A lógica anterior era
tudo-ou-nada: bastava uma falha para o sistema usar a ordem do arquivo — ou
seja, invertida — e cada pessoa receber a presença de outra, sem nenhum sinal
de erro.

Rodar:
    venv\\Scripts\\pytest tests\\
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from exportar import _inferir_numeros_paginas as inferir


class TestReconstrucao:

    def test_scan_invertido_com_falhas_de_ocr(self):
        """Caso real: Ondina, 11 páginas, OCR falhou em 3."""
        numeros, aviso = inferir([None, 10, 9, None, None, 6, 5, 4, 3, 2, 1])

        assert numeros == [11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1]
        assert "3 página(s)" in aviso

    def test_maco_real_completo_de_ondina(self):
        """
        Caso real inteiro: 3 arquivos, 27 páginas, todas invertidas, 5 páginas
        que o OCR não leu e uma que ele leu **errado** — a página 14 saiu como
        "4", porque o traço do "1" se perdeu.
        """
        detectados = (
            [None, 10, 9, None, None, 6, 5, 4, 3, 2, 1]     # arquivo 1: 11..1
            + [20, 19, 18, 17, 16, 15, 4, 13, None]         # arquivo 2: 20..12
            + [27, 26, None, 24, 23, None, 21]              # arquivo 3: 27..21
        )
        esperado = (list(range(11, 0, -1))
                    + list(range(20, 11, -1))
                    + list(range(27, 20, -1)))

        numeros, aviso = inferir(detectados)

        assert numeros == esperado
        assert "corrigidos pela sequência" in aviso

    def test_desempate_prefere_cluster_apertado_a_coincidencia_espalhada(self):
        """
        Caso real: Canela 01/06 a 06/06, 3 arquivos concatenados (10+8+5
        páginas). Duas leituras erradas ('11' virou '14' e '15' virou '19')
        fizeram um par espúrio (a '14' errada, a '19' verdadeira — cinco
        posições de distância) empatar em votos com o par genuíno (20,19,
        adjacentes). Preferir o span maior no desempate escolhia a
        coincidência e travava a reconstrução inteira ("números repetidos");
        a correção exige span MENOR no empate.
        """
        detectados = (
            [10, 9, 8, 7, None, 9, 4, 3, 2, 1]          # arquivo 1: 10..1
            + [18, 17, 16, 19, 14, None, 12, 14]         # arquivo 2: 18..11
            + [None, None, None, 20, 19]                 # arquivo 3: 23..19
        )

        numeros, aviso = inferir(detectados)

        assert numeros is not None
        assert sorted(numeros) == list(range(1, 24))
        assert numeros[:10] == list(range(10, 0, -1))
        assert numeros[10:18] == list(range(18, 10, -1))
        assert numeros[18:23] == list(range(23, 18, -1))
        assert "corrigidos" in aviso

    def test_ultima_pagina_fora_de_ordem_resolvida_por_eliminacao(self):
        """
        Caso real: Canela 13/07 a 17/07. Um arquivo de redigitalização trouxe
        as páginas 23,22,20,19 e só então a 21 — fora de sequência física, não
        é erro de leitura. A extensão por vizinhança não alcança essa posição
        (o vizinho mais próximo já preenchido não é o vizinho físico real).
        Mas sobrando uma única posição vazia e um único número ainda não
        usado, não há ambiguidade: só existe um valor possível.
        """
        detectados = (
            [10, 9, 8, None, 6, None, 4, 3, 2, 1]    # arquivo 1: 10..1, 2 falhas
            + [18, 17, 16, 15, 14, 13, 12, 11]        # arquivo 2: 18..11, completo
            + [23, 22, 20, 19, 21]                    # arquivo 3: fora de ordem
        )

        numeros, aviso = inferir(detectados)

        assert numeros is not None
        assert sorted(numeros) == list(range(1, 24))
        assert numeros[18:23] == [23, 22, 20, 19, 21]

    def test_leitura_errada_e_sobreposta_pela_sequencia(self):
        """
        Um número errado é mais perigoso do que um ausente: parece confiável.
        A progressão que explica a maioria das leituras vence, e a discordante
        é descartada.
        """
        numeros, aviso = inferir([5, 4, 9, 2, 1])

        assert numeros == [5, 4, 3, 2, 1]
        assert "mal lidos" in aviso

    def test_sequencia_ascendente_com_buracos(self):
        numeros, _ = inferir([1, None, 3, None, 5])
        assert numeros == [1, 2, 3, 4, 5]

    def test_dois_macos_concatenados(self):
        """Cada arquivo é uma progressão; a lista é quebrada em trechos."""
        numeros, _ = inferir([11, 10, 9, None, 20, 19, 18])
        assert numeros == [11, 10, 9, 8, 20, 19, 18]

    def test_tudo_detectado_passa_intacto(self):
        numeros, aviso = inferir([3, 2, 1])
        assert numeros == [3, 2, 1]
        assert aviso == ""

    def test_extrapola_para_as_pontas(self):
        numeros, _ = inferir([None, None, 3, 4, None])
        assert numeros == [1, 2, 3, 4, 5]


class TestRecusa:
    """
    Adivinhar aqui é pior do que desistir: um número errado troca a presença de
    25 pessoas de uma vez. Nestes casos a função devolve None e o chamador
    mantém a ordem do arquivo, avisando.
    """

    def test_uma_ancora_so_nao_basta(self):
        numeros, motivo = inferir([None, None, 5, None])
        assert numeros is None
        assert "duas páginas" in motivo

    def test_nenhuma_ancora(self):
        numeros, _ = inferir([None, None, None])
        assert numeros is None

    def test_sequencia_incoerente(self):
        numeros, motivo = inferir([1, None, 7, None, 2])
        assert numeros is None
        assert "reconstruível" in motivo

    def test_lista_vazia(self):
        numeros, motivo = inferir([])
        assert numeros is None
        assert "nenhuma página" in motivo

    def test_nunca_produz_numero_repetido(self):
        """Duas páginas com o mesmo número dariam presença duplicada."""
        for entrada in ([1, None, 1], [5, 5, None], [2, None, 2, None]):
            numeros, _ = inferir(entrada)
            if numeros is not None:
                assert len(set(numeros)) == len(numeros), entrada

    def test_nunca_produz_pagina_menor_que_um(self):
        for entrada in ([2, 1, None], [1, None, None, None]):
            numeros, _ = inferir(entrada)
            if numeros is not None:
                assert all(n >= 1 for n in numeros), entrada

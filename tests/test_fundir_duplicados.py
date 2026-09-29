"""
Testes de `fundir_duplicados.unir_contagens` — a função pura que decide o
resultado da união de duas listas duplicadas.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fundir_duplicados import unir_contagens

DIAS = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta"]


def _detalhe(presente, almoco=None, janta=None):
    if almoco is None:
        almoco = presente
    if janta is None:
        janta = False
    return {"presente": presente, "almoco": almoco, "janta": janta}


def _contagem(numero, marcas):
    """marcas: {dia: bool presente} — vira detalhes completos para todos os DIAS."""
    detalhes = {d: _detalhe(marcas.get(d, False)) for d in DIAS}
    return {
        "numero": numero,
        "presencas": sum(1 for v in detalhes.values() if v["presente"]),
        "detalhes": detalhes,
    }


class TestUnirContagens:

    def test_uniao_por_or_dia_a_dia(self):
        a = [_contagem(1, {"Segunda": True, "Terça": False})]
        b = [_contagem(1, {"Segunda": False, "Terça": True})]

        unida, _ = unir_contagens(a, b, DIAS)

        assert unida[0]["detalhes"]["Segunda"]["presente"] is True
        assert unida[0]["detalhes"]["Terça"]["presente"] is True
        assert unida[0]["presencas"] == 2

    def test_semana_cheia_numa_lista_e_zerada_na_outra(self):
        """O caso relatado: 5 presenças numa lista, 0 na outra, mesma semana."""
        a = [_contagem(1, {d: True for d in DIAS})]
        b = [_contagem(1, {})]

        unida, diferencas = unir_contagens(a, b, DIAS)

        assert unida[0]["presencas"] == 5
        assert diferencas == [{
            "numero": 1, "presencas_a": 5, "presencas_b": 0, "presencas_unida": 5,
        }]

    def test_pessoa_so_na_lista_a_e_tratada_como_ausente_na_b(self):
        a = [_contagem(3, {"Segunda": True})]
        b = []

        unida, diferencas = unir_contagens(a, b, DIAS)

        assert len(unida) == 1
        assert unida[0]["numero"] == 3
        assert unida[0]["presencas"] == 1
        assert diferencas == [{
            "numero": 3, "presencas_a": 1, "presencas_b": 0, "presencas_unida": 1,
        }]

    def test_pessoa_so_na_lista_b(self):
        a = []
        b = [_contagem(2, {"Sexta": True})]

        unida, diferencas = unir_contagens(a, b, DIAS)

        assert unida[0]["numero"] == 2
        assert unida[0]["presencas"] == 1
        assert diferencas[0]["presencas_a"] == 0
        assert diferencas[0]["presencas_b"] == 1

    def test_sem_diferenca_quando_as_duas_batem(self):
        a = [_contagem(1, {"Segunda": True})]
        b = [_contagem(1, {"Segunda": True})]

        unida, diferencas = unir_contagens(a, b, DIAS)

        assert diferencas == []
        assert unida[0]["presencas"] == 1

    def test_almoco_e_janta_sao_unidos_independentemente(self):
        a = [_contagem(1, {})]
        b = [_contagem(1, {})]
        a[0]["detalhes"]["Segunda"] = _detalhe(True, almoco=True, janta=False)
        b[0]["detalhes"]["Segunda"] = _detalhe(True, almoco=False, janta=True)

        unida, _ = unir_contagens(a, b, DIAS)

        seg = unida[0]["detalhes"]["Segunda"]
        assert seg["almoco"] is True
        assert seg["janta"] is True
        assert seg["presente"] is True

    def test_resultado_ordenado_por_numero(self):
        a = [_contagem(5, {}), _contagem(1, {})]
        b = [_contagem(3, {})]

        unida, _ = unir_contagens(a, b, DIAS)

        assert [c["numero"] for c in unida] == [1, 3, 5]

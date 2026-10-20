"""
Testes de `completar_paginas_ausentes`: página perdida no maço.

A regra que importa: só presumir presença quando dá para saber com segurança
qual página faltou. Presumir na página errada dá presença à pessoa errada.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from exportar import completar_paginas_ausentes

DIAS = ["Segunda", "Terça"]
POR_PAGINA = 2


def _lote(total_paginas):
    alunos = [
        {"numero": n, "pagina": (n - 1) // POR_PAGINA + 1,
         "nome": f"Aluno {n}", "matricula": str(n)}
        for n in range(1, total_paginas * POR_PAGINA + 1)
    ]
    return {"alunos": alunos, "dias": DIAS, "alunos_por_pagina": POR_PAGINA,
            "total_paginas": total_paginas}


def _lidos(paginas):
    """Resultados de bolhas (nada marcado) só das páginas informadas."""
    vazio = {"almoco": False, "janta": False, "almoco_pct": 0.0, "janta_pct": 0.0}
    return [
        {"numero": (p - 1) * POR_PAGINA + i, "dias": {d: dict(vazio) for d in DIAS}}
        for p in paginas for i in range(1, POR_PAGINA + 1)
    ]


class TestPaginaAusente:

    def test_scan_completo_nao_muda_nada(self):
        res, info = completar_paginas_ausentes(
            _lidos([1, 2, 3]), _lote(3), {"ordem_confiavel": True})

        assert len(res) == 6
        assert info["ausentes"] == [] and info["avisos"] == []

    def test_pagina_do_meio_ausente_marca_todos_os_dias_das_pessoas_dela(self):
        res, info = completar_paginas_ausentes(
            _lidos([1, 3]), _lote(3), {"ordem_confiavel": True})

        novos = [r for r in res if r.get("presumida")]
        assert [r["numero"] for r in novos] == [3, 4]
        assert info["ausentes"] == [2] and info["presumidas"] == 2
        for r in novos:
            for d in DIAS:
                assert r["dias"][d]["almoco"] and r["dias"][d]["janta"]
        # Quem veio no scan segue como foi lido
        assert not any(r.get("presumida") for r in res if r["numero"] in (1, 2, 5, 6))

    def test_primeira_e_ultima_pagina_ausentes(self):
        res, info = completar_paginas_ausentes(
            _lidos([2]), _lote(3), {"ordem_confiavel": True})

        assert info["ausentes"] == [1, 3]
        assert sorted(r["numero"] for r in res if r.get("presumida")) == [1, 2, 5, 6]

    def test_ordem_nao_confiavel_nao_presume_nada(self):
        res, info = completar_paginas_ausentes(
            _lidos([1, 3]), _lote(3), {"ordem_confiavel": False})

        assert not any(r.get("presumida") for r in res)
        assert info["ambiguas"] == [2] and info["avisos"]

    def test_ausente_colada_em_pagina_sem_numero_lido_e_ambigua(self):
        # "1,2,X,5,6": a X pode ser a 3 ou a 4 — não dá para saber qual faltou
        res, info = completar_paginas_ausentes(
            _lidos([1, 3, 4]), _lote(4),
            {"ordem_confiavel": True, "paginas_sem_leitura": [3]})

        assert not any(r.get("presumida") for r in res)
        assert info["ambiguas"] == [2] and info["ausentes"] == []

    def test_pagina_sem_numero_longe_da_ausente_nao_atrapalha(self):
        res, info = completar_paginas_ausentes(
            _lidos([1, 2, 4]), _lote(4),
            {"ordem_confiavel": True, "paginas_sem_leitura": [1]})

        assert info["ausentes"] == [3]

    def test_sem_lote_ou_sem_leitura_nao_faz_nada(self):
        assert completar_paginas_ausentes(_lidos([1]), None)[1]["ausentes"] == []
        assert completar_paginas_ausentes([], _lote(3))[1]["ausentes"] == []

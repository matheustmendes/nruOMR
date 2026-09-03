"""
ler_bolhas.py

Lê os círculos de presença da imagem alinhada usando as posições
do config.yaml. Retorna uma lista de resultados por aluno.

Uso standalone (debug):
    python ler_bolhas.py <scan.pdf ou scan.png> <config.yaml> [pagina_pdf] [pagina_lista]
"""

import sys
import cv2
import numpy as np
import yaml

# Defaults globais — sobrescritos por scan.offset_y / scan.threshold no config.yaml
OFFSET_Y = -3
THRESHOLD = 0.40

# Fração do raio usada para medir o interior do círculo. O valor tem que ser
# pequeno o bastante para não encostar na borda impressa (1.5pt ≈ 4px a 200dpi)
# nem sofrer com o desalinhamento residual do scanner.
MEDICAO_FRACAO = 0.65

# "interior" mede só o miolo do círculo; "quadrado" reproduz o comportamento
# antigo (ROI quadrada de lado 2r), mantido apenas para reprocessar leituras
# calibradas com o método anterior.
MEDICAO = "interior"

# Calibração padrão por modo de medição. Os dois conjuntos NÃO são
# intercambiáveis: no modo "quadrado" a borda impressa já responde por ~29% da
# área medida, então o ponto de corte precisa ficar alto; no modo "interior" a
# borda sai da conta e um círculo vazio lê perto de zero.
#
# Valores do modo "interior" calibrados sobre scan real (500 bolhas):
#   vazios      → 0,00–0,09  (384 de 500 abaixo de 0,05)
#   preenchidos → 0,27–0,93  (mediana ~0,68; ninguém preenche o círculo inteiro)
# O corte em 0,20 fica no meio do vão, e a zona ambígua 0,10–0,45 cobre a cauda
# baixa das marcações reais, que é onde mora a dúvida de verdade.
CALIBRACAO = {
    "interior": {"threshold": 0.20, "margem_abaixo": 0.10, "margem_acima": 0.25},
    "quadrado": {"threshold": 0.40, "margem_abaixo": 0.04, "margem_acima": 0.10},
}

# Cache dos discos de medição por raio — a máscara é a mesma para todos os
# círculos de uma página e recalculá-la milhares de vezes é desperdício.
_MASCARAS = {}


def _get_offset_y(config: dict) -> float:
    return config.get("scan", {}).get("offset_y", OFFSET_Y)


def _get_medicao(config: dict) -> str:
    return config.get("scan", {}).get("medicao", MEDICAO)


def _get_medicao_fracao(config: dict) -> float:
    return config.get("scan", {}).get("medicao_fracao", MEDICAO_FRACAO)


def _calibracao(config: dict) -> dict:
    return CALIBRACAO.get(_get_medicao(config), CALIBRACAO["quadrado"])


def _get_threshold(config: dict) -> float:
    """
    Ponto de corte marcado/vazio.

    Um `scan.threshold` explícito no config sempre vence; sem ele, o padrão
    acompanha o modo de medição, porque o mesmo número significa coisas
    diferentes nos dois modos.
    """
    return config.get("scan", {}).get("threshold", _calibracao(config)["threshold"])


def get_zona_ambigua(config: dict):
    """
    Faixa de percentuais que vai para conferência humana: (mínimo, máximo).

    Centralizada aqui porque antes cada chamador remontava a faixa com os
    mesmos dois `.get()` — e bastava um deles divergir para a contagem exibida
    não bater com a lista de casos apresentada na revisão.
    """
    scan = config.get("scan", {})
    cal = _calibracao(config)
    threshold = _get_threshold(config)
    abaixo = scan.get("ambiguo_margem_abaixo", cal["margem_abaixo"])
    acima = scan.get("ambiguo_margem_acima", cal["margem_acima"])
    return threshold - abaixo, threshold + acima


def _mascara_disco(raio_px: float):
    """Máscara booleana circular de raio `raio_px`, centrada na ROI."""
    chave = round(raio_px, 2)
    if chave not in _MASCARAS:
        lado = max(1, int(round(raio_px * 2)))
        yy, xx = np.ogrid[:lado, :lado]
        centro = (lado - 1) / 2.0
        _MASCARAS[chave] = (yy - centro) ** 2 + (xx - centro) ** 2 <= raio_px ** 2
    return _MASCARAS[chave]


def mm_para_px(valor_mm: float, dpi: int = 200) -> float:
    """Converte milímetros para pixels dado um DPI."""
    return (valor_mm / 25.4) * dpi


def carregar_posicoes(config: dict) -> dict:
    """
    Extrai do config.yaml as posições de todos os círculos em pixels.

    Returns:
        dict com:
            - dias: lista de nomes dos dias
            - circulos_x: dict[dia] -> (almoco_px, janta_px)
            - primeira_linha_y_px: Y do primeiro aluno
            - linha_altura_px: distância vertical entre alunos
            - raio_px: raio do círculo em pixels
            - alunos_por_pagina: quantos alunos por página
    """
    dpi = config["scan"]["dpi"]
    layout = config["layout"]
    circulos = config["circulos"]

    dias = layout["dias"]

    circulos_x = {}
    for dia, pos in layout["circulos_por_dia"].items():
        circulos_x[dia] = (
            mm_para_px(pos["almoco_x"], dpi),
            mm_para_px(pos["janta_x"], dpi),
        )

    return {
        "dias": dias,
        "circulos_x": circulos_x,
        "primeira_linha_y_px": mm_para_px(layout["primeira_linha_y_mm"], dpi),
        "linha_altura_px": mm_para_px(layout["linha_altura_mm"], dpi),
        "raio_px": mm_para_px(circulos["raio_mm"], dpi),
        "alunos_por_pagina": layout["alunos_por_pagina"],
    }


def ler_circulo(binary: np.ndarray, cx: float, cy: float, raio_px: float,
                medicao: str = MEDICAO, fracao: float = MEDICAO_FRACAO) -> float:
    """
    Analisa um único círculo e retorna a proporção de marcação.

    Na imagem BINARY_INV:
      - pixels 255 (branco) = tinta/marcação
      - pixels 0 (preto) = fundo

    Por que medir só o interior
    ---------------------------
    A versão original media uma ROI quadrada de lado 2r, que inclui a borda
    impressa do círculo inteira. Essa borda sozinha ocupa cerca de 29% da área
    medida (verificado em scan real: círculos vazios liam 0,26–0,30 contra um
    threshold de 0,40). A folga até a zona ambígua era de apenas ~0,07 — e
    qualquer scan um pouco mais escuro, ou uma impressão com traço mais grosso,
    empurrava *todos* os círculos vazios para dentro dela de uma vez. É essa a
    origem dos "252 casos ambíguos num scan de 25 nomes": não é contagem
    errada, é a medição encostando na borda.

    Medindo um disco de 0,65·r, a borda fica de fora: no mesmo scan os vazios
    passam a ler ~0,02 e os preenchidos ~0,90. O threshold de 0,40 continua
    válido, agora com folga dos dois lados.

    Args:
        binary: imagem binarizada (BINARY_INV)
        cx, cy: centro do círculo em pixels
        raio_px: raio do círculo impresso em pixels
        medicao: "interior" (padrão) ou "quadrado" (comportamento antigo)
        fracao: fração do raio medida no modo "interior"

    Returns:
        float entre 0 e 1 — proporção de pixels marcados
    """
    h, w = binary.shape[:2]

    if medicao == "quadrado":
        x1 = max(0, int(cx - raio_px))
        y1 = max(0, int(cy - raio_px))
        x2 = min(w, int(cx + raio_px))
        y2 = min(h, int(cy + raio_px))
        roi = binary[y1:y2, x1:x2]
        return (np.count_nonzero(roi) / roi.size) if roi.size else 0.0

    raio_int = raio_px * fracao
    lado = max(1, int(round(raio_int * 2)))

    x1 = int(round(cx - raio_int))
    y1 = int(round(cy - raio_int))

    # Fora da página: sem pixel para medir.
    if x1 < 0 or y1 < 0 or x1 + lado > w or y1 + lado > h:
        return 0.0

    roi = binary[y1:y1 + lado, x1:x1 + lado]
    mascara = _mascara_disco(raio_int)
    total = np.count_nonzero(mascara)
    if total == 0:
        return 0.0

    return np.count_nonzero(roi[mascara]) / total


def ler_pagina(binary: np.ndarray, config: dict, num_alunos: int,
               threshold: float = None) -> list:
    """
    Lê todas as bolhas de uma página.

    Args:
        binary: imagem binarizada e alinhada (BINARY_INV)
        config: dict do config.yaml
        num_alunos: quantos alunos tem nessa página
        threshold: proporção mínima pra considerar marcado

    Returns:
        Lista de dicts, um por aluno:
        {
            "numero": 1,
            "dias": {
                "Segunda": {"almoco": True, "janta": False, "almoco_pct": 0.72, "janta_pct": 0.08},
                ...
            }
        }
    """
    pos = carregar_posicoes(config)
    if threshold is None:
        threshold = _get_threshold(config)
    offset_y = _get_offset_y(config)
    medicao = _get_medicao(config)
    fracao = _get_medicao_fracao(config)
    resultados = []

    for i in range(num_alunos):
        cy = pos["primeira_linha_y_px"] + offset_y + i * pos["linha_altura_px"]

        aluno = {
            "numero": i + 1,
            "dias": {},
        }

        for dia in pos["dias"]:
            ax, jx = pos["circulos_x"][dia]

            almoco_pct = ler_circulo(binary, ax, cy, pos["raio_px"], medicao, fracao)
            janta_pct = ler_circulo(binary, jx, cy, pos["raio_px"], medicao, fracao)

            aluno["dias"][dia] = {
                "almoco": almoco_pct >= threshold,
                "janta": janta_pct >= threshold,
                "almoco_pct": round(almoco_pct, 3),
                "janta_pct": round(janta_pct, 3),
            }

        resultados.append(aluno)

    return resultados


def imprimir_resultados(resultados: list, dias: list):
    """Imprime os resultados em formato legível."""
    # Cabeçalho
    header = f"{'Nº':>3} |"
    for dia in dias:
        header += f" {dia[:3]:>3} A | {dia[:3]:>3} J |"
    print(header)
    print("-" * len(header))

    for aluno in resultados:
        linha = f"{aluno['numero']:>3} |"
        for dia in dias:
            d = aluno["dias"][dia]
            a = "●" if d["almoco"] else "○"
            j = "●" if d["janta"] else "○"
            linha += f"  {a} {d['almoco_pct']:.0%} |  {j} {d['janta_pct']:.0%} |"
        print(linha)


# --- DEBUG VISUAL ---

# def debug_visual(img, binary, config, resultados):
#     """
#     Abre no navegador a imagem com os círculos destacados:
#     - Verde = detectado como marcado
#     - Vermelho = detectado como vazio
#     - Amarelo = ambíguo (perto do threshold)
#     """
#     import base64
#     import webbrowser
#     import tempfile

#     pos = carregar_posicoes(config)
#     vis = img.copy()

#     threshold = THRESHOLD
#     margem = 0.05 # zona ambígua

#     for aluno in resultados:
#         i = aluno["numero"] - 1
#         cy = int(pos["primeira_linha_y_px"] + OFFSET_Y + i * pos["linha_altura_px"])
#         raio = int(pos["raio_px"])

#         for dia in pos["dias"]:
#             ax, jx = pos["circulos_x"][dia]

#             for cx_float, tipo in [(ax, "almoco"), (jx, "janta")]:
#                 cx = int(cx_float)
#                 pct = aluno["dias"][dia][f"{tipo}_pct"]
#                 marcado = aluno["dias"][dia][tipo]

#                 # Cor baseada na confiança
#                 # +margem
#                 if pct > threshold + margem:
              
#                     cor = (0, 200, 0)      # verde — claramente marcado
#                     # -margem
#                 elif pct < threshold - margem:
#                     cor = (180, 180, 180)   # cinza — claramente vazio
#                 else:
#                     cor = (0, 200, 255)     # amarelo — ambíguo

#                 cv2.circle(vis, (cx, cy), raio + 3, cor, 2)

#                 # Mostra a porcentagem dentro do círculo
#                 texto = f"{pct:.0%}"
#                 cv2.putText(vis, texto, (cx - 12, cy + 4),
#                             cv2.FONT_HERSHEY_SIMPLEX, 0.3, cor, 1)

#     # Gera HTML
#     _, buffer = cv2.imencode(".png", vis)
#     b64 = base64.b64encode(buffer).decode("utf-8")

#     html = f"""<!DOCTYPE html>
#     <html>
#     <head>
#         <style>
#             body {{ font-family: sans-serif; padding: 2rem; background: #f5f5f5; }}
#             .legenda {{ display: flex; gap: 2rem; margin-bottom: 1rem; font-size: 14px; }}
#             .legenda span {{ display: flex; align-items: center; gap: 6px; }}
#             .dot {{ width: 14px; height: 14px; border-radius: 50%; display: inline-block; }}
#         </style>
#     </head>
#     <body>
#         <h2 style="font-family: monospace;">Leitura das bolhas</h2>
#         <div class="legenda">
#             <span><span class="dot" style="background: #00c800;"></span> Marcado</span>
#             <span><span class="dot" style="background: #b4b4b4;"></span> Vazio</span>
#             <span><span class="dot" style="background: #00c8ff;"></span> Ambíguo</span>
#         </div>
#         <img src="data:image/png;base64,{b64}" style="max-width: 100%; border: 1px solid #ccc;">
#     </body>
#     </html>"""

#     with tempfile.NamedTemporaryFile(suffix=".html", delete=False, mode="w") as f:
#         f.write(html)
#         webbrowser.open(f"file://{f.name}")


# --- EXECUÇÃO STANDALONE ---

if __name__ == "__main__":
    from localizar_marcadores import carregar_imagem, processar

    if len(sys.argv) < 3:
        print("Uso: python ler_bolhas.py <scan.pdf ou scan.png> <config.yaml> [pagina_pdf] [pagina_lista]")
        print()
        print("  pagina_pdf:   página do PDF (0 = primeira, default: 0)")
        print("  pagina_lista: página da lista (1 = primeira, default: 1)")
        print()
        print("Exemplos:")
        print("  python ler_bolhas.py scan.pdf config_canela.yaml")
        print("  python ler_bolhas.py scan.pdf config_canela.yaml 1      # pula verso branco do duplex")
        print("  python ler_bolhas.py scan.pdf config_canela.yaml 1 1    # página 1 da lista")
        sys.exit(1)

    caminho_scan = sys.argv[1]
    caminho_config = sys.argv[2]
    pagina_pdf = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    pagina_lista = int(sys.argv[4]) if len(sys.argv) > 4 else 1

    # Carrega config
    with open(caminho_config, "r") as f:
        config = yaml.safe_load(f)

    # Carrega e alinha a imagem
    img = carregar_imagem(caminho_scan, pagina_pdf)
    alinhada = processar(img, config)

    # Binariza a imagem alinhada
    gray = cv2.cvtColor(alinhada, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    _, binary = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

    # Quantos alunos nessa página
    alunos_por_pagina = config["layout"]["alunos_por_pagina"]

    # Lê as bolhas
    resultados = ler_pagina(binary, config, alunos_por_pagina)

    # Imprime resultados
    dias = config["layout"]["dias"]
    print(f"\n=== Página {pagina_lista} da lista ===\n")
    imprimir_resultados(resultados, dias)

    # Debug visual
    # debug_visual(alinhada, binary, config, resultados)
    # print("\nDebug visual aberto no navegador.")
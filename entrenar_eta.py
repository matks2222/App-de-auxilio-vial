"""Entrena el modelo que predice el tiempo de llegada del repartidor y lo compara contra la fórmula actual.

Uso (desde la carpeta del proyecto):
    python entrenar_eta.py                       # con datos SIMULADOS (20 000 viajes)
    python entrenar_eta.py --desde-db despacho.db # con los viajes REALES guardados por la app (tabla «viajes»)
    python entrenar_eta.py --desde-db despacho.db --con-simulados   # incluye también los de los repartidores de prueba

Genera en app/modelo/: eta_modelo.joblib (el modelo), eta_modelo.json (métricas y datos de origen) y eta_comparacion.png (gráfica).

Los datos simulados salen de un «mundo» inventado con reglas razonables (hora pico, fin de semana, tipo de vehículo, rodeo de las calles,
ruido). NO son mediciones reales: sirven para probar todo el proceso. Cuando haya viajes reales, reentrena con --desde-db.
"""
import argparse
import json
import math
import random
import sqlite3
import sys
from pathlib import Path

import numpy as np
import sklearn
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.model_selection import train_test_split

from app import ia_eta, motor

SALIDA = ia_eta.DIR_MODELO
VEL_FORMULA_KMH = 18.0                                          # la velocidad que usa la app hoy


def generar_simulado(n=20000, semilla=7):
    """Viajes inventados: (distancia_m, hora, día, vehículo, duración_s)."""
    rng = random.Random(semilla)
    vel = {"moto": 30.0, "auto": 24.0, "bicicleta": 14.0, "a_pie": 5.0}              # km/h con la calle libre
    filas = []
    for _ in range(n):
        veh = rng.choices(ia_eta.VEHICULOS, weights=[55, 30, 10, 5])[0]
        dist = min(rng.lognormvariate(math.log(1500), 0.7), 12000.0)                  # la mayoría de viajes: 0.5 a 5 km
        dist = max(dist, 150.0)
        hora, dia = rng.uniform(0, 24), rng.randrange(7)
        laboral = dia < 5
        pico = laboral and (7 <= hora < 9.5 or 17.5 <= hora < 20)
        factor = 0.55 if pico else 0.8 if (laboral and 9.5 <= hora < 17.5) else 0.9 if hora < 23 and hora >= 6 else 1.1
        if veh in ("bicicleta", "a_pie"):
            factor = 1.0 - (1.0 - factor) * 0.25                                      # el tráfico casi no les afecta
        if veh == "moto":
            factor = 1.0 - (1.0 - factor) * 0.6                                       # la moto se cuela entre autos
        rodeo = 1.2 + 0.5 * math.exp(-dist / 2000.0) + rng.uniform(0, 0.15)           # trayectos cortos dan más vueltas
        arranque = rng.uniform(20, 90)                                                # tarda en salir
        ruido = rng.lognormvariate(0, 0.15)
        dur = (arranque + dist * rodeo / (vel[veh] * factor / 3.6)) * ruido
        filas.append((dist, hora, dia, veh, dur))
    return filas


def desde_db(ruta, con_simulados=False):
    con = sqlite3.connect(ruta)
    q = "SELECT dist_m, t, vehiculo, duracion_s FROM viajes WHERE duracion_s > 10 AND dist_m > 0" + ("" if con_simulados else " AND simulado = 0")
    filas = []
    for dist, t, veh, dur in con.execute(q):
        hora, dia = ia_eta.hora_y_dia(t)
        filas.append((dist, hora, dia, veh, dur))
    return filas


def matriz(filas):
    X = np.array([ia_eta.caracteristicas(d, h, dia, v) for d, h, dia, v, _ in filas])
    y = np.array([f[4] for f in filas])
    return X, y


def metricas(y, pred):
    err = np.abs(pred - y)
    return dict(mae_s=float(err.mean()), mediana_s=float(np.median(err)), mape=float(np.mean(err / y)), p90_s=float(np.percentile(err, 90)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--desde-db")
    ap.add_argument("--con-simulados", action="store_true")
    ap.add_argument("--n", type=int, default=20000)
    a = ap.parse_args()

    if a.desde_db:
        filas, origen = desde_db(a.desde_db, a.con_simulados), "viajes reales" + (" y de prueba" if a.con_simulados else "") + f" de {a.desde_db}"
    else:
        filas, origen = generar_simulado(a.n), f"datos SIMULADOS ({a.n} viajes)"
    if len(filas) < 200:
        sys.exit(f"Hay solo {len(filas)} viajes: se necesitan al menos 200 para entrenar algo útil. Sigue operando y vuelve a intentar.")

    X, y = matriz(filas)
    Xe, Xp, ye, yp = train_test_split(X, y, test_size=0.2, random_state=1)
    modelo = HistGradientBoostingRegressor(max_iter=300, learning_rate=0.08, max_leaf_nodes=24, random_state=1)
    modelo.fit(Xe, np.log1p(ye))                                       # se aprende en escala logarítmica: los errores son proporcionales
    pred = np.expm1(modelo.predict(Xp))
    base = np.array([motor.eta_desde_distancia(d, VEL_FORMULA_KMH) for d in Xp[:, 0]])

    m_modelo, m_formula = metricas(yp, pred), metricas(yp, base)
    por_veh = {}
    for i, v in enumerate(ia_eta.VEHICULOS):
        k = Xp[:, 3] == i
        if k.sum() >= 20:
            por_veh[v] = dict(modelo_mae_s=float(np.abs(pred[k] - yp[k]).mean()), formula_mae_s=float(np.abs(base[k] - yp[k]).mean()), n=int(k.sum()))

    SALIDA.mkdir(parents=True, exist_ok=True)
    import joblib
    joblib.dump(modelo, SALIDA / "eta_modelo.joblib")
    info = dict(origen_datos=origen, viajes=len(filas), prueba=len(yp), sklearn=sklearn.__version__, modelo="HistGradientBoostingRegressor",
                caracteristicas=["distancia_m", "hora", "dia_semana", "vehiculo"], metricas_modelo=m_modelo, metricas_formula=m_formula, por_vehiculo=por_veh,
                mejora_mae=1 - m_modelo["mae_s"] / m_formula["mae_s"], simulado=not a.desde_db)
    (SALIDA / "eta_modelo.json").write_text(json.dumps(info, indent=2, ensure_ascii=False), encoding="utf-8")

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(1, 2, figsize=(11, 4.2))
    cuadro = dict(color="#7B1E3A")
    ax[0].bar(["Fórmula\nactual", "Modelo\n(IA)"], [m_formula["mae_s"] / 60, m_modelo["mae_s"] / 60], color=["#999999", "#7B1E3A"])
    ax[0].set_ylabel("Error promedio (minutos)")
    ax[0].set_title("¿Cuánto se equivoca cada uno?")
    for i, v in enumerate([m_formula["mae_s"] / 60, m_modelo["mae_s"] / 60]):
        ax[0].text(i, v, f"{v:.1f} min", ha="center", va="bottom")
    lim = float(np.percentile(yp, 98)) / 60
    ax[1].scatter(yp / 60, base / 60, s=3, alpha=.25, color="#999999", label="Fórmula")
    ax[1].scatter(yp / 60, pred / 60, s=3, alpha=.25, color="#7B1E3A", label="Modelo")
    ax[1].plot([0, lim], [0, lim], "k--", lw=1)
    ax[1].set_xlim(0, lim); ax[1].set_ylim(0, lim)
    ax[1].set_xlabel("Tiempo real (min)"); ax[1].set_ylabel("Tiempo estimado (min)"); ax[1].set_title("Estimado vs real (más cerca de la línea = mejor)")
    ax[1].legend(markerscale=4)
    fig.suptitle(("DATOS SIMULADOS · " if not a.desde_db else "") + f"{len(filas)} viajes, {len(yp)} de prueba", fontsize=9)
    fig.tight_layout()
    fig.savefig(SALIDA / "eta_comparacion.png", dpi=140)

    print(f"Origen de los datos: {origen}")
    print(f"Error promedio  fórmula: {m_formula['mae_s']/60:.2f} min   modelo: {m_modelo['mae_s']/60:.2f} min   (mejora {info['mejora_mae']*100:.0f}%)")
    print(f"Error mediano   fórmula: {m_formula['mediana_s']/60:.2f} min   modelo: {m_modelo['mediana_s']/60:.2f} min")
    print(f"Guardado en {SALIDA}")


if __name__ == "__main__":
    main()

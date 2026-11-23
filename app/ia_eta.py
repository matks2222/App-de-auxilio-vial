"""Modelo de aprendizaje automático que predice cuánto tarda un repartidor en llegar.

Entradas (4 datos): distancia en línea recta, hora del día, día de la semana y tipo de vehículo.
Salida: segundos que tardará en llegar.
Modelo: gradient boosting (árboles de decisión que se corrigen unos a otros), de scikit-learn.

Se usa solo si existe el archivo del modelo y es compatible; si no, la app sigue con la fórmula. Nunca rompe nada.
El modelo se entrena con `python entrenar_eta.py` (datos simulados por omisión, o los viajes reales con --desde-db).
"""
import json
import logging
import os
from pathlib import Path

VEHICULOS = ["moto", "auto", "bicicleta", "a_pie"]
DIR_MODELO = Path(__file__).parent / "modelo"
ZONA_HORARIA_H = -6                                              # Ciudad de México


def hora_y_dia(t):
    """Hora del día (con decimales) y día de la semana (0 = lunes) para una marca de tiempo, en hora de México."""
    local = t + ZONA_HORARIA_H * 3600
    dias = int(local // 86400)
    return (local % 86400) / 3600.0, (dias + 3) % 7              # el 1 de enero de 1970 fue jueves


def caracteristicas(dist_m, hora, dia, vehiculo):
    v = VEHICULOS.index(vehiculo) if vehiculo in VEHICULOS else 0
    return [float(dist_m), float(hora), float(dia), float(v)]


class ModeloETA:
    def __init__(self):
        self.modelo = None
        self.info = {}
        self.intentado = False

    def ruta(self):
        return Path(os.environ.get("DESPACHO_MODELO") or DIR_MODELO / "eta_modelo.joblib")

    def cargar(self):
        """Carga el modelo una sola vez. Si falta o es de otra versión de scikit-learn, queda desactivado sin error."""
        if self.intentado:
            return self.modelo is not None
        self.intentado = True
        log = logging.getLogger("uvicorn.error")
        try:
            ruta = self.ruta()
            if not ruta.exists():
                return False
            meta = ruta.with_suffix(".json")
            self.info = json.loads(meta.read_text(encoding="utf-8")) if meta.exists() else {}
            import joblib
            import sklearn
            if self.info.get("sklearn") and self.info["sklearn"].split(".")[:2] != sklearn.__version__.split(".")[:2]:
                log.warning("Modelo de tiempos de llegada hecho con scikit-learn %s y aquí hay %s: no se usa (corre entrenar_eta.py).",
                            self.info["sklearn"], sklearn.__version__)
                return False
            self.modelo = joblib.load(ruta)
            log.info("Modelo de tiempos de llegada cargado (%s).", self.info.get("origen_datos", "sin datos de origen"))
        except Exception as e:
            log.warning("No se pudo cargar el modelo de tiempos de llegada: %r", e)
            self.modelo = None
        return self.modelo is not None

    @property
    def activo(self):
        return self.cargar()

    def predecir(self, dist_m, t, vehiculo):
        """Segundos estimados, o None si no hay modelo."""
        if not self.cargar():
            return None
        try:
            hora, dia = hora_y_dia(t)
            seg = float(self.modelo.predict([caracteristicas(dist_m, hora, dia, vehiculo)])[0])
            import math
            return max(20.0, math.expm1(seg))                    # el modelo predice en escala logarítmica
        except Exception:
            return None


modelo_eta = ModeloETA()

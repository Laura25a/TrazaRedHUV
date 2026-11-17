"""Comprobación reproducible con las dependencias de ml/requirements.txt.

Ejecutar en la raíz con PYTHONPATH=ml:python python -m unittest discover -s pruebas -p test_ml.py.
"""
import hashlib
import unittest

class EvaluacionML(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global entrenar, rem, np, roc_auc_score, train_test_split
        try:
            import numpy as np
            from sklearn.metrics import roc_auc_score
            from sklearn.model_selection import train_test_split
            import entrenar
            import remisiones_modelo as rem
        except ModuleNotFoundError as exc:
            raise unittest.SkipTest(f"Se requiere el entorno ML (revisar ml/requirements.txt y PYTHONPATH): {exc.name}")
        cls.ktas = entrenar.entrenar("prueba", guardar=False)
        cls.remisiones = rem.entrenar_remisiones(guardar=False)

    def test_ktas_repite_particion_y_metricas_en_la_prueba_reservada(self):
        df = entrenar.cargar_ktas()
        datos = df[entrenar.COLS_NUM + entrenar.COLS_CAT]
        desarrollo, prueba = train_test_split(df, test_size=0.2, random_state=entrenar.SEMILLA,
                                               stratify=df["urgente"])
        paquete = self.ktas
        manifiesto = paquete["evaluacion"]["prueba"]
        self.assertEqual(manifiesto["filas"], sorted(prueba.index.tolist()))
        self.assertFalse(set(desarrollo.index) & set(manifiesto["filas"]))
        self.assertEqual(manifiesto["dataset_sha256"],
                         hashlib.sha256(entrenar.RUTA_CSV.read_bytes()).hexdigest())
        probabilidad = paquete["modelo"].predict_proba(datos.loc[manifiesto["filas"]])[:, 1]
        auc = roc_auc_score(df.loc[manifiesto["filas"], "urgente"], probabilidad)
        self.assertEqual(round(auc, 3), paquete["metricas"]["auc_prueba"])
        self.assertEqual(sum(paquete["metricas"]["matriz_confusion"].values()), len(prueba))

    def test_busqueda_y_comparacion_por_subgrupo(self):
        e = self.ktas["evaluacion"]
        self.assertEqual(len(e["candidatos"]), 24)
        self.assertEqual(set(e["subgrupos"]), {"mediana", "faltantes_nativos"})
        for resultados in e["subgrupos"].values():
            presentes = resultados["SpO₂ registrada"]["n"]
            ausentes = resultados["SpO₂ ausente"]["n"]
            self.assertEqual(presentes + ausentes, e["prueba"]["n_desarrollo"])
        elegido = max(c["auc_validacion"] for c in e["candidatos"]
                     if c["estrategia"] == e["imputacion_elegida"])
        self.assertEqual(elegido, max(c["auc_validacion"] for c in e["candidatos"]))

    def test_pronostico_temporal_sin_cruzar_pacientes_ni_cierres(self):
        datos, contactos = rem.cargar_datos()
        desarrollo, prueba, corte = rem.particion_temporal(datos, contactos)
        self.assertFalse(set(desarrollo.paciente_ref) & set(prueba.paciente_ref))
        self.assertLess(desarrollo.fecha_solicitud.max(), corte.date().isoformat())
        self.assertGreaterEqual(prueba.fecha_solicitud.min(), corte.date().isoformat())
        self.assertLess(contactos[contactos.remision_ref.isin(desarrollo.remision_ref)].fecha.max(), corte)
        e = self.remisiones["evaluacion_temporal"]
        self.assertEqual(e["radicacion"]["n"], len(prueba))
        fotos = rem.fotos_de_gestion(prueba, contactos)
        self.assertEqual(e["gestion"]["n"], fotos.remision_ref.nunique())
        base = set(rem.COLS_NUM + rem.COLS_CAT + rem.COLS_BIN)
        for etapa, columnas in (("radicacion", base), ("gestion", base | set(rem.COLS_GESTION))):
            resultados = e["permutacion"][etapa]
            self.assertEqual({f["variable"] for f in resultados}, columnas)
            self.assertTrue(all(np.isfinite(f["caida_auc"]) for f in resultados))
            self.assertTrue(all(f["desviacion"] >= 0 for f in resultados))


if __name__ == "__main__":
    unittest.main()

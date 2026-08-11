import asyncio
import unittest
from app.core.mapping import SfcSalesforceMapper


class TestMappingRaceCondition(unittest.IsolatedAsyncioTestCase):

    async def test_race_condition_prevented_during_inverse_catalogs_rebuild(self):
        """
        Verifica que las consultas de traducción concurrentes durante la reconstrucción
        de catálogos inversos nunca lean diccionarios vacíos o incompletos.
        """
        # 1. Cargar catálogos locales iniciales
        SfcSalesforceMapper.cargar_catalogos_local(force=True)
        self.assertIn("tipo_id", SfcSalesforceMapper.INVERSE_CATALOGS)

        lecturas_incompletas_o_vacias = 0
        lecturas_totales = 0

        # 2. Corrutina lectora concurrente
        async def _lector_concurrente():
            nonlocal lecturas_incompletas_o_vacias, lecturas_totales
            for _ in range(200):
                lecturas_totales += 1
                inv = SfcSalesforceMapper.INVERSE_CATALOGS
                
                # Evaluar si en algún instante el diccionario estuvo vacío o faltaron claves críticas
                if not inv or "tipo_id" not in inv or "canal" not in inv:
                    lecturas_incompletas_o_vacias += 1
                elif inv["tipo_id"].get("cc") != 1:
                    lecturas_incompletas_o_vacias += 1
                    
                await asyncio.sleep(0.001)

        # 3. Disparar lector concurrente y forzar reconstrucciones simultáneas
        tarea_lectora = asyncio.create_task(_lector_concurrente())

        for _ in range(20):
            SfcSalesforceMapper._construir_indices_inversos()
            await asyncio.sleep(0.005)

        await tarea_lectora

        # 4. Aserción: Ninguna lectura debió detectar diccionarios vacíos o parciales
        self.assertEqual(
            lecturas_incompletas_o_vacias, 0,
            f"Se detectaron {lecturas_incompletas_o_vacias} lecturas de INVERSE_CATALOGS "
            f"incompleto o vacío de un total de {lecturas_totales} lecturas."
        )


if __name__ == "__main__":
    unittest.main()
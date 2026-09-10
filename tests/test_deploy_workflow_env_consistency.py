# tests/test_deploy_workflow_env_consistency.py
"""
🟢 FIX (auditoría nombres de recursos AWS): ya pasó una vez (commit 1fdbbaf) que
render_task_def.py exigía una variable de entorno pero el job `deploy` del workflow
no la declaraba en su `env:` -- el render caía en silencio a su valor por defecto
(coincidía con el real, así que no se notó hasta la auditoría). Este test lee el
workflow como texto plano (sin depender de PyYAML, que no está en requirements.txt
-- sólo llega a este venv como dependencia transitiva de bandit, así que un test
que lo importara pasaría en local y fallaría en un runner de CI limpio) y falla si
alguna variable que render_task_def.py exige sin fallback no está declarada en el
`env:` del job `deploy` como Variable de GitHub.
"""
import os
import re
import unittest

try:
    from scripts.render_task_def import CAMPOS_CRITICOS_INFRA
except ImportError:
    from render_task_def import CAMPOS_CRITICOS_INFRA

WORKFLOW_PATH = os.path.join(
    os.path.dirname(__file__), "..", ".github", "workflows", "deploy-aws.yml"
)

# AWS_ACCOUNT_ID y SMTP_FROM_EMAIL son igual de obligatorias (fail-fast propio, sin
# default) pero se validan aparte de CAMPOS_CRITICOS_INFRA por tener un mensaje de
# error distinto -- se suman aquí para que el chequeo cubra toda variable sin
# default que dependa de una Variable de GitHub. IMAGE_TAG queda fuera a propósito:
# no es una Variable de GitHub, la produce el job build-and-push como output.
OTRAS_VARS_SIN_DEFAULT = ["AWS_ACCOUNT_ID", "SMTP_FROM_EMAIL"]


class TestDeployWorkflowEnvConsistency(unittest.TestCase):

    def setUp(self):
        with open(WORKFLOW_PATH, "r", encoding="utf-8") as f:
            self.workflow_text = f.read()

    def _bloque_env_del_job_deploy(self) -> str:
        """Extrae únicamente el `env:` del job `deploy`, no el de `test`/`build-and-push`."""
        inicio = self.workflow_text.index("\n  deploy:\n")
        bloque = self.workflow_text[inicio:]
        inicio_env = bloque.index("\n    env:\n")
        # El bloque `env:` termina en la primera línea siguiente con la misma
        # indentación de 4 espacios que no sea parte del propio env (ej. "    steps:").
        resto = bloque[inicio_env + len("\n    env:\n"):]
        fin = resto.index("\n    steps:")
        return resto[:fin]

    def test_toda_variable_critica_de_render_task_def_esta_en_el_workflow(self):
        """
        Cada nombre en CAMPOS_CRITICOS_INFRA debe aparecer en el `env:` del job
        `deploy` como `NOMBRE: ${{ vars.NOMBRE }}` -- si no, un ambiente sin esa
        Variable configurada fallaría recién al correr el render, en vez de que
        esta suite lo detecte en cada push/PR.
        """
        bloque_env = self._bloque_env_del_job_deploy()

        for var in CAMPOS_CRITICOS_INFRA + OTRAS_VARS_SIN_DEFAULT:
            with self.subTest(var=var):
                patron = re.compile(
                    rf"^\s*{re.escape(var)}:\s*\$\{{\{{\s*vars\.{re.escape(var)}\s*\}}\}}",
                    re.MULTILINE,
                )
                self.assertRegex(
                    bloque_env,
                    patron,
                    f"'{var}' es obligatoria en render_task_def.py pero no está declarada "
                    f"en el env: del job 'deploy' del workflow.",
                )

    def test_no_quedan_convenciones_de_nombre_hardcodeadas_en_el_workflow(self):
        """
        Regresión específica: los nombres que ya se sabe que eran convenciones
        hardcodeadas (Task Role, repo ECR, secreto compuesto) no deben reaparecer
        como literales en líneas ejecutables del workflow (se ignoran los
        comentarios, que sí los mencionan a propósito como contexto histórico).
        """
        lineas_no_comentario = "\n".join(
            linea for linea in self.workflow_text.splitlines()
            if linea.strip() and not linea.strip().startswith("#")
        )
        literales_prohibidos = [
            "msSmartsupervisionTaskRole-",
            "ms-integracion-smartsupervision",
            "smartsupervision/app-secrets",
        ]
        for literal in literales_prohibidos:
            with self.subTest(literal=literal):
                self.assertNotIn(
                    literal,
                    lineas_no_comentario,
                    f"'{literal}' reapareció hardcodeado en el workflow -- debe venir de una Variable.",
                )


if __name__ == "__main__":
    unittest.main()

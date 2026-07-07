"""
Test rápido con datos falsos para validar la lógica de atribución por ejecutivo
(Tickets/Usuarios -> Empresas -> Leyenda) y el reparto de fallback (FALLBACK_SPLIT)
para lo que no logra matchear, sin necesitar credenciales de Google.

Correr con: python3 scripts/test_update_data.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from update_data import build_dashboard_data, build_header_index  # noqa: E402


def records(headers, rows):
    h_idx = build_header_index(headers)
    recs = [dict(zip(headers, row)) for row in rows]
    return recs, h_idx


def main():
    # --- Leyenda: define headcount y rol/pais por email
    leyenda_h = ["Correo", "KAM ID", "Rol", "Pais"]
    leyenda_rows = [
        ["ana@apprecio.com", "AN", "KAM", "Chile"],
        ["beto@apprecio.com", "BE", "BDM", "Chile"],
        ["cami@apprecio.com", "CA", "Full Cycle", "Colombia"],
        ["sinid@apprecio.com", "", "KAM", "Colombia"],
        ["diego@apprecio.com", "DI", "Full Cycle", "México"],
        ["fer@apprecio.com", "FE", "KAM", "Perú"],
    ]
    leyenda_records, leyenda_hidx = records(leyenda_h, leyenda_rows)
    # Headcount esperado: KAM/Chile=1, BDM/Chile=1, Full Cycle/Colombia=1, KAM/Colombia=1,
    # Full Cycle/México=1, KAM/Perú=1. Todo lo demás (BDM/Colombia, KAM/México, Ecuador, etc) = 0.

    # --- Empresas (mapa ID Empresa -> dueño). Empresa "9" sin dueño reconocible en Leyenda.
    # Empresas 20, 30, 31, 40 y 99 NO están en el mapa a propósito (simulan cuentas nuevas
    # que todavía no se agregaron a la hoja "Empresas"), para probar el reparto fallback.
    # Clave (país, ID Empresa): el ID es correlativo POR país en la hoja Empresas, no global.
    empresa_owner_map = {
        ("Chile", "1"): {"email": "ana@apprecio.com", "pais": "Chile"},
        ("Chile", "2"): {"email": "ana@apprecio.com", "pais": "Chile"},
        ("Chile", "3"): {"email": "beto@apprecio.com", "pais": "Chile"},
        ("Colombia", "4"): {"email": "sinid@apprecio.com", "pais": "Colombia"},
        ("Chile", "9"): {"email": "owner_x@apprecio.com", "pais": "Chile"},  # no está en Leyenda
    }

    # --- Tickets ---
    # 1,2,4 -> empresa 1 (Ana/KAM/Chile). 3 -> empresa 2 (Ana/KAM/Chile). 5 -> empresa 3 (Beto/BDM/Chile).
    # 6 -> empresa 9 (dueño no reconocido, Chile) -> fallback: Chile reparte 100% a KAM.
    # 7 -> empresa 99 (no existe en Empresas, Chile) -> fallback: KAM también.
    # 8 -> empresa 20 (no existe, Colombia) -> fallback determinístico: cae en KAM.
    # 9 -> empresa 44 (no existe, Colombia) -> fallback determinístico: cae en BDM (el 10%).
    # 10 -> empresa 30 (no existe, México) -> fallback: México reparte 100% a Full Cycle.
    # 11 -> empresa 31 (no existe, Perú) -> fallback: Perú reparte 100% a KAM.
    # 12 -> empresa 40 (no existe, Ecuador) -> Ecuador no tiene regla de fallback -> excluido.
    tickets_h = ["id", "empresa", "id_tributario", "fechaCreacion", "pais"]
    tickets_rows = [
        ["1", "1", "RUT-A", "2025-07-02 9:00:00", "Chile"],
        ["2", "1", "RUT-A", "2025-07-05 9:00:00", "Chile"],
        ["3", "2", "RUT-B", "2025-07-10 9:00:00", "Chile"],
        ["4", "1", "RUT-A", "2025-08-01 9:00:00", "Chile"],
        ["5", "3", "RUT-C", "2025-07-15 9:00:00", "Chile"],
        ["6", "9", "RUT-D", "2025-07-15 9:00:00", "Chile"],
        ["7", "99", "RUT-E", "2025-07-15 9:00:00", "Chile"],
        ["8", "20", "RUT-F", "2025-07-20 9:00:00", "Colombia"],
        ["9", "44", "RUT-G", "2025-07-20 9:00:00", "Colombia"],
        ["10", "30", "RUT-H", "2025-07-25 9:00:00", "México"],
        ["11", "31", "RUT-I", "2025-07-28 9:00:00", "Perú"],
        ["12", "40", "RUT-J", "2025-07-29 9:00:00", "Ecuador"],
    ]
    tickets_records, tickets_hidx = records(tickets_h, tickets_rows)
    # KAM/Chile: emp1 (x2 jul, x1 ago) + emp2 (x1 jul) + fallback emp9 (jul) + fallback emp99 (jul)
    #   jul = 3 + 2 = 5, ago = 1 -> mensual = (5+1)/2 = 3.0 ; clientes {A,B,D,E} = 4
    # BDM/Chile: emp3 (1 jul) -> mensual 1.0, clientes {C} = 1
    # KAM/Colombia: fallback emp20 (1 jul) -> mensual 1.0, clientes {F} = 1
    # BDM/Colombia: fallback emp44 (1 jul) -> mensual 1.0, clientes {G} = 1 (pero 0 ejecutivos BDM ahí)
    # Full Cycle/México: fallback emp30 (1 jul) -> mensual 1.0, clientes {H} = 1
    # KAM/Perú: fallback emp31 (1 jul) -> mensual 1.0, clientes {I} = 1
    # Ecuador emp40: sin regla de fallback -> excluido, no aporta a ningún país/rol

    # --- Usuarios: columna L por ID Empresa ---
    # emp9 (Chile, fallback KAM), emp44 (Colombia, fallback BDM), emp30 (México, fallback Full Cycle),
    # emp40 (Ecuador, sin regla -> excluido), emp4 (columna L vacía -> excluido de plano)
    usuarios_h = ["ID Empresa", "N° usuarios incentivados", "País"]
    usuarios_rows = [
        ["1", "100", "Chile"],    # Ana/KAM/Chile
        ["2", "50", "Chile"],     # Ana/KAM/Chile
        ["3", "30", "Chile"],     # Beto/BDM/Chile
        ["9", "20", "Chile"],     # fallback -> KAM/Chile
        ["4", "", "Colombia"],    # columna L vacía -> excluido
        ["44", "40", "Colombia"], # fallback -> BDM/Colombia
        ["30", "15", "México"],   # fallback -> Full Cycle/México
        ["40", "5", "Ecuador"],   # sin regla de fallback -> excluido
    ]
    usuarios_records, usuarios_hidx = records(usuarios_h, usuarios_rows)
    # KAM/Chile usuarios_total = 100+50+20 = 170 ; BDM/Chile = 30
    # BDM/Colombia = 40 ; Full Cycle/México = 15

    # --- Reuniones (igual que antes, vía KAM ID / email) ---
    reuniones_h = ["ID_REUNION", "FECHA_ISO", "ANIO", "MES", "SELLER_EMAIL", "KAM ID"]
    reuniones_rows = [
        ["r1", "2026-01-10", "2026", "1", "ana@apprecio.com", "AN"],
        ["r2", "2026-01-15", "2026", "1", "ana@apprecio.com", "AN"],
        ["r3", "2026-02-01", "2026", "2", "beto@apprecio.com", "BE"],
        ["r4", "2026-01-20", "2026", "1", "sinid@apprecio.com", "#N/A"],
        ["r5", "2026-01-22", "2026", "1", "desconocido@apprecio.com", "#N/A"],
        ["r6", "2026-01-25", "2026", "1", "otro@apprecio.com, beto@apprecio.com", "#N/A"],
    ]
    reuniones_records, reuniones_hidx = records(reuniones_h, reuniones_rows)

    output, unmatched_report = build_dashboard_data(
        tickets_records, tickets_hidx,
        usuarios_records, usuarios_hidx,
        reuniones_records, reuniones_hidx,
        leyenda_records, leyenda_hidx,
        empresa_owner_map,
    )

    errors = []

    def check(label, actual, expected):
        if actual != expected:
            errors.append(f"{label}: esperado {expected}, obtuve {actual}")

    kam_chile = output["tabs"]["KAM"]["countries"]["Chile"]
    bdm_chile = output["tabs"]["BDM"]["countries"]["Chile"]
    kam_col = output["tabs"]["KAM"]["countries"]["Colombia"]
    bdm_col = output["tabs"]["BDM"]["countries"]["Colombia"]
    fc_col = output["tabs"]["Full Cycle"]["countries"]["Colombia"]
    fc_mex = output["tabs"]["Full Cycle"]["countries"]["México"]
    kam_peru = output["tabs"]["KAM"]["countries"]["Perú"]
    ecuador_kam = output["tabs"]["KAM"]["countries"]["Ecuador"]

    check("Headcount KAM/Chile", kam_chile["ejecutivos"], 1)
    check("Headcount BDM/Chile", bdm_chile["ejecutivos"], 1)
    check("Headcount KAM/Colombia", kam_col["ejecutivos"], 1)
    check("Headcount BDM/Colombia (nadie)", bdm_col["ejecutivos"], 0)
    check("Headcount Full Cycle/Colombia", fc_col["ejecutivos"], 1)
    check("Headcount Full Cycle/México", fc_mex["ejecutivos"], 1)
    check("Headcount KAM/Perú", kam_peru["ejecutivos"], 1)
    check("Headcount KAM/Ecuador (nadie en Leyenda)", ecuador_kam["ejecutivos"], 0)

    # --- Reparto normal (empresas SÍ mapeadas) ---
    check("BDM/Chile total_clientes (RUT-C)", bdm_chile["total_clientes"], 1)
    check("BDM/Chile tickets_mensual", bdm_chile["tickets_mensual"], 1.0)

    # --- Fallback: Chile reparte 100% a KAM (empresas 9 y 99 se suman a KAM/Chile) ---
    check("KAM/Chile total_clientes (A,B + fallback D,E)", kam_chile["total_clientes"], 4)
    check("KAM/Chile tickets_mensual (con fallback)", kam_chile["tickets_mensual"], 3.0)
    check("KAM/Chile tickets_exec (1 exec)", kam_chile["tickets_exec"], 3.0)
    check("KAM/Chile usuarios_total (100+50+20 fallback)", kam_chile["usuarios_total"], 170.0)
    check("BDM/Chile usuarios_total", bdm_chile["usuarios_total"], 30.0)

    # --- Fallback: Colombia reparte 90% KAM / 10% BDM (reparto determinístico por ID empresa) ---
    check("KAM/Colombia tickets_mensual (fallback emp20)", kam_col["tickets_mensual"], 1.0)
    check("KAM/Colombia total_clientes (fallback emp20)", kam_col["total_clientes"], 1)
    check("BDM/Colombia tickets_mensual (fallback emp44)", bdm_col["tickets_mensual"], 1.0)
    check("BDM/Colombia total_clientes (fallback emp44)", bdm_col["total_clientes"], 1)
    check("BDM/Colombia usuarios_total (fallback emp44)", bdm_col["usuarios_total"], 40.0)
    check("BDM/Colombia tickets_exec (0 ejecutivos BDM ahí)", bdm_col["tickets_exec"], 0)

    # --- Fallback: México reparte 100% a Full Cycle ---
    check("Full Cycle/México tickets_mensual (fallback emp30)", fc_mex["tickets_mensual"], 1.0)
    check("Full Cycle/México tickets_exec", fc_mex["tickets_exec"], 1.0)
    check("Full Cycle/México usuarios_total (fallback emp30)", fc_mex["usuarios_total"], 15.0)

    # --- Fallback: Perú reparte 100% a KAM ---
    check("KAM/Perú tickets_mensual (fallback emp31)", kam_peru["tickets_mensual"], 1.0)
    check("KAM/Perú tickets_exec", kam_peru["tickets_exec"], 1.0)

    # --- Ecuador no tiene regla de fallback -> sigue excluyendo (emp40) ---
    check("Ecuador no recibe nada por fallback (sin regla)", ecuador_kam["tickets_mensual"], 0)

    # --- Detalle por ejecutivo (nombre). Ya NO hay "pool_sin_asignar" aparte: lo
    # redistribuido se reparte en partes iguales y se suma directo a cada ejecutivo,
    # con el mismo denominador de meses que el país (para que sumen exacto al total). ---
    kam_chile_execs = {e["correo"]: e for e in kam_chile["ejecutivos_detalle"]}
    check("KAM/Chile tiene 1 ejecutivo en el detalle", len(kam_chile["ejecutivos_detalle"]), 1)
    check("KAM/Chile detalle: nombre de Ana", kam_chile_execs.get("ana@apprecio.com", {}).get("nombre"), "Ana")
    # Ana es la única KAM en Chile -> se lleva el 100% de lo redistribuido (emp9+emp99) sumado a lo propio
    check("KAM/Chile detalle: tickets de Ana (propios + 100% del pool)", kam_chile_execs.get("ana@apprecio.com", {}).get("tickets_mensual"), 3.0)
    check("KAM/Chile detalle: clientes de Ana (propios + pool)", kam_chile_execs.get("ana@apprecio.com", {}).get("total_clientes"), 4.0)
    check("KAM/Chile detalle: usuarios de Ana (propios + pool)", kam_chile_execs.get("ana@apprecio.com", {}).get("usuarios_total"), 170.0)
    check("KAM/Chile detalle: reuniones de Ana (no hay pool de reuniones)", kam_chile_execs.get("ana@apprecio.com", {}).get("reuniones_mensual"), 2.0)
    # Con 1 solo ejecutivo, su total coincide exacto con el total del país (no hay a quién más repartir)
    check("KAM/Chile: suma de ejecutivos == total del país (tickets)", sum(e["tickets_mensual"] for e in kam_chile["ejecutivos_detalle"]), kam_chile["tickets_mensual"])
    check("KAM/Chile: suma de ejecutivos == total del país (usuarios)", sum(e["usuarios_total"] for e in kam_chile["ejecutivos_detalle"]), kam_chile["usuarios_total"])

    bdm_chile_execs = {e["correo"]: e for e in bdm_chile["ejecutivos_detalle"]}
    check("BDM/Chile detalle: nombre de Beto", bdm_chile_execs.get("beto@apprecio.com", {}).get("nombre"), "Beto")
    check("BDM/Chile detalle: tickets de Beto (Chile reparte 100% a KAM, no le toca pool)", bdm_chile_execs.get("beto@apprecio.com", {}).get("tickets_mensual"), 1.0)

    # Colombia: sinid no tiene nombre en Leyenda -> se deriva del correo. Es la única KAM ahí,
    # así que se lleva el 100% del fallback de Colombia que cae en KAM (emp20).
    kam_col_execs = {e["correo"]: e for e in kam_col["ejecutivos_detalle"]}
    check("KAM/Colombia detalle: nombre derivado del correo (sinid)", kam_col_execs.get("sinid@apprecio.com", {}).get("nombre"), "Sinid")
    check("KAM/Colombia detalle: tickets de sinid (100% del pool, no tiene propios)", kam_col_execs.get("sinid@apprecio.com", {}).get("tickets_mensual"), 1.0)
    check("KAM/Colombia detalle: clientes de sinid (pool, no tiene propios)", kam_col_execs.get("sinid@apprecio.com", {}).get("total_clientes"), 1.0)
    check("BDM/Colombia sin ejecutivos reales (lista vacía, nadie a quién repartirle)", bdm_col["ejecutivos_detalle"], [])

    fc_mex_execs = {e["correo"]: e for e in fc_mex["ejecutivos_detalle"]}
    check("Full Cycle/México detalle: tickets de diego (100% del pool)", fc_mex_execs.get("diego@apprecio.com", {}).get("tickets_mensual"), 1.0)
    check("Full Cycle/México detalle: usuarios de diego (100% del pool)", fc_mex_execs.get("diego@apprecio.com", {}).get("usuarios_total"), 15.0)

    kam_peru_execs = {e["correo"]: e for e in kam_peru["ejecutivos_detalle"]}
    check("KAM/Perú detalle: tickets de fer (100% del pool)", kam_peru_execs.get("fer@apprecio.com", {}).get("tickets_mensual"), 1.0)

    check("No debe existir 'pool_sin_asignar' en la salida (se repartió, no se muestra aparte)", "pool_sin_asignar" in kam_chile, False)

    # --- Meta: sin_asignar / redistribuidos / excluidos ---
    check("Tickets sin dueño reconocido (total)", output["meta"]["tickets_sin_asignar"], 7)
    check("Tickets redistribuidos por fallback", output["meta"]["tickets_redistribuidos"], 6)
    check("Tickets excluidos (Ecuador, sin regla)", output["meta"]["tickets_excluidos"], 1)

    check("Usuarios sin dueño reconocido (total)", output["meta"]["usuarios_sin_asignar"], 4)
    check("Usuarios redistribuidos por fallback", output["meta"]["usuarios_redistribuidos"], 3)
    check("Usuarios excluidos (Ecuador, sin regla)", output["meta"]["usuarios_excluidos"], 1)

    check("Reuniones sin match (r5)", output["meta"]["reuniones_sin_match"], 1)

    # --- Reporte detallado de sin-asignar (incluye a quién se redistribuyó) ---
    tix_detalle = {d["empresa_id"]: d for d in unmatched_report["tickets_sin_asignar"]}
    check("Detalle tickets: empresa 9 presente", "9" in tix_detalle, True)
    check("Detalle tickets: motivo empresa 9", tix_detalle.get("9", {}).get("motivo"), "email_kam_no_esta_en_leyenda")
    check("Detalle tickets: empresa 9 redistribuida a KAM", tix_detalle.get("9", {}).get("redistribuido_a"), "KAM")
    check("Detalle tickets: empresa 99 presente", "99" in tix_detalle, True)
    check("Detalle tickets: motivo empresa 99", tix_detalle.get("99", {}).get("motivo"), "empresa_no_esta_en_hoja_empresas")
    check("Detalle tickets: empresa 44 redistribuida a BDM (Colombia)", tix_detalle.get("44", {}).get("redistribuido_a"), "BDM")
    check("Detalle tickets: empresa 30 redistribuida a Full Cycle (México)", tix_detalle.get("30", {}).get("redistribuido_a"), "Full Cycle")
    check("Detalle tickets: empresa 40 (Ecuador) sin redistribuir", tix_detalle.get("40", {}).get("redistribuido_a"), None)

    usr_detalle = {d["empresa_id"]: d for d in unmatched_report["usuarios_sin_asignar"]}
    check("Detalle usuarios: empresa 9 presente", "9" in usr_detalle, True)
    check("Detalle usuarios: usuarios_incentivados empresa 9", usr_detalle.get("9", {}).get("usuarios_incentivados"), 20.0)
    check("Detalle usuarios: empresa 9 redistribuida a KAM", usr_detalle.get("9", {}).get("redistribuido_a"), "KAM")

    reu_keys = [(d["kam_id"], d["seller_email"]) for d in unmatched_report["reuniones_sin_match"]]
    check("Detalle reuniones: fila r5 presente", ("#N/A", "desconocido@apprecio.com") in reu_keys, True)

    check("KAM/Chile reuniones_mensual", kam_chile["reuniones_mensual"], 2.0)
    check("KAM/Chile reuniones_exec", kam_chile["reuniones_exec"], 2.0)

    check("Tabs presentes", sorted(output["tabs"].keys()), ["BDM", "Full Cycle", "KAM"])
    check("Month labels", output["month_labels"], ["Ene 26", "Feb 26"])

    if errors:
        print("FALLÓ:")
        for e in errors:
            print(" -", e)
        sys.exit(1)
    else:
        print("TODOS LOS CHECKS PASARON ✔")


def test_ids_no_se_mezclan_entre_paises():
    """Regresión CRÍTICA: el "ID Empresa" es correlativo POR país en la hoja Empresas
    (cada país arranca su propia numeración desde 1), no es un ID global. Antes, el mapa
    ID->dueño se armaba sin distinguir país, así que un ID "1" de Chile y un ID "1" de
    Colombia (empresas totalmente distintas) se pisaban entre sí -el último país leído
    ganaba-, mezclando tickets de un país con el ejecutivo de otro. Esto es exactamente
    lo que reportó el usuario: Colombia mostraba más que Chile a pesar de tener menos
    tickets reales. Ahora la búsqueda es por (país, ID), así que no se pueden mezclar.
    """
    leyenda_h = ["Correo", "KAM ID", "Rol", "Pais"]
    leyenda_rows = [
        ["beto@apprecio.com", "BE", "BDM", "Chile"],
        ["carla@apprecio.com", "CA", "KAM", "Colombia"],
    ]
    leyenda_records, leyenda_hidx = records(leyenda_h, leyenda_rows)

    # Mismo ID "1" en dos países, dueños completamente distintos.
    empresa_owner_map = {
        ("Chile", "1"): {"email": "beto@apprecio.com", "pais": "Chile"},
        ("Colombia", "1"): {"email": "carla@apprecio.com", "pais": "Colombia"},
    }

    tickets_h = ["id", "empresa", "id_tributario", "fechaCreacion", "pais"]
    tickets_rows = [
        ["1", "1", "A", "2025-01-05", "Chile"],
        ["2", "1", "B", "2025-02-05", "Chile"],
        ["3", "1", "C", "2025-01-10", "Colombia"],
        ["4", "1", "D", "2025-02-10", "Colombia"],
        ["5", "1", "E", "2025-03-10", "Colombia"],
    ]
    tickets_records, tickets_hidx = records(tickets_h, tickets_rows)

    usuarios_records, usuarios_hidx = [], build_header_index(
        ["ID Empresa", "N usuarios incentivados", "Pais"]
    )
    reuniones_records, reuniones_hidx = [], build_header_index(
        ["ID_REUNION", "FECHA_ISO", "ANIO", "MES", "SELLER_EMAIL", "KAM ID"]
    )

    output, _ = build_dashboard_data(
        tickets_records, tickets_hidx,
        usuarios_records, usuarios_hidx,
        reuniones_records, reuniones_hidx,
        leyenda_records, leyenda_hidx,
        empresa_owner_map,
    )

    errors = []

    def check(label, actual, expected):
        if actual != expected:
            errors.append(f"{label}: esperado {expected}, obtuve {actual}")

    bdm_chile = output["tabs"]["BDM"]["countries"]["Chile"]
    kam_col = output["tabs"]["KAM"]["countries"]["Colombia"]

    check("Beto (Chile) solo se lleva sus 2 tickets, no los de Carla", bdm_chile["total_clientes"], 2)
    check("Carla (Colombia) solo se lleva sus 3 tickets, no los de Beto", kam_col["total_clientes"], 3)
    check("Detalle: Beto en BDM/Chile", bdm_chile["ejecutivos_detalle"][0]["nombre"], "Beto")
    check("Detalle: Carla en KAM/Colombia", kam_col["ejecutivos_detalle"][0]["nombre"], "Carla")

    if errors:
        print("FALLÓ (IDs cruzados entre países):")
        for e in errors:
            print(" -", e)
        sys.exit(1)
    else:
        print("IDs NO SE MEZCLAN ENTRE PAÍSES: OK ✔")


def test_pais_real_del_dueno_gana_sobre_tab_de_empresas():
    """Caso más angosto: un ejecutivo puede tener una cuenta registrada en la pestaña
    de Empresas de OTRO país (ej. gestiona remotamente una cuenta de Perú) pero su país
    de trabajo real -el que importa para las métricas de carga operativa- es el que
    dice su propia fila en la Leyenda. Ese debe ser el que gane."""
    leyenda_h = ["Correo", "KAM ID", "Rol", "Pais"]
    leyenda_rows = [["diana@apprecio.com", "DI", "KAM", "Chile"]]
    leyenda_records, leyenda_hidx = records(leyenda_h, leyenda_rows)

    # La empresa "5" vive en la pestaña Perú de "Empresas", pero la dueña (Diana) es KAM/Chile.
    empresa_owner_map = {("Perú", "5"): {"email": "diana@apprecio.com", "pais": "Perú"}}

    tickets_h = ["id", "empresa", "id_tributario", "fechaCreacion", "pais"]
    tickets_rows = [["1", "5", "Z", "2025-01-05", "Perú"]]
    tickets_records, tickets_hidx = records(tickets_h, tickets_rows)

    usuarios_records, usuarios_hidx = [], build_header_index(
        ["ID Empresa", "N usuarios incentivados", "Pais"]
    )
    reuniones_records, reuniones_hidx = [], build_header_index(
        ["ID_REUNION", "FECHA_ISO", "ANIO", "MES", "SELLER_EMAIL", "KAM ID"]
    )

    output, _ = build_dashboard_data(
        tickets_records, tickets_hidx,
        usuarios_records, usuarios_hidx,
        reuniones_records, reuniones_hidx,
        leyenda_records, leyenda_hidx,
        empresa_owner_map,
    )

    errors = []

    def check(label, actual, expected):
        if actual != expected:
            errors.append(f"{label}: esperado {expected}, obtuve {actual}")

    check("El ticket va a Chile (país real de Diana), no a Perú", output["tabs"]["KAM"]["countries"]["Chile"]["total_clientes"], 1)
    check("Perú no recibe nada (la empresa vive ahí, pero la dueña trabaja desde Chile)", output["tabs"]["KAM"]["countries"]["Perú"]["total_clientes"], 0)

    if errors:
        print("FALLÓ (país real del dueño):")
        for e in errors:
            print(" -", e)
        sys.exit(1)
    else:
        print("PAÍS REAL DEL DUEÑO GANA: OK ✔")


if __name__ == "__main__":
    main()
    test_ids_no_se_mezclan_entre_paises()
    test_pais_real_del_dueno_gana_sobre_tab_de_empresas()

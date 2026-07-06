"""
Test rápido con datos falsos para validar la lógica de atribución por ejecutivo
(Tickets/Usuarios -> Empresas -> Leyenda) sin necesitar credenciales de Google.

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
    ]
    leyenda_records, leyenda_hidx = records(leyenda_h, leyenda_rows)
    # Headcount esperado: KAM/Chile=1, BDM/Chile=1, Full Cycle/Colombia=1, KAM/Colombia=1

    # --- Empresas (mapa ID Empresa -> dueño). Empresa "9" sin dueño reconocible en Leyenda (owner_x)
    empresa_owner_map = {
        "1": {"email": "ana@apprecio.com", "pais": "Chile"},
        "2": {"email": "ana@apprecio.com", "pais": "Chile"},
        "3": {"email": "beto@apprecio.com", "pais": "Chile"},
        "4": {"email": "sinid@apprecio.com", "pais": "Colombia"},
        "9": {"email": "owner_x@apprecio.com", "pais": "Chile"},  # no está en Leyenda
    }

    # --- Tickets: empresas 1 y 2 (Ana/KAM/Chile), empresa 3 (Beto/BDM/Chile),
    #     empresa 9 (dueño no reconocido -> sin asignar), empresa 99 (no existe en Empresas -> sin asignar)
    tickets_h = ["id", "empresa", "id_tributario", "fechaCreacion", "pais"]
    tickets_rows = [
        ["1", "1", "RUT-A", "2025-07-02 9:00:00", "Chile"],
        ["2", "1", "RUT-A", "2025-07-05 9:00:00", "Chile"],
        ["3", "2", "RUT-B", "2025-07-10 9:00:00", "Chile"],
        ["4", "1", "RUT-A", "2025-08-01 9:00:00", "Chile"],
        ["5", "3", "RUT-C", "2025-07-15 9:00:00", "Chile"],
        ["6", "9", "RUT-D", "2025-07-15 9:00:00", "Chile"],
        ["7", "99", "RUT-E", "2025-07-15 9:00:00", "Chile"],
    ]
    tickets_records, tickets_hidx = records(tickets_h, tickets_rows)
    # KAM/Chile (Ana, empresas 1 y 2): clientes únicos {RUT-A, RUT-B} = 2
    #   meses activos: jul (3 tickets: emp1x2 + emp2x1), ago (1 ticket emp1) -> (3+1)/2 = 2.0
    # BDM/Chile (Beto, empresa 3): 1 cliente {RUT-C}, 1 ticket en jul -> mensual 1.0
    # tickets_sin_asignar: empresa 9 (dueño no en leyenda) + empresa 99 (no en empresas) = 2

    # --- Usuarios: columna L por ID Empresa
    usuarios_h = ["ID Empresa", "N° usuarios incentivados", "País"]
    usuarios_rows = [
        ["1", "100", "Chile"],   # Ana/KAM/Chile
        ["2", "50", "Chile"],    # Ana/KAM/Chile
        ["3", "30", "Chile"],    # Beto/BDM/Chile
        ["9", "20", "Chile"],    # sin asignar (owner no en Leyenda)
        ["4", "", "Colombia"],   # columna L vacía -> excluido
    ]
    usuarios_records, usuarios_hidx = records(usuarios_h, usuarios_rows)
    # KAM/Chile usuarios_total = 150, BDM/Chile = 30, usuarios_sin_asignar = 1

    # --- Reuniones (igual que antes, vía KAM ID / email)
    reuniones_h = ["ID_REUNION", "FECHA_ISO", "ANIO", "MES", "SELLER_EMAIL", "KAM ID"]
    reuniones_rows = [
        ["r1", "2026-01-10", "2026", "1", "ana@apprecio.com", "AN"],
        ["r2", "2026-01-15", "2026", "1", "ana@apprecio.com", "AN"],
        ["r3", "2026-02-01", "2026", "2", "beto@apprecio.com", "BE"],
        ["r4", "2026-01-20", "2026", "1", "sinid@apprecio.com", "#N/A"],
        ["r5", "2026-01-22", "2026", "1", "desconocido@apprecio.com", "#N/A"],
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
    fc_col = output["tabs"]["Full Cycle"]["countries"]["Colombia"]
    ecuador_kam = output["tabs"]["KAM"]["countries"]["Ecuador"]

    check("Headcount KAM/Chile", kam_chile["ejecutivos"], 1)
    check("Headcount BDM/Chile", bdm_chile["ejecutivos"], 1)
    check("Headcount KAM/Colombia", kam_col["ejecutivos"], 1)
    check("Headcount Full Cycle/Colombia", fc_col["ejecutivos"], 1)
    check("Headcount KAM/Ecuador (nadie en Leyenda)", ecuador_kam["ejecutivos"], 0)

    check("KAM/Chile total_clientes (RUT-A, RUT-B)", kam_chile["total_clientes"], 2)
    check("KAM/Chile tickets_mensual", kam_chile["tickets_mensual"], 2.0)
    check("KAM/Chile tickets_exec (1 exec)", kam_chile["tickets_exec"], 2.0)
    check("BDM/Chile total_clientes (RUT-C)", bdm_chile["total_clientes"], 1)
    check("BDM/Chile tickets_mensual", bdm_chile["tickets_mensual"], 1.0)

    check("KAM/Chile usuarios_total", kam_chile["usuarios_total"], 150.0)
    check("BDM/Chile usuarios_total", bdm_chile["usuarios_total"], 30.0)

    check("Tickets sin asignar (empresa 9 + empresa 99)", output["meta"]["tickets_sin_asignar"], 2)
    check("Usuarios sin asignar (empresa 9)", output["meta"]["usuarios_sin_asignar"], 1)
    check("Reuniones sin match (r5)", output["meta"]["reuniones_sin_match"], 1)

    # --- Reporte detallado de sin-asignar ---
    tix_detalle = {d["empresa_id"]: d for d in unmatched_report["tickets_sin_asignar"]}
    check("Detalle tickets: empresa 9 presente", "9" in tix_detalle, True)
    check("Detalle tickets: motivo empresa 9", tix_detalle.get("9", {}).get("motivo"), "email_kam_no_esta_en_leyenda")
    check("Detalle tickets: empresa 99 presente", "99" in tix_detalle, True)
    check("Detalle tickets: motivo empresa 99", tix_detalle.get("99", {}).get("motivo"), "empresa_no_esta_en_hoja_empresas")

    usr_detalle = {d["empresa_id"]: d for d in unmatched_report["usuarios_sin_asignar"]}
    check("Detalle usuarios: empresa 9 presente", "9" in usr_detalle, True)
    check("Detalle usuarios: usuarios_incentivados empresa 9", usr_detalle.get("9", {}).get("usuarios_incentivados"), 20.0)

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


if __name__ == "__main__":
    main()

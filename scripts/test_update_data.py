"""
Test rápido con datos falsos (basados en filas reales que vimos en la sheet)
para validar la lógica de agregación sin necesitar credenciales de Google.

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
    # --- Tickets: 2 empresas en Chile (3 tickets en jul, 1 en ago), 1 en Colombia
    tickets_h = ["id", "empresa", "id_tributario", "fechaCreacion", "pais"]
    tickets_rows = [
        ["1", "A", "76.339.081-0", "2025-07-02 9:00:00", "Chile"],
        ["2", "A", "76.339.081-0", "2025-07-05 9:00:00", "Chile"],
        ["3", "B", "96568740-8", "2025-07-10 9:00:00", "Chile"],
        ["4", "A", "76339081-0", "2025-08-01 9:00:00", "Chile"],  # mismo id_tributario, sin puntos
        ["5", "C", "900000-1", "2025-07-15 9:00:00", "Colombia"],
    ]
    tickets_records, tickets_hidx = records(tickets_h, tickets_rows)

    # Expectativa Chile: clientes únicos = {763390810, 965687408} = 2
    #   meses activos: jul(3 tickets), ago(1 ticket) -> promedio = (3+1)/2 = 2.0
    # Colombia: 1 cliente, 1 ticket en jul -> promedio = 1.0

    # --- Usuarios: columna L, filtrar vacíos
    usuarios_h = ["ID Empresa", "N° usuarios incentivados", "País"]
    usuarios_rows = [
        ["A", "100", "Chile"],
        ["B", "50", "Chile"],
        ["X", "", "Chile"],       # vacío -> excluido
        ["C", "30", "Colombia"],
    ]
    usuarios_records, usuarios_hidx = records(usuarios_h, usuarios_rows)
    # Chile usuarios_total = 150, Colombia = 30

    # --- Leyenda
    leyenda_h = ["Correo", "KAM ID", "Rol", "Pais"]
    leyenda_rows = [
        ["ana@apprecio.com", "AN", "KAM", "Chile"],
        ["beto@apprecio.com", "BE", "BDM", "Chile"],
        ["cami@apprecio.com", "CA", "Full Cycle", "Colombia"],
        ["sinid@apprecio.com", "", "KAM", "Colombia"],  # sin KAM ID, solo por email
    ]
    leyenda_records, leyenda_hidx = records(leyenda_h, leyenda_rows)

    # --- Reuniones: incluye un caso #N/A que debe matchear por email
    reuniones_h = ["ID_REUNION", "FECHA_ISO", "ANIO", "MES", "SELLER_EMAIL", "KAM ID"]
    reuniones_rows = [
        ["r1", "2026-01-10", "2026", "1", "ana@apprecio.com", "AN"],
        ["r2", "2026-01-15", "2026", "1", "ana@apprecio.com", "AN"],
        ["r3", "2026-02-01", "2026", "2", "beto@apprecio.com", "BE"],
        ["r4", "2026-01-20", "2026", "1", "sinid@apprecio.com", "#N/A"],  # fallback por email
        ["r5", "2026-01-22", "2026", "1", "desconocido@apprecio.com", "#N/A"],  # sin match -> excluida
    ]
    reuniones_records, reuniones_hidx = records(reuniones_h, reuniones_rows)

    # --- Config: ejecutivos
    config_h = ["Pais", "Rol", "Ejecutivos"]
    config_rows = [
        ["Chile", "KAM", "2"],
        ["Chile", "BDM", "1"],
        ["Chile", "Full Cycle", "0"],
        ["Colombia", "KAM", "1"],
        ["Colombia", "BDM", "0"],
        ["Colombia", "Full Cycle", "1"],
    ]
    config_records, config_hidx = records(config_h, config_rows)

    output = build_dashboard_data(
        tickets_records, tickets_hidx,
        usuarios_records, usuarios_hidx,
        reuniones_records, reuniones_hidx,
        leyenda_records, leyenda_hidx,
        config_records, config_hidx,
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

    check("Chile total_clientes", kam_chile["total_clientes"], 2)
    check("Chile tickets_mensual", kam_chile["tickets_mensual"], 2.0)
    check("Chile KAM tickets_exec (2 exec)", kam_chile["tickets_exec"], 1.0)
    check("Chile BDM tickets_exec (1 exec)", bdm_chile["tickets_exec"], 2.0)
    check("Chile usuarios_total", kam_chile["usuarios_total"], 150.0)
    check("Chile KAM usuarios_exec", kam_chile["usuarios_exec"], 75.0)

    check("Colombia total_clientes", kam_col["total_clientes"], 1)
    check("Colombia usuarios_total", kam_col["usuarios_total"], 30.0)

    # Reuniones: Ana (KAM, Chile) tiene 2 en enero -> reuniones_mensual=2.0, ejecutivos=2 -> exec=1.0
    check("Chile KAM reuniones_mensual", kam_chile["reuniones_mensual"], 2.0)
    check("Chile KAM reuniones_exec", kam_chile["reuniones_exec"], 1.0)
    # Beto (BDM, Chile) 1 en feb -> mensual=1.0, ejecutivos=1 -> exec=1.0
    check("Chile BDM reuniones_mensual", bdm_chile["reuniones_mensual"], 1.0)
    # sinid (KAM, Colombia, matcheado por email) 1 en enero -> mensual=1.0, ejecutivos=1 -> exec=1.0
    check("Colombia KAM reuniones_mensual", kam_col["reuniones_mensual"], 1.0)
    # Full Cycle Colombia (Cami) no tuvo reuniones en el sample -> 0
    check("Colombia Full Cycle reuniones_mensual", fc_col["reuniones_mensual"], 0)

    check("Reuniones sin match", output["meta"]["reuniones_sin_match"], 1)  # r5

    # Ecuador sin datos en ninguna sheet -> todo en 0, sin división por cero
    check("Ecuador ejecutivos", ecuador_kam["ejecutivos"], 0)
    check("Ecuador tickets_exec (0 exec, no debe reventar)", ecuador_kam["tickets_exec"], 0)

    check("Tabs presentes", sorted(output["tabs"].keys()), ["BDM", "Full Cycle", "KAM"])
    check("Month labels", output["month_labels"], ["Ene 26", "Feb 26"])

    if errors:
        print("FALLÓ:")
        for e in errors:
            print(" -", e)
        sys.exit(1)
    else:
        print("TODOS LOS CHECKS PASARON ✔")
        print(f"reuniones_sin_match = {output['meta']['reuniones_sin_match']}")


if __name__ == "__main__":
    main()

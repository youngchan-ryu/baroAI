"""Freeze reference labels for legacy snapshots without claiming historical verification."""

import sqlalchemy as sa
from alembic import op

revision = "v10720261103"
down_revision = "v10620261103"
branch_labels = None
depends_on = None

# This is the 2026-11-03 application code table, not proof of an earlier display.
LEGACY_REFERENCE = {
    "ARCH_ACCESSIBILITY": {
        "department_code": "FAC_ARCH_CIVIL",
        "domain": "ARCH_CIVIL",
        "label": "장애인 편의시설",
    },
    "ARCH_DOOR_WINDOW_LOCK": {
        "department_code": "FAC_ARCH_CIVIL",
        "domain": "ARCH_CIVIL",
        "label": "문·창·잠금",
    },
    "ARCH_FIRE_DOOR": {
        "department_code": "FAC_ARCH_CIVIL",
        "domain": "ARCH_CIVIL",
        "label": "방화문·건축 소방",
    },
    "ARCH_FLOOR_WALL_CEILING": {
        "department_code": "FAC_ARCH_CIVIL",
        "domain": "ARCH_CIVIL",
        "label": "바닥·벽·천장",
    },
    "ARCH_GLASS": {"department_code": "FAC_ARCH_CIVIL", "domain": "ARCH_CIVIL", "label": "유리"},
    "ARCH_OTHER": {
        "department_code": "FAC_ARCH_CIVIL",
        "domain": "ARCH_CIVIL",
        "label": "건축 기타",
    },
    "ARCH_WATERPROOF": {
        "department_code": "FAC_ARCH_CIVIL",
        "domain": "ARCH_CIVIL",
        "label": "방수",
    },
    "BUILDING": {"department_code": "FACILITY_REVIEW", "domain": "FACILITY", "label": "건축·시설"},
    "CIVIL_DRAINAGE": {"department_code": "FAC_CIVIL", "domain": "CIVIL", "label": "배수로·맨홀"},
    "CIVIL_OTHER": {"department_code": "FAC_CIVIL", "domain": "CIVIL", "label": "토목 기타"},
    "CIVIL_RETAINING_WALL": {
        "department_code": "FAC_CIVIL",
        "domain": "CIVIL",
        "label": "옹벽·석축",
    },
    "CIVIL_ROAD_SIDEWALK": {
        "department_code": "FAC_CIVIL",
        "domain": "CIVIL",
        "label": "도로·보도",
    },
    "ELECTRICAL": {"department_code": "FACILITY_REVIEW", "domain": "FACILITY", "label": "전기"},
    "ELEC_ELEVATOR": {"department_code": "FAC_ELEC", "domain": "ELEC", "label": "승강기"},
    "ELEC_EV_CHARGER": {"department_code": "FAC_ELEC", "domain": "ELEC", "label": "전기차 충전"},
    "ELEC_FIRE_ALARM": {
        "department_code": "FAC_ELEC",
        "domain": "ELEC",
        "label": "전기 소방(감지기·경보)",
    },
    "ELEC_LIGHTING": {"department_code": "FAC_ELEC", "domain": "ELEC", "label": "조명"},
    "ELEC_OTHER": {"department_code": "FAC_ELEC", "domain": "ELEC", "label": "전기 기타"},
    "ELEC_OUTLET_SWITCH": {
        "department_code": "FAC_ELEC",
        "domain": "ELEC",
        "label": "콘센트·스위치",
    },
    "ELEC_POWER_OUTAGE": {"department_code": "FAC_ELEC", "domain": "ELEC", "label": "정전"},
    "ELEC_TELECOM_PHONE": {
        "department_code": "FAC_ELEC",
        "domain": "ELEC",
        "label": "통신·구내전화",
    },
    "HVAC": {"department_code": "FACILITY_REVIEW", "domain": "FACILITY", "label": "냉난방"},
    "MECH_FIRE_SUPPRESSION": {
        "department_code": "FAC_MECH",
        "domain": "MECH",
        "label": "기계 소방(스프링클러·소화전)",
    },
    "MECH_HOT_WATER": {"department_code": "FAC_MECH", "domain": "MECH", "label": "급탕"},
    "MECH_HVAC": {"department_code": "FAC_MECH", "domain": "MECH", "label": "냉난방·공조"},
    "MECH_OTHER": {"department_code": "FAC_MECH", "domain": "MECH", "label": "기계 기타"},
    "MECH_PLUMBING_CLOG": {
        "department_code": "FAC_MECH",
        "domain": "MECH",
        "label": "배관·변기·세면대 막힘",
    },
    "MECH_SANITARY_FIXTURE": {
        "department_code": "FAC_MECH",
        "domain": "MECH",
        "label": "위생기구 파손(수전·변기 부품)",
    },
    "MECH_WATER_LEAK": {"department_code": "FAC_MECH", "domain": "MECH", "label": "누수"},
    "PLUMBING": {"department_code": "FACILITY_REVIEW", "domain": "FACILITY", "label": "급배수"},
    "UNKNOWN": {"department_code": "REVIEW", "domain": "UNKNOWN", "label": "미분류 · 담당자 확인"},
    "XFER_CAMPUS_SAFETY": {
        "department_code": "CAMPUS_SAFETY",
        "domain": "TRANSFER",
        "label": "보안·안전",
    },
    "XFER_CLEANING": {"department_code": "CLEANING", "domain": "TRANSFER", "label": "청소"},
    "XFER_CONSTRUCTION_SITE": {
        "department_code": "CONSTRUCTION",
        "domain": "TRANSFER",
        "label": "공사 현장 위험",
    },
    "XFER_DORM": {"department_code": "DORM", "domain": "TRANSFER", "label": "기숙사"},
    "XFER_IT": {"department_code": "IT", "domain": "TRANSFER", "label": "네트워크·IT"},
    "XFER_LAB_SAFETY": {
        "department_code": "LAB_SAFETY",
        "domain": "TRANSFER",
        "label": "실험실·화학",
    },
    "XFER_LANDSCAPE": {"department_code": "LANDSCAPE", "domain": "TRANSFER", "label": "조경·수목"},
}


def upgrade():
    connection = op.get_bind()
    statement = sa.text("""
        UPDATE tickets SET catalog_snapshot = catalog_snapshot || jsonb_build_object(
          'label', :label, 'domain', :domain, 'department_code', :department,
          'legacy_display_source', 'application-catalog-2026-11-03',
          'legacy_display_unverified', true)
        WHERE catalog_snapshot->>'legacy' = 'true' AND NOT (catalog_snapshot ? 'label')
          AND category_code = :code
    """).bindparams(
        *(
            sa.bindparam(name, type_=sa.String())
            for name in ("label", "domain", "department", "code")
        )
    )
    for code, values in LEGACY_REFERENCE.items():
        connection.execute(
            statement,
            {
                "code": code,
                "label": values["label"],
                "domain": values["domain"],
                "department": values["department_code"],
            },
        )
    op.execute("""UPDATE assignments SET catalog_snapshot = catalog_snapshot ||
        jsonb_build_object('legacy_values_basis', 'migration_current_not_historical')
        WHERE catalog_snapshot->>'legacy' = 'true'
          AND NOT (catalog_snapshot ? 'legacy_values_basis')""")


def downgrade():
    op.execute("""UPDATE tickets SET catalog_snapshot = catalog_snapshot - 'label' - 'domain' -
        'department_code' - 'legacy_display_source' - 'legacy_display_unverified'
        WHERE catalog_snapshot->>'legacy_display_source' = 'application-catalog-2026-11-03'""")
    op.execute("""UPDATE assignments SET catalog_snapshot = catalog_snapshot - 'legacy_values_basis'
        WHERE catalog_snapshot->>'legacy_values_basis' = 'migration_current_not_historical'""")
